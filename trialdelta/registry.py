"""Prospective ClinicalTrials.gov acquisition and deterministic field-level diffs."""

import hashlib
import json
import re
import time
from contextlib import contextmanager
from pathlib import Path
import httpx
from sqlalchemy import select, text
from .db import Session, engine, Company, Study, Snapshot, Change, Job, now
from .company_names import sponsor_key

API = "https://clinicaltrials.gov/api/v2/studies"
COVERAGE_REFRESH_WARNING = (
    "Company coverage refresh failed. Continuing with the previously active coverage; "
    "check the market database and company-name dataset before the next run."
)
INACTIVE_SELECTION_WARNING = (
    "Selected companies tagged Delisted or Outside Healthcare were skipped. "
    "Their saved trial history is retained."
)
FIELDS = {
    "status": ("development_status", "Recruitment status changed"),
    "enrollment": ("enrollment", "Enrollment changed"),
    "primary_completion": ("registry_date", "Primary completion changed"),
    "completion": ("registry_date", "Study completion changed"),
    "phase": ("trial_design", "Trial phase changed"),
    "design": ("trial_design", "Trial design changed"),
    "primary_outcomes": ("trial_design", "Primary outcomes changed"),
    "results_posted": ("results", "Results publication changed"),
}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def normalize(raw):
    p = raw["protocolSection"]
    identification, status, design = (
        p["identificationModule"],
        p["statusModule"],
        p.get("designModule", {}),
    )
    if not re.fullmatch(r"NCT\d{8}", identification["nctId"]) or not status.get(
        "overallStatus"
    ):
        raise ValueError("Invalid registry study")
    interventions = p.get("armsInterventionsModule", {}).get("interventions", [])
    return {
        "nct_id": identification["nctId"],
        "title": identification.get("briefTitle", ""),
        "sponsor": p.get("sponsorCollaboratorsModule", {})
        .get("leadSponsor", {})
        .get("name", ""),
        # Keep the compatibility field name while indexing all interventions.
        "drug": "; ".join(
            sorted(
                {
                    i["name"]
                    for i in interventions
                    if i.get("name")
                }
            )
        ),
        "conditions": sorted(p.get("conditionsModule", {}).get("conditions", [])),
        "status": status["overallStatus"],
        "enrollment": design.get("enrollmentInfo"),
        "primary_completion": status.get("primaryCompletionDateStruct"),
        "completion": status.get("completionDateStruct"),
        "phase": sorted(design.get("phases", [])),
        "design": design.get("designInfo"),
        "primary_outcomes": sorted(
            p.get("outcomesModule", {}).get("primaryOutcomes", []), key=canonical
        ),
        "results_posted": status.get("resultsFirstPostDateStruct"),
        "source_updated": status.get("lastUpdatePostDateStruct", {}).get("date"),
    }


def display(value):
    if value is None or value == [] or value == "":
        return "Not reported"
    if isinstance(value, dict) and "date" in value:
        return f"{value['date']} ({value.get('type', 'type not reported').lower()})"
    if isinstance(value, dict) and "count" in value:
        return f"{value['count']} participants ({value.get('type', 'type not reported').lower()})"
    if isinstance(value, str):
        return value.replace("_", " ").title()
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def ingest(db, company, raw):
    n = normalize(raw)
    study = db.get(Study, n["nct_id"])
    if study and study.company_id != company.id:
        return 0
    if not study:
        study = Study(id=n["nct_id"], company_id=company.id, title=n["title"])
        db.add(study)
        db.flush()
    old = db.scalar(
        select(Snapshot)
        .where(Snapshot.study_id == study.id)
        .order_by(Snapshot.id.desc())
        .limit(1)
    )
    digest = hashlib.sha256(canonical(raw).encode()).hexdigest()
    study.title, study.drug, study.conditions = (
        n["title"],
        n["drug"][:500],
        canonical(n["conditions"]),
    )
    study.status, study.phase = n["status"], ", ".join(n["phase"])
    study.primary_completion = (n["primary_completion"] or {}).get("date")
    study.completion = (n["completion"] or {}).get("date")
    study.last_checked = now()
    if old and old.digest == digest:
        return 0
    snapshot = Snapshot(
        study_id=study.id,
        digest=digest,
        raw_json=canonical(raw),
        normalized=canonical(n),
        source_updated=n["source_updated"],
    )
    db.add(snapshot)
    db.flush()
    count = 0
    if old:
        previous = json.loads(old.normalized)
        for key, (category, title) in FIELDS.items():
            if previous.get(key) != n.get(key):
                db.add(
                    Change(
                        company_id=company.id,
                        study_id=study.id,
                        category=category,
                        title=title,
                        before_value=display(previous.get(key)),
                        after_value=display(n.get(key)),
                        source="registry",
                        review_status="automatic",
                        reviewed=False,
                        before_snapshot=old.id,
                        after_snapshot=snapshot.id,
                    )
                )
                count += 1
    return count


def fetch_company(company):
    """Collect every study type and condition for the exact lead sponsor."""
    params = {
        "query.spons": company.sponsor,
        "format": "json",
        "pageSize": 100,
    }
    studies = []
    seen_tokens = set()
    with httpx.Client(timeout=45) as client:
        while True:
            for attempt in range(3):
                response = client.get(API, params=params)
                if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2**attempt)
                    continue
                response.raise_for_status()
                break
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("studies"), list):
                raise ValueError("Malformed registry response")
            token = payload.get("nextPageToken")
            if token is not None and (not isinstance(token, str) or not token):
                raise ValueError("Invalid registry page cursor; no partial update applied")
            if token in seen_tokens:
                raise ValueError("Repeated registry page cursor; no partial update applied")
            for raw in payload["studies"]:
                n = normalize(raw)
                if sponsor_key(n["sponsor"]) == sponsor_key(company.sponsor):
                    studies.append(raw)
            if not token:
                return studies
            seen_tokens.add(token)
            params["pageToken"] = token
            time.sleep(0.25)


@contextmanager
def sync_lock():
    if engine.dialect.name == "mysql":
        with engine.connect() as connection:
            acquired = connection.scalar(
                text("SELECT GET_LOCK('trialdelta_registry_sync', 0)")
            )
            try:
                yield acquired == 1
            finally:
                if acquired == 1:
                    connection.execute(
                        text("SELECT RELEASE_LOCK('trialdelta_registry_sync')")
                    )
    else:
        Path("data").mkdir(exist_ok=True)
        with open("data/sync.lock", "a+b") as handle:
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            try:
                import os

                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def interrupt_running_jobs(db, except_job_id=None):
    """Only call while holding sync_lock; preserve bulk checkpoints and totals."""
    for previous in db.scalars(select(Job).where(Job.status == "running")):
        if previous.id == except_job_id:
            continue
        previous.status, previous.finished_at = "interrupted", now()
        try:
            summary = json.loads(previous.summary or "{}")
        except (TypeError, ValueError):
            summary = {}
        if not isinstance(summary, dict):
            summary = {}
        summary["errors"] = ["Previous collection process stopped before completion."]
        previous.summary = canonical(summary)


def refresh_company_coverage(emit=None):
    """Refresh under the caller's collection lock; retain coverage on source failure."""
    from .company_discovery import CompanyDiscoveryError, discover_companies

    emit = emit or (lambda message: None)
    try:
        result = discover_companies(emit)
    except CompanyDiscoveryError:
        # Use fixed text: source exceptions can contain private connection details.
        emit(COVERAGE_REFRESH_WARNING)
        return {"status": "unavailable"}, [COVERAGE_REFRESH_WARNING]
    return {key: value for key, value in result.items() if key != "company_ids"}, []


def safe_coverage_warnings(warnings):
    """Only application-authored warnings may be shown from stored job summaries."""
    allowed = (COVERAGE_REFRESH_WARNING, INACTIVE_SELECTION_WARNING)
    if not isinstance(warnings, list):
        return []
    return [value for value in warnings if isinstance(value, str) and value in allowed]


def sync_all(company_id=None, refresh_coverage=False):
    with sync_lock() as acquired:
        if not acquired:
            return {
                "status": "busy",
                "detail": "A registry refresh is already running.",
            }
        coverage, warnings = refresh_company_coverage() if refresh_coverage else (None, [])
        with Session() as db:
            # Owning the collection lock proves no previous running job is still active.
            interrupt_running_jobs(db)
            job = Job()
            db.add(job)
            db.commit()
            query = select(Company).where(Company.coverage_status == "active")
            if company_id is not None:
                query = query.where(Company.id == company_id)
                selected = db.get(Company, company_id)
                if selected is not None and selected.coverage_status != "active":
                    warnings.append(INACTIVE_SELECTION_WARNING)
            companies = db.scalars(query).all()
            totals = {"companies": 0, "studies": 0, "changes": 0, "errors": [], "warnings": warnings}
            if coverage is not None:
                totals["coverage"] = coverage
            for company in companies:
                try:
                    rows = fetch_company(company)
                    changes = sum(ingest(db, company, raw) for raw in rows)
                    company.last_checked, company.last_error = now(), None
                    db.commit()
                    totals["companies"] += 1
                    totals["studies"] += len(rows)
                    totals["changes"] += changes
                except Exception as exc:
                    db.rollback()
                    company.last_error = f"{type(exc).__name__}: Registry refresh failed; last good snapshot retained."
                    totals["errors"].append(
                        {"company": company.ticker, "error": company.last_error}
                    )
                    db.commit()
                time.sleep(0.35)
            job.status = "partial" if totals["errors"] else "complete"
            job.finished_at, job.summary = now(), canonical(totals)
            db.commit()
            return {"status": job.status, **totals}
