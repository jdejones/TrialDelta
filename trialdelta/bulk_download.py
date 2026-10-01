"""Explicit, resumable sponsor-batched ClinicalTrials.gov imports.

Adapted from Healthcare_Data/clinical_trials.py. Each page and its cursor commit
together; a failed page cannot advance the cursor or damage previous snapshots.
"""

import copy
import hashlib
import json
import re
import threading
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy import select
from sqlalchemy.exc import DataError

from . import registry
from .db import BulkCheckpoint, Company, Job, Session, Study, now

BATCH_SIZE = 100
PAGE_SIZE = 1000
REQUEST_INTERVAL = 1.3
ONCOLOGY_QUERY = "cancer OR neoplasm OR tumor OR leukemia OR lymphoma OR myeloma"
COUNTERS = ("companies", "studies", "changes", "pages", "scanned", "skipped")
DATABASE_STORAGE_ERROR = (
    "A registry record exceeded the database's storage limit. Update the database schema "
    "before resuming this run. Saved pages are retained."
)
DATABASE_DATA_ERROR = (
    "The database rejected a registry record (DataError). Check the database schema and "
    "record format before resuming. Saved pages are retained."
)
UNKNOWN_BULK_ERROR = (
    "Bulk download stopped. Saved pages are retained. Check database and registry "
    "availability before resuming."
)
_LEGACY_ERROR_TEMPLATE = "Bulk download stopped ({}). Saved pages are retained; resume to retry the unfinished page."
_SAFE_EXCEPTION_NAMES = frozenset({
    "ValueError", "KeyError", "TypeError", "OverflowError", "JSONDecodeError", "RuntimeError",
    "TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    "ConnectError", "ReadError", "WriteError", "CloseError", "ProxyError", "UnsupportedProtocol",
    "LocalProtocolError", "RemoteProtocolError", "HTTPStatusError", "InvalidURL", "DecodingError",
    "TooManyRedirects", "DatabaseError", "OperationalError", "IntegrityError", "InternalError",
    "ProgrammingError", "InterfaceError", "StatementError", "SQLAlchemyError", "PendingRollbackError",
    "TimeoutError", "OSError",
})
_SAFE_ERROR_MESSAGES = frozenset({
    DATABASE_STORAGE_ERROR, DATABASE_DATA_ERROR, UNKNOWN_BULK_ERROR,
    "Updated since must be a valid YYYY-MM-DD date, no later than today.",
    "Updated through must be a valid YYYY-MM-DD date, no later than today.",
    "Updated through must be on or after Updated since.",
    "The registry returned an invalid page. The last saved page is retained.",
    "The registry returned an invalid page cursor. Start a new bulk download.",
    "Select at least one company.",
    "Choose valid company IDs.",
    "A selected company no longer exists. Refresh the view.",
    "No selected companies are active for collection. Their saved trial history is retained.",
    "No companies are in coverage. Initialize the company directory first.",
    "Company lead sponsors must be nonempty and unambiguous before a bulk download.",
    "The registry rejected the query or saved cursor. Start a new download if resuming fails.",
    "Select an unfinished bulk download to resume.",
    "Company coverage changed since this run. Start a new bulk download.",
    "The registry repeated a page cursor. Start a new bulk download.",
    "Previous collection process stopped before completion.",
    *(_LEGACY_ERROR_TEMPLATE.format(name) for name in _SAFE_EXCEPTION_NAMES),
})


class BulkDownloadError(ValueError):
    """A safe explanation suitable for the administrator console."""


class _Cancelled(Exception):
    pass


def validate_date(value, label="Updated since"):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        if not isinstance(value, str):
            raise ValueError
        value = value.strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        parsed = date.fromisoformat(value)
        if parsed > datetime.now(timezone.utc).date():
            raise ValueError
    except (ValueError, TypeError):
        raise BulkDownloadError(f"{label} must be a valid YYYY-MM-DD date, no later than today.") from None
    return value


def validate_date_range(updated_since=None, updated_through=None):
    """Validate optional inclusive bounds before discovery or database writes."""
    start = validate_date(updated_since)
    end = validate_date(updated_through, "Updated through")
    if start and end and start > end:
        raise BulkDownloadError("Updated through must be on or after Updated since.")
    return start, end


def _wait(cancel, seconds):
    if cancel.wait(seconds):
        raise _Cancelled


def _retry_delay(response, attempt):
    value = response.headers.get("Retry-After", "") if response is not None else ""
    try:
        delay = float(value)
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            delay = (stamp - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            delay = 2 ** attempt
    return max(REQUEST_INTERVAL, min(120, max(0, delay)))


def _fetch_page(client, params, cancel, emit):
    for attempt in range(4):
        _wait(cancel, 0)
        response = None
        try:
            response = client.get(registry.API, params=params)
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                emit("Registry temporarily unavailable; retrying this page…")
                _wait(cancel, _retry_delay(response, attempt))
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("studies"), list):
                raise BulkDownloadError("The registry returned an invalid page. The last saved page is retained.")
            token = payload.get("nextPageToken")
            if token is not None and (not isinstance(token, str) or not token):
                raise BulkDownloadError("The registry returned an invalid page cursor. Start a new bulk download.")
            return payload
        except httpx.TransportError:
            if attempt == 3:
                raise
            emit("Registry connection interrupted; retrying this page…")
            _wait(cancel, _retry_delay(response, attempt))


def _scope(db, company_ids, *, include_inactive=False, warnings=None, emit=None):
    if company_ids is not None:
        ids = list(dict.fromkeys(company_ids))
        if not ids:
            raise BulkDownloadError("Select at least one company.")
        if any(type(value) is not int for value in ids):
            raise BulkDownloadError("Choose valid company IDs.")
        companies = db.scalars(select(Company).where(Company.id.in_(ids)).order_by(Company.id)).all()
        if len(companies) != len(ids):
            raise BulkDownloadError("A selected company no longer exists. Refresh the view.")
    else:
        companies = db.scalars(select(Company).order_by(Company.id)).all()
    if not include_inactive:
        active = [company for company in companies if company.coverage_status == "active"]
        if company_ids is not None and len(active) != len(companies):
            if warnings is not None:
                warnings.append(registry.INACTIVE_SELECTION_WARNING)
            if emit is not None:
                emit(registry.INACTIVE_SELECTION_WARNING)
            if not active:
                raise BulkDownloadError("No selected companies are active for collection. Their saved trial history is retained.")
        companies = active
    if not companies:
        raise BulkDownloadError("No companies are in coverage. Initialize the company directory first.")
    keys = set()
    for company in companies:
        key = registry.sponsor_key(company.sponsor or "")
        if not key or key in keys:
            raise BulkDownloadError("Company lead sponsors must be nonempty and unambiguous before a bulk download.")
        keys.add(key)
    return [{"id": company.id, "sponsor": company.sponsor} for company in companies]


def _params(scope, state):
    # Quote sponsor names so punctuation cannot become query operators.
    terms = ["(COVERAGE[Contains]" + json.dumps(row["sponsor"], ensure_ascii=False) + ")" for row in scope]
    term = "AREA[StudyType]INTERVENTIONAL" if state["oncology_only"] else ""
    start, end = state["updated_since"], state.get("updated_through")
    if start or end:
        term += (" AND " if term else "") + f"AREA[LastUpdatePostDate]RANGE[{start or 'MIN'},{end or 'MAX'}]"
    params = {
        "query.spons": " OR ".join(terms), "format": "json", "pageSize": PAGE_SIZE,
        "sort": "LastUpdatePostDate:asc", "countTotal": "true",
    }
    if term:
        params["query.term"] = term
    if state["oncology_only"]:
        params["query.cond"] = ONCOLOGY_QUERY
    if state["next_page_token"]:
        params["pageToken"] = state["next_page_token"]
    return params


def safe_error_messages(errors):
    """Expose only known application messages, including errors saved by older versions."""
    if errors is None:
        return []
    if not isinstance(errors, list):
        return [UNKNOWN_BULK_ERROR]
    result = []
    for message in errors:
        if isinstance(message, str) and message == _LEGACY_ERROR_TEMPLATE.format("DataError"):
            result.append(DATABASE_DATA_ERROR)
        elif isinstance(message, str) and message in _SAFE_ERROR_MESSAGES:
            result.append(message)
        else:
            result.append(UNKNOWN_BULK_ERROR)
    return result


def _error(exc):
    if isinstance(exc, BulkDownloadError):
        return safe_error_messages([str(exc)])[0]
    if isinstance(exc, DataError):
        # Inspect only the driver's numeric code, never its message, SQL, or parameters.
        code = exc.orig.args[0] if getattr(exc.orig, "args", ()) else None
        return DATABASE_STORAGE_ERROR if type(code) is int and code == 1406 else DATABASE_DATA_ERROR
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 400:
        return "The registry rejected the query or saved cursor. Start a new download if resuming fails."
    return safe_error_messages([_LEGACY_ERROR_TEMPLATE.format(type(exc).__name__)])[0]


def _result(job, state):
    result = {"job_id": job.id, "status": job.status, **{key: state[key] for key in COUNTERS}, "errors": state["errors"], "warnings": registry.safe_coverage_warnings(state.get("warnings"))}
    if "coverage" in state:
        result["coverage"] = state["coverage"]
    return result


def _summary(state):
    summary = {key: state[key] for key in (*COUNTERS, "kind", "updated_since", "oncology_only", "errors")}
    summary["updated_through"] = state.get("updated_through")
    summary["warnings"] = registry.safe_coverage_warnings(state.get("warnings"))
    if "coverage" in state:
        summary["coverage"] = state["coverage"]
    return registry.canonical(summary)


def run_bulk_download(company_ids=None, updated_since=None, emit=None, cancel=None, resume_job_id=None, oncology_only=False, updated_through=None, refresh_coverage=False):
    """Collect all conditions and study types by default.

    Keep the optional narrow scope for callers and historical checkpoints:
    resuming must reuse the original filters because cursors are query-specific.
    Administrator callers refresh coverage for new runs while holding this lock.
    Low-level callers opt in explicitly; a resume never refreshes its saved scope.
    """
    emit = emit or (lambda message: None)
    cancel = cancel or threading.Event()
    if resume_job_id is None:
        updated_since, updated_through = validate_date_range(updated_since, updated_through)
    with registry.sync_lock() as acquired:
        if not acquired:
            return {"status": "busy", "job_id": None, **dict.fromkeys(COUNTERS, 0), "errors": []}
        coverage, warnings = (
            registry.refresh_company_coverage(emit)
            if refresh_coverage and resume_job_id is None else (None, [])
        )
        with Session() as db:
            if resume_job_id is not None:
                job = db.get(Job, resume_job_id)
                saved = db.get(BulkCheckpoint, resume_job_id)
                try:
                    state = json.loads(saved.payload) if saved else {}
                except (ValueError, TypeError):
                    state = {}
                if not job or state.get("kind") != "bulk" or job.status not in ("partial", "cancelled", "interrupted", "running"):
                    raise BulkDownloadError("Select an unfinished bulk download to resume.")
                # Older cursors were issued with an open upper bound. Preserve it.
                state.setdefault("updated_through", None)
                saved_scope = state["company_scope"]
                # A checkpoint retains the original scope, including companies
                # that were retired by a later coverage refresh.
                current = _scope(db, [row["id"] for row in saved_scope], include_inactive=True)
                if current != saved_scope:
                    raise BulkDownloadError("Company coverage changed since this run. Start a new bulk download.")
                state["errors"] = []
            else:
                company_scope = _scope(db, company_ids, warnings=warnings, emit=emit)
                state = {
                    "kind": "bulk", "version": 1, "company_scope": company_scope,
                    "oncology_only": bool(oncology_only),
                    "updated_since": updated_since, "updated_through": updated_through, "batch_index": 0,
                    "next_page_token": None, "token_hashes": [], "errors": [], "warnings": warnings,
                    **dict.fromkeys(COUNTERS, 0),
                }
                if coverage is not None:
                    state["coverage"] = coverage
                job = Job()
                db.add(job)
                db.flush()
                saved = BulkCheckpoint(job_id=job.id, payload=registry.canonical(state))
                db.add(saved)
            registry.interrupt_running_jobs(db, except_job_id=job.id)
            saved.payload = registry.canonical(state)
            job.status, job.finished_at, job.summary = "running", None, _summary(state)
            db.commit()
            emit(f"Bulk run #{job.id}: {len(state['company_scope'])} companies; registry update dates {state['updated_since'] or 'any date'} through {state['updated_through'] or 'present'} (inclusive).")
            batches = [state["company_scope"][i:i + BATCH_SIZE] for i in range(0, len(state["company_scope"]), BATCH_SIZE)]
            try:
                with httpx.Client(timeout=httpx.Timeout(45, connect=10)) as client:
                    while state["batch_index"] < len(batches):
                        _wait(cancel, 0)
                        scope = batches[state["batch_index"]]
                        payload = _fetch_page(client, _params(scope, state), cancel, emit)
                        _wait(cancel, 0)
                        token = payload.get("nextPageToken")
                        token_hash = hashlib.sha256(token.encode()).hexdigest() if token else None
                        if token_hash and token_hash in state["token_hashes"]:
                            raise BulkDownloadError("The registry repeated a page cursor. Start a new bulk download.")
                        # Validate the entire page before mutating it, including skipped records.
                        records = {}
                        for raw in payload["studies"]:
                            normalized = registry.normalize(raw)
                            records[normalized["nct_id"]] = (raw, normalized)
                        companies = {row["id"]: db.get(Company, row["id"]) for row in scope}
                        by_sponsor = {registry.sponsor_key(row["sponsor"]): companies[row["id"]] for row in scope}
                        checkpoint = copy.deepcopy(state)
                        checkpoint["pages"] += 1
                        checkpoint["scanned"] += len(payload["studies"])
                        for study_id, (raw, normalized) in records.items():
                            company = by_sponsor.get(registry.sponsor_key(normalized["sponsor"]))
                            existing = db.get(Study, study_id)
                            if company is None or (existing and existing.company_id != company.id):
                                checkpoint["skipped"] += 1
                                continue
                            checkpoint["changes"] += registry.ingest(db, company, raw)
                            checkpoint["studies"] += 1
                        checkpoint["next_page_token"] = token
                        if token:
                            checkpoint["token_hashes"].append(token_hash)
                        else:
                            checkpoint["batch_index"] += 1
                            checkpoint["companies"] += len(scope)
                            checkpoint["token_hashes"] = []
                            # A filtered refresh is not proof of complete company coverage.
                            if state["updated_since"] is None and state["updated_through"] is None and not state["oncology_only"]:
                                for company in companies.values():
                                    company.last_checked, company.last_error = now(), None
                        complete = checkpoint["batch_index"] == len(batches)
                        job.summary = _summary(checkpoint)
                        saved.payload = registry.canonical(checkpoint)
                        if complete:
                            job.status, job.finished_at = "complete", now()
                        db.commit()
                        state = checkpoint
                        emit(f"Page {state['pages']} saved (batch {min(state['batch_index'] + 1, len(batches))}/{len(batches)}): {state['scanned']} scanned, {state['studies']} trials processed, {state['skipped']} unmatched, {state['changes']} differences.")
                        if not complete:
                            _wait(cancel, REQUEST_INTERVAL)
            except Exception as exc:
                db.rollback()
                # Read the last committed checkpoint, never a half-processed page.
                db.refresh(job)
                db.refresh(saved)
                state = json.loads(saved.payload)
                job.status = "cancelled" if isinstance(exc, _Cancelled) else "partial"
                if not isinstance(exc, _Cancelled):
                    state["errors"] = [_error(exc)]
                    emit(state["errors"][0])
                job.finished_at, job.summary = now(), _summary(state)
                saved.payload = registry.canonical(state)
                db.commit()
            emit(f"Bulk run #{job.id}: {job.status}. {state['pages']} pages saved.")
            return _result(job, state)
