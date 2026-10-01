"""Database operations for the trusted desktop administrator.

No collector runs on import or during initialization. Every operation owns its
session so the GUI can call it from a worker thread without sharing ORM objects.
"""

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import func, select

from . import auth, registry, bulk_download, company_discovery
from .db import Company, Study, Snapshot, Change, Evidence, Job, User, Session, engine
from .inbox import SourceCandidate, import_news
from .services import as_dict, change_needs_review
from .tad import (
    TADValidationError, add_manual_target_action_date, delete_target_action_date,
    edit_target_action_date, import_target_action_dates, list_target_action_dates,
)


class AdminOperationError(Exception):
    """A deliberately safe, user-facing desktop error."""


def safe_error(exc):
    """Never display driver/HTTP exception messages, URLs, SQL, or credentials."""
    if isinstance(exc, (AdminOperationError, TADValidationError, bulk_download.BulkDownloadError, company_discovery.CompanyDiscoveryError)):
        return str(exc)
    if isinstance(exc, HTTPException):
        return {
            403: "Administrator access is required.",
            404: "The selected record no longer exists. Refresh the view.",
            409: "Already reviewed, or source chronology conflicts with the approved baseline. Refresh and inspect the evidence.",
            422: "The operation contains invalid data. Check the selection and try again.",
        }.get(exc.status_code, "The operation could not be completed.")
    return f"Operation failed ({type(exc).__name__}). Check database/source availability and configuration. Credentials and technical details are hidden."


def initialize_desktop():
    """Explicit desktop startup may create missing tables/directory/accounts."""
    from .db import initialize
    from .seed import seed

    initialize()
    seed()
    auth.bootstrap()
    return {"mode": auth.MODE, "database": engine.dialect.name}


def authenticate(email="", password=""):
    """Local mode trusts OS access; hosted mode requires an existing admin login."""
    with Session() as db:
        if auth.MODE == "local":
            user = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
        else:
            user = db.scalar(select(User).where(User.email == email.strip().lower()))
            if (
                not user
                or user.email == "local@trialdelta.local"
                or not user.is_admin
                or not auth.verify_password(password, user.password_hash)
            ):
                raise AdminOperationError("Sign-in failed. Use an existing administrator account.")
        if not user or not user.is_admin:
            raise AdminOperationError("Administrator account unavailable. Check desktop startup configuration.")
        return {"id": user.id, "email": user.email, "mode": auth.MODE}


def _require_admin(db, user_id):
    user = db.get(User, user_id)
    if (
        not user
        or not user.is_admin
        or (auth.MODE == "hosted" and user.email == "local@trialdelta.local")
    ):
        raise AdminOperationError("Administrator access is required.")
    return user


def _count(db, model, *conditions):
    return db.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


def _job_dict(job):
    """Show aggregate counts and known state, never arbitrary stored error text."""
    result = {k: getattr(job, k) for k in ("id", "status", "started_at", "finished_at")}
    try:
        summary = json.loads(job.summary or "{}")
    except (TypeError, ValueError):
        summary = {}
    if not isinstance(summary, dict):
        summary = {}
    for key in ("companies", "studies", "changes", "pages", "scanned", "skipped"):
        result[key] = summary.get(key) if isinstance(summary.get(key), int) else None
    errors = summary.get("errors", [])
    result["errors"] = len(errors) if isinstance(errors, list) else 0
    result["kind"] = "bulk" if summary.get("kind") == "bulk" else "company"
    result["error_messages"] = bulk_download.safe_error_messages(errors) if result["kind"] == "bulk" else []
    result["warning_messages"] = registry.safe_coverage_warnings(summary.get("warnings"))
    for key, label in (("updated_since", "Updated since"), ("updated_through", "Updated through")):
        value = summary.get(key)
        try:
            result[key] = bulk_download.validate_date(value, label) if isinstance(value, str) else None
        except bulk_download.BulkDownloadError:
            result[key] = None
    result["oncology_only"] = summary.get("oncology_only") is True
    return result


def dashboard_snapshot(user_id):
    with Session() as db:
        _require_admin(db, user_id)
        counts = {
            "companies": _count(db, Company),
            "studies": _count(db, Study),
            "snapshots": _count(db, Snapshot),
            "approved": _count(db, Change, Change.review_status == "approved"),
            "pending_changes": _count(db, Change, change_needs_review()),
            "pending_evidence": _count(db, Evidence, Evidence.review_status == "pending"),
            "inbox": _count(db, SourceCandidate, SourceCandidate.status == "unreviewed"),
        }
        counts["pending"] = counts["pending_changes"] + counts["pending_evidence"]
        study_counts = dict(db.execute(select(Study.company_id, func.count()).group_by(Study.company_id)).all())
        companies = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        for company in db.scalars(select(Company).order_by(Company.ticker)):
            last_check = company.last_checked
            try:
                checked = datetime.fromisoformat(last_check) if last_check else None
                if checked and checked.tzinfo is None:
                    checked = checked.replace(tzinfo=timezone.utc)
                freshness = "Current" if checked and checked > cutoff else "Stale" if checked else "Not collected"
            except (TypeError, ValueError):
                freshness = "Unknown"
            companies.append({
                "id": company.id, "ticker": company.ticker, "name": company.name,
                "sponsor": company.sponsor, "studies": study_counts.get(company.id, 0),
                "last_checked": last_check, "freshness": freshness,
                "coverage_status": company.coverage_status,
                "coverage_label": {
                    "active": "Active", "delisted": "Delisted", "out_of_sector": "Outside Healthcare",
                }.get(company.coverage_status, "Unknown"),
                "error": "Refresh failed; last good snapshot retained" if company.last_error else "",
            })
        jobs = [_job_dict(j) for j in db.scalars(select(Job).order_by(Job.id.desc()).limit(100))]
        bulk_jobs = [_job_dict(j) for j in db.scalars(
            select(Job).join(bulk_download.BulkCheckpoint, bulk_download.BulkCheckpoint.job_id == Job.id)
            .order_by(Job.id.desc()).limit(100)
        )]
        return {"counts": counts, "companies": companies, "jobs": jobs, "bulk_jobs": bulk_jobs, "database": engine.dialect.name}


def review_items(user_id):
    with Session() as db:
        _require_admin(db, user_id)
        companies = {c.id: c.ticker for c in db.scalars(select(Company))}
        items = []
        for kind, model in (("change", Change), ("evidence", Evidence)):
            pending = change_needs_review() if model is Change else Evidence.review_status == "pending"
            for row in db.scalars(select(model).where(pending).order_by(model.id)):
                items.append({
                    "id": row.id, "kind": kind, "ticker": companies.get(row.company_id, ""),
                    "study_id": row.study_id or "Company-level", "title": row.title,
                    "category": row.category,
                    "date": row.detected_at if kind == "change" else row.published_at,
                })
        return items


def review_detail(user_id, kind, item_id):
    if kind not in ("change", "evidence"):
        raise AdminOperationError("Choose a change or evidence record.")
    with Session() as db:
        _require_admin(db, user_id)
        row = db.get(Change if kind == "change" else Evidence, item_id)
        if not row:
            raise AdminOperationError("The selected record no longer exists. Refresh the view.")
        company = db.get(Company, row.company_id)
        detail = {"kind": kind, "ticker": company.ticker, "record": as_dict(row)}
        if kind == "change":
            detail["snapshots"] = [
                {**as_dict(s), "raw_json": json.loads(s.raw_json), "normalized": json.loads(s.normalized)}
                for snapshot_id in (row.before_snapshot, row.after_snapshot)
                if snapshot_id and (s := db.get(Snapshot, snapshot_id))
            ]
            detail["evidence"] = [
                as_dict(e) for evidence_id in (row.before_evidence, row.after_evidence)
                if evidence_id and (e := db.get(Evidence, evidence_id))
            ]
        elif row.study_id:
            detail["approved_baseline"] = [as_dict(e) for e in db.scalars(
                select(Evidence).where(
                    Evidence.study_id == row.study_id, Evidence.category == row.category,
                    Evidence.review_status == "approved",
                ).order_by(Evidence.published_at.desc()).limit(1)
            )]
        return detail


def review(user_id, kind, item_id, action, note=""):
    from .services import review_item

    if kind not in ("change", "evidence") or action not in ("approve", "reject"):
        raise AdminOperationError("Choose a valid review action.")
    if len(note) > 2000:
        raise AdminOperationError("Review notes must be 2,000 characters or fewer.")
    with Session() as db:
        _require_admin(db, user_id)
        return review_item(db, kind, item_id, action, note, user_id)


def inbox_items(user_id):
    with Session() as db:
        _require_admin(db, user_id)
        companies = {c.id: c.ticker for c in db.scalars(select(Company))}
        return [{**as_dict(row), "ticker": companies.get(row.company_id, "")} for row in db.scalars(
            select(SourceCandidate).where(SourceCandidate.status == "unreviewed")
            .order_by(SourceCandidate.published_at.desc()).limit(500)
        )]


def dismiss_inbox(user_id, item_id):
    with Session() as db:
        _require_admin(db, user_id)
        row = db.get(SourceCandidate, item_id)
        if not row:
            raise AdminOperationError("The selected record no longer exists. Refresh the view.")
        row.status = "dismissed"
        db.commit()
        return {"status": "dismissed"}


def public_source_url(value):
    parsed = urlparse(value or "")
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise AdminOperationError("This record has no valid public HTTP(S) source URL.")
    return value


def run_registry_sync(user_id, company_ids=None, emit=None):
    """Run explicitly; a selected batch uses the collector's existing per-run lock."""
    emit = emit or (lambda message: None)
    with Session() as db:
        _require_admin(db, user_id)
        if company_ids is None:
            targets = [(None, "all covered companies")]
        else:
            ids = list(dict.fromkeys(company_ids))
            if not ids:
                raise AdminOperationError("Select at least one company.")
            companies = {c.id: c for c in db.scalars(select(Company).where(Company.id.in_(ids)))}
            if len(companies) != len(ids):
                raise AdminOperationError("A selected company no longer exists. Refresh the view.")
            inactive = [company_id for company_id in ids if companies[company_id].coverage_status != "active"]
            if inactive:
                emit(registry.INACTIVE_SELECTION_WARNING)
            targets = [(company_id, companies[company_id].ticker) for company_id in ids
                       if companies[company_id].coverage_status == "active"]
            if not targets:
                raise AdminOperationError("No selected companies are active for collection. Their saved trial history is retained.")
    results = []
    for company_id, label in targets:
        emit(f"Collecting ClinicalTrials.gov for {label}…")
        result = registry.sync_all(company_id)
        status = result.get("status", "unknown")
        results.append(result)
        for warning in registry.safe_coverage_warnings(result.get("warnings")):
            emit(warning)
        emit(f"{label}: {status}; {result.get('studies', 0)} studies, {result.get('changes', 0)} new differences, {len(result.get('errors', []))} errors.")
        if status == "busy":
            emit("Another collector owns the collection lock. Remaining selections were not started.")
            break
    status = "complete" if all(r.get("status") == "complete" for r in results) else "partial"
    return {"status": status, "runs": results}


def run_news_import(user_id):
    with Session() as db:
        _require_admin(db, user_id)
    return import_news()


def run_tad_import(user_id):
    """Explicit administrator import from the stored SEC HTML table."""
    with Session() as db:
        _require_admin(db, user_id)
    return import_target_action_dates()


def tad_items(user_id, *, status=""):
    with Session() as db:
        _require_admin(db, user_id)
        return list_target_action_dates(db, status=status, limit=500, include_identity=True)


def add_tad(user_id, *, ticker, tad, source_url="", notes=""):
    """Save an explicitly entered date through the trusted administrator only."""
    with Session() as db:
        user = _require_admin(db, user_id)
        result = add_manual_target_action_date(
            db, ticker=ticker, tad=tad, source_url=source_url, notes=notes,
            entered_by=user.id,
        )
        db.commit()
        return result


def delete_tad(user_id, item_id, *, expected_identity):
    """Delete the selected saved date group through the administrator only."""
    with Session() as db:
        _require_admin(db, user_id)
        result = delete_target_action_date(db, item_id, expected_identity=expected_identity)
        db.commit()
        return result


def edit_tad(user_id, item_id, *, expected_identity, ticker, tad, source_url="", notes=""):
    """Save corrections separately, preserving all original date evidence."""
    with Session() as db:
        user = _require_admin(db, user_id)
        result = edit_target_action_date(
            db, item_id, expected_identity=expected_identity, ticker=ticker, tad=tad,
            source_url=source_url, notes=notes, entered_by=user.id,
        )
        db.commit()
        return result


def discover_bulk_companies(user_id, emit=None):
    with Session() as db:
        _require_admin(db, user_id)
    with registry.sync_lock() as acquired:
        if not acquired:
            raise AdminOperationError("Another registry collector is running. Refresh the company list after it finishes.")
        return company_discovery.discover_companies(emit)


def run_bulk_download(user_id, company_ids=None, updated_since=None, emit=None, cancel=None, resume_job_id=None, discover_companies=False, oncology_only=False, updated_through=None):
    with Session() as db:
        _require_admin(db, user_id)
    if resume_job_id is None:
        updated_since, updated_through = bulk_download.validate_date_range(updated_since, updated_through)
    # Keep discover_companies for existing callers. Every new administrator bulk
    # run now refreshes coverage within the collector's lock, then applies its
    # requested active-company selection. Resumes retain their saved scope.
    return bulk_download.run_bulk_download(
        company_ids=company_ids, updated_since=updated_since, emit=emit,
        cancel=cancel, resume_job_id=resume_job_id, oncology_only=oncology_only,
        updated_through=updated_through,
        refresh_coverage=resume_job_id is None,
    )
