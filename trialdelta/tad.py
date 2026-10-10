"""Deterministic filing dates and explicit administrator target action dates.

Only SELECT is issued against filings.biotech_filings. The administrator import
and the filing stream share per-filing persistence; importing this module or
reading the feed does not collect, download, or change any source data.
"""

import hashlib
import json
import os
import re
from datetime import date, datetime
from urllib.parse import urlsplit

from sqlalchemy import DateTime, and_, case, create_engine, delete, func, literal, or_, select, text, union_all
from sqlalchemy.exc import IntegrityError

from .db import Company, Session, TargetActionDate, TargetActionDateEdit, engine
from .tad_parser import extract_target_action_dates


SOURCE_QUERY = text(
    "SELECT symbol, form_type, filing, accession_no, filed_at, filing_link "
    "FROM filings.biotech_filings"
).columns(filed_at=DateTime())
RESULT_FIELDS = (
    "tad", "precision", "status", "matched_text", "reason", "start", "end",
    "context_start", "context_end", "context",
)
MAX_PAGE_SIZE = 500
MAX_MANUAL_NOTES = 4000


class TADValidationError(ValueError):
    """A safe, operator-facing correction for an invalid date operation."""


def _manual_values(ticker, tad, source_url, notes):
    if not isinstance(ticker, str):
        raise TADValidationError("Enter a ticker symbol.")
    symbol = ticker.strip().upper()
    if len(symbol) > 16 or not re.fullmatch(r"[A-Z0-9]+(?:[.-][A-Z0-9]+)*", symbol):
        raise TADValidationError("Enter a ticker of up to 16 letters or numbers, optionally separated by dots or hyphens.")
    if not isinstance(tad, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}(?:-[0-9]{2})?", tad.strip()):
        raise TADValidationError("Enter the date as YYYY-MM-DD or YYYY-MM when the day is unknown.")
    tad = tad.strip()
    try:
        # Adding a day is only for calendar validation; month entries retain
        # their exact YYYY-MM value and never acquire a day in storage.
        date.fromisoformat(tad if len(tad) == 10 else tad + "-01")
    except ValueError:
        raise TADValidationError("Enter a valid calendar date or month.") from None
    if not isinstance(source_url, str):
        raise TADValidationError("Enter an HTTP or HTTPS source URL, or leave it empty.")
    source_url = source_url.strip()
    if source_url:
        try:
            parsed = urlsplit(source_url)
            valid = (
                len(source_url) <= 2048 and parsed.scheme in ("http", "https")
                and bool(parsed.hostname) and parsed.username is None and parsed.password is None
                and not re.search(r"[\s\\\x00-\x1f\x7f]", source_url)
            )
            parsed.port  # Reject malformed and out-of-range ports as well.
        except ValueError:
            valid = False
        if not valid:
            raise TADValidationError("Use an HTTP or HTTPS source URL of up to 2,048 characters without credentials or spaces.")
        source_url = parsed.geturl()
    if not isinstance(notes, str) or len(notes.strip()) > MAX_MANUAL_NOTES:
        raise TADValidationError("Keep notes within 4,000 characters.")
    return symbol, tad, source_url, notes.strip()


def add_manual_target_action_date(db, *, ticker, tad, source_url="", notes="", entered_by):
    """Save an identified administrator entry in the caller's transaction.

    This does not read filings, invoke extraction, or replace imported evidence.
    Repeating the same normalized fields returns the existing record, retaining
    its original operator and timestamp. Authorization belongs to admin_ops.
    """
    symbol, tad, source_url, notes = _manual_values(ticker, tad, source_url, notes)
    if not isinstance(entered_by, int) or isinstance(entered_by, bool) or entered_by <= 0:
        raise TADValidationError("A signed-in administrator is required to enter a date.")
    destination = db.get_bind()
    if destination.dialect.name == "mysql" and destination.url.database != "trialdelta":
        raise RuntimeError("Target action date writes require the trialdelta schema.")
    identity = hashlib.sha256(json.dumps(
        ["manual", symbol, tad, source_url, notes], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    precision = "day" if len(tad) == 10 else "month"
    values = {
        "identity": identity, "entry_method": "manual", "entered_by": entered_by,
        "symbol": symbol, "tad": tad, "precision": precision, "status": "identified",
        "form_type": "", "filing_sha256": identity, "filing_link": source_url or None,
        "matched_text": "", "context": notes, "reason": "Manually entered by an administrator.",
        "start": 0, "end": 0, "context_start": 0, "context_end": 0,
    }
    if destination.dialect.name == "sqlite":
        connection = db.connection()
        if not connection.connection.driver_connection.in_transaction:
            # Keep releasing the insert savepoint from committing the entry
            # ahead of the caller's commit or rollback (legacy sqlite3 mode).
            connection.exec_driver_sql("BEGIN")
    inserted = _save_result(db, values)
    # A current read also sees a competing insertion recovered by _save_result
    # under MySQL REPEATABLE READ, rather than the earlier lookup's snapshot.
    row_id = db.scalar(select(TargetActionDate.id).where(
        TargetActionDate.identity == identity
    ).with_for_update())
    correction = db.scalar(select(TargetActionDateEdit).where(
        TargetActionDateEdit.target_identity == identity
    ).order_by(TargetActionDateEdit.id.desc()).limit(1).with_for_update())
    return {
        "status": "saved" if inserted else "existing", "id": row_id,
        "ticker": correction.symbol if correction is not None else symbol,
        "tad": correction.tad if correction is not None else tad,
        "precision": correction.precision if correction is not None else precision,
        **({"is_edited": True} if correction is not None else {}),
    }


def _source_engine():
    source_url = os.getenv("SOURCE_DATABASE_URL")
    if source_url:
        return create_engine(source_url, pool_pre_ping=True)
    if os.getenv("USE_LOCAL_MYSQL", "false").lower() == "true":
        return engine
    return None


def _locked_target_action_date(db, item_id, expected_identity):
    """Read the current saved row and latest correction, checking the snapshot."""
    if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id <= 0:
        raise TADValidationError("Select a saved target action date.")
    if not isinstance(expected_identity, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_identity):
        raise TADValidationError("The selected target action date has changed. Refresh the list.")
    destination = db.get_bind()
    if destination.dialect.name == "mysql" and destination.url.database != "trialdelta":
        raise RuntimeError("Target action date writes require the trialdelta schema.")
    if destination.dialect.name == "sqlite":
        connection = db.connection()
        if not connection.connection.driver_connection.in_transaction:
            # SELECT FOR UPDATE is unavailable in SQLite. Reserve its writer
            # before selecting so another writer cannot change the chosen row.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    row = db.scalar(select(TargetActionDate).where(
        TargetActionDate.id == item_id
    ).with_for_update().execution_options(populate_existing=True))
    edit = None if row is None else db.scalar(select(TargetActionDateEdit).where(
        TargetActionDateEdit.target_identity == row.identity
    ).order_by(TargetActionDateEdit.id.desc()).limit(1).with_for_update())
    revision = edit.revision_token if edit is not None else row.identity if row is not None else None
    if row is None or revision != expected_identity:
        # The token changes for each correction and cannot identify a different
        # record if SQLite reuses a deleted integer ID. Locking reads see edits
        # committed after a MySQL REPEATABLE READ snapshot was first opened.
        raise TADValidationError("The selected target action date no longer exists or has changed. Refresh the list.")
    return row, edit


def _effective_values(row, edit):
    return {
        "ticker": edit.symbol if edit is not None else row.symbol,
        "tad": edit.tad if edit is not None else row.tad,
        "precision": edit.precision if edit is not None else row.precision,
        "status": "identified" if edit is not None else row.status,
        "source_url": edit.source_url if edit is not None else row.filing_link or "",
        "notes": edit.notes if edit is not None else row.context,
    }


def _locked_effective_group(db, row, edit):
    """Current-read and lock each original represented by the effective group.

    Correction history supplies candidate identities, not final membership.
    Reading those candidates and then their latest revisions with locking reads
    avoids grouping by an older MySQL snapshot. All returned base rows remain
    locked until the caller commits, serializing subsequent edits to them.
    """
    selected = _effective_values(row, edit)
    if selected["status"] != "identified":
        return [(row, edit)]
    edited_identities = set(db.scalars(select(TargetActionDateEdit.target_identity).where(
        TargetActionDateEdit.symbol == selected["ticker"],
        TargetActionDateEdit.tad == selected["tad"],
        TargetActionDateEdit.precision == selected["precision"],
    ).with_for_update()))
    original_group = and_(
        TargetActionDate.symbol == selected["ticker"], TargetActionDate.tad == selected["tad"],
        TargetActionDate.precision == selected["precision"], TargetActionDate.status == "identified",
    )
    candidates = list(db.scalars(select(TargetActionDate).where(or_(
        original_group, TargetActionDate.identity.in_(edited_identities),
    )).order_by(TargetActionDate.id).with_for_update().execution_options(populate_existing=True)))
    latest = {}
    if candidates:
        for correction in db.scalars(select(TargetActionDateEdit).where(
            TargetActionDateEdit.target_identity.in_([candidate.identity for candidate in candidates])
        ).order_by(TargetActionDateEdit.id.desc()).with_for_update()):
            latest.setdefault(correction.target_identity, correction)
    members = []
    for candidate in candidates:
        correction = latest.get(candidate.identity)
        values = _effective_values(candidate, correction)
        if all(values[key] == selected[key] for key in ("ticker", "tad", "precision", "status")):
            members.append((candidate, correction))
    return members


def _comparable_edit_values(ticker, tad, source_url, notes):
    """Normalize displayed text without imposing manual-entry restrictions."""
    if not all(isinstance(value, str) for value in (ticker, tad, source_url, notes)):
        return None
    source_url = source_url.strip()
    try:
        source_url = urlsplit(source_url).geturl()
    except ValueError:
        pass
    return ticker.strip().upper(), tad.strip(), source_url, notes.strip()


def edit_target_action_date(db, item_id, *, expected_identity, ticker, tad, source_url="", notes="", entered_by):
    """Append corrections for a displayed row; never modify original evidence.

    Identified selections correct every original represented by their effective
    ticker/date/precision group; unresolved selections correct only one match.
    Unchanged displayed values do not create history. A savepoint makes multiple
    corrections atomic while the caller retains the outer commit or rollback.
    """
    if not isinstance(entered_by, int) or isinstance(entered_by, bool) or entered_by <= 0:
        raise TADValidationError("A signed-in administrator is required to enter a date.")
    row, previous_edit = _locked_target_action_date(db, item_id, expected_identity)
    current = _effective_values(row, previous_edit)
    supplied = _comparable_edit_values(ticker, tad, source_url, notes)
    if supplied is not None and supplied == _comparable_edit_values(
        current["ticker"], current["tad"] or "", current["source_url"], current["notes"],
    ):
        return {
            "status": "unchanged", "id": row.id, "ticker": current["ticker"],
            "tad": current["tad"], "precision": current["precision"], "updated": 0,
        }
    symbol, tad, source_url, notes = _manual_values(ticker, tad, source_url, notes)
    precision = "day" if len(tad) == 10 else "month"
    members = _locked_effective_group(db, row, previous_edit)
    if not members:
        raise TADValidationError("The selected target action date has changed. Refresh the list.")
    # The locked read establishes a real SQLite transaction before this
    # savepoint. A partial append failure restores all history from this edit,
    # even if the caller catches the failure and commits other work afterward.
    with db.begin_nested():
        for original, latest in members:
            previous_revision = latest.revision_token if latest is not None else original.identity
            revision = hashlib.sha256(json.dumps(
                [original.identity, previous_revision, symbol, tad, source_url, notes, entered_by],
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            db.add(TargetActionDateEdit(
                target_identity=original.identity, revision_token=revision, symbol=symbol,
                tad=tad, precision=precision, source_url=source_url, notes=notes, entered_by=entered_by,
            ))
        db.flush()
    return {
        "status": "updated", "id": row.id, "ticker": symbol,
        "tad": tad, "precision": precision, "updated": len(members),
    }


def delete_target_action_date(db, item_id, *, expected_identity):
    """Explicitly delete originals represented by one effective calendar row.

    This separate administrator action retains append-only correction history
    and never changes source filings. A later import of the same evidence can
    restore that evidence with its latest saved correction.
    """
    row, edit = _locked_target_action_date(db, item_id, expected_identity)
    values = _effective_values(row, edit)
    member_ids = [original.id for original, _ in _locked_effective_group(db, row, edit)]
    deleted = db.execute(delete(TargetActionDate).where(TargetActionDate.id.in_(member_ids)).execution_options(
        synchronize_session="fetch"
    )).rowcount
    if not deleted:
        raise TADValidationError("The selected target action date no longer exists. Refresh the list.")
    return {
        "status": "deleted", "ticker": values["ticker"], "tad": values["tad"],
        "precision": values["precision"], "deleted": deleted,
    }


def _identity(symbol, form_type, filing_hash, accession_no, occurrence, result):
    # Every match keeps its evidence. Identical source rows and subsequent runs
    # converge on the same identities, without requiring a source primary key.
    parts = [symbol, form_type, filing_hash, accession_no, occurrence] + [
        result[key] for key in ("start", "end", "tad", "precision", "status")
    ]
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _save_result(db, values):
    exists = select(TargetActionDate.id).where(
        TargetActionDate.identity == values["identity"]
    )
    if db.scalar(exists) is not None:
        return False
    try:
        # A unique key plus savepoint handles a concurrent importer that inserts
        # the same match after the existence check, without losing this run.
        with db.begin_nested():
            db.add(TargetActionDate(**values))
            db.flush()
    except IntegrityError:
        # A locking read sees a racing commit even under MySQL's default
        # REPEATABLE READ isolation (the earlier lookup may have a snapshot).
        if db.scalar(exists.with_for_update()) is not None:
            return False
        raise
    return True


class FilingTargetActionDateError(RuntimeError):
    """A failed filing save with scan counts available to the batch importer."""

    def __init__(self, counts):
        super().__init__("Target action date persistence failed for a stored filing.")
        self.counts = dict(counts)


def save_filing_target_action_dates(db, row):
    """Extract and save one stored filing in the caller's transaction.

    ``row`` contains symbol, form_type, filing, accession_no, filed_at, and
    filing_link. ``filed_at`` may be a native date/datetime, an ISO datetime
    string from a durable job, or None. No source connection, network request,
    commit, or rollback occurs here. The caller must roll back after failure.

    All marker evidence is saved, including ambiguous and unidentifiable
    mentions, so administrators can supply a date later. Filings without a
    marker return zero counts. Identities match the full import and preserve
    existing administrator corrections when either path repeats a filing.
    """
    destination = db.get_bind()
    if destination.dialect.name == "mysql" and destination.url.database != "trialdelta":
        raise RuntimeError("Target action date writes require the trialdelta schema.")
    markup = row["filing"] or ""
    symbol = (row["symbol"] or "").strip().upper()
    form_type = (row["form_type"] or "").strip().upper()
    accession_no = (row["accession_no"] or "").strip() or None
    filed_at = row["filed_at"]
    if isinstance(filed_at, str):
        filed_at = datetime.fromisoformat(filed_at.strip()).isoformat()
    elif isinstance(filed_at, (date, datetime)):
        filed_at = filed_at.isoformat()
    elif filed_at is not None:
        raise ValueError("Source filing timestamp must be an ISO timestamp or datetime.")
    filing_link = row["filing_link"] or None
    # Never silently truncate source identifiers.
    if len(symbol) > 255 or len(form_type) > 64:
        raise ValueError("Source identifier exceeds result storage length.")
    filing_hash = hashlib.sha256(markup.encode("utf-8")).hexdigest()
    results = extract_target_action_dates(markup)
    if destination.dialect.name == "sqlite" and results:
        connection = db.connection()
        if not connection.connection.driver_connection.in_transaction:
            # sqlite3's legacy mode otherwise allows RELEASE SAVEPOINT to
            # commit before the caller commits its transaction or job status.
            connection.exec_driver_sql("BEGIN")
    counts = {
        "matched": 0, "identified": 0, "month": 0,
        "unresolved": 0, "imported": 0, "existing": 0,
    }
    try:
        for occurrence, result in enumerate(results):
            counts["matched"] += 1
            if result["status"] == "identified":
                counts["identified"] += 1
                counts["month"] += int(result["precision"] == "month")
            else:
                counts["unresolved"] += 1
            values = {key: result[key] for key in RESULT_FIELDS}
            values.update(
                symbol=symbol, form_type=form_type, filing_sha256=filing_hash,
                accession_no=accession_no, filed_at=filed_at, filing_link=filing_link,
                identity=_identity(symbol, form_type, filing_hash, accession_no, occurrence, result),
            )
            key = "imported" if _save_result(db, values) else "existing"
            counts[key] += 1
    except Exception as exc:
        # Preserve the full import's partial scan counters on a save failure.
        # The fixed message is safe to show; the chained cause is diagnostic.
        raise FilingTargetActionDateError(counts) from exc
    return counts


def import_target_action_dates():
    """Stream all saved filings and atomically save new extraction results.

    A month-only date is an identified result, not an error. Counters describe
    this scan (including duplicate source rows); ``imported`` counts new stored
    results and ``existing`` counts repeated matches. A failed scan rolls back
    every new result while leaving earlier successful imports intact.
    """
    summary = {
        "status": "complete", "scanned": 0, "matched": 0, "identified": 0,
        "month": 0, "unresolved": 0, "imported": 0, "existing": 0,
    }
    source_engine = None
    try:
        source_engine = _source_engine()
        if source_engine is None:
            return {
                **summary, "status": "unconfigured",
                "detail": "Set SOURCE_DATABASE_URL or USE_LOCAL_MYSQL to read saved filings.",
            }
        with Session() as db:
            destination = db.get_bind()
            if destination.dialect.name == "mysql" and destination.url.database != "trialdelta":
                raise RuntimeError("Target action date writes require the trialdelta schema.")
            with db.begin():
                if destination.dialect.name == "sqlite":
                    # sqlite3's legacy transaction mode does not begin a real
                    # transaction for SELECT/SAVEPOINT; ensure releasing an
                    # insert savepoint cannot commit before the scan finishes.
                    db.connection().exec_driver_sql("BEGIN")
                with source_engine.connect() as connection:
                    # Do not filter raw HTML with LIKE: tags can divide the
                    # exact marker. A server-side cursor bounds memory to one
                    # filing, including when the source has no paging key.
                    rows = connection.execution_options(yield_per=1).execute(
                        SOURCE_QUERY
                    ).mappings()
                    try:
                        for row in rows:
                            summary["scanned"] += 1
                            try:
                                counts = save_filing_target_action_dates(db, row)
                            except FilingTargetActionDateError as exc:
                                for key, value in exc.counts.items():
                                    summary[key] += value
                                raise
                            for key, value in counts.items():
                                summary[key] += value
                    finally:
                        rows.close()
    except Exception:
        # Driver exceptions can include credentials, query arguments, and full
        # source documents. Return only a fixed operator-facing explanation.
        summary.update(
            status="error", imported=0,
            detail="Target action date import failed. Saved results were retained; check database/source availability and configuration.",
        )
    finally:
        if source_engine is not None and source_engine is not engine:
            source_engine.dispose()
    return summary


def _effective_tad_rows():
    """SQL projection used before grouping, filtering, counts, and pagination."""
    latest = select(
        TargetActionDateEdit.target_identity,
        func.max(TargetActionDateEdit.id).label("edit_id"),
    ).group_by(TargetActionDateEdit.target_identity).subquery()
    return select(
        TargetActionDate.id, TargetActionDate.identity,
        func.coalesce(TargetActionDateEdit.symbol, TargetActionDate.symbol).label("symbol"),
        func.coalesce(TargetActionDateEdit.tad, TargetActionDate.tad).label("tad"),
        func.coalesce(TargetActionDateEdit.precision, TargetActionDate.precision).label("precision"),
        case((TargetActionDateEdit.id.is_not(None), "identified"), else_=TargetActionDate.status).label("status"),
        TargetActionDate.entry_method, TargetActionDate.accession_no, TargetActionDate.filing_sha256,
        TargetActionDateEdit.id.label("edit_id"),
    ).select_from(TargetActionDate).outerjoin(
        latest, latest.c.target_identity == TargetActionDate.identity,
    ).outerjoin(TargetActionDateEdit, TargetActionDateEdit.id == latest.c.edit_id).subquery()


def list_target_action_dates(db, *, ticker="", status="", q="", month="", limit=100, offset=0, include_identity=False):
    """Read a bounded calendar page and its counts, without accessing filings.

    Repeated identified symbol/date pairs appear once with source and occurrence
    counts. One representative saved context accompanies each pair. Unresolved
    matches stay separate so each can be inspected. No current/superseded state
    is inferred from import time, which is not a filing date.
    """
    limit = max(1, min(MAX_PAGE_SIZE, int(limit)))
    offset = max(0, int(offset))
    effective = _effective_tad_rows()
    conditions = []
    if ticker:
        conditions.append(effective.c.symbol == ticker.strip().upper())
    if status == "unresolved":
        conditions.append(effective.c.status != "identified")
    elif status in ("identified", "ambiguous", "unidentifiable"):
        conditions.append(effective.c.status == status)
    elif status:
        raise ValueError("Unknown target action date status.")
    if month:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("Use YYYY-MM for the calendar month.")
        try:
            date.fromisoformat(month + "-01")
        except ValueError:
            raise ValueError("Use a valid calendar month.") from None
        conditions.append(or_(
            and_(effective.c.precision == "month", effective.c.tad == month),
            and_(effective.c.precision == "day", effective.c.tad.startswith(month + "-")),
        ))

    filing_identity = case(
        (effective.c.entry_method == "manual", None),
        (effective.c.accession_no.is_not(None), literal("accession:") + effective.c.accession_no),
        else_=literal("hash:") + effective.c.filing_sha256,
    )
    identified = (
        select(
            func.min(effective.c.id).label("id"),
            effective.c.symbol, effective.c.tad,
            effective.c.status, effective.c.precision,
            func.count(func.distinct(filing_identity)).label("source_count"),
            func.count().label("occurrence_count"),
            func.sum(case((effective.c.entry_method == "manual", 1), else_=0)).label("manual_count"),
            func.sum(case((effective.c.edit_id.is_not(None), 1), else_=0)).label("edited_count"),
        )
        .where(*conditions, effective.c.status == "identified")
        .group_by(
            effective.c.symbol, effective.c.tad,
            effective.c.status, effective.c.precision,
        )
    )
    unresolved = select(
        effective.c.id, effective.c.symbol, effective.c.tad,
        effective.c.status, effective.c.precision,
        literal(1).label("source_count"), literal(1).label("occurrence_count"),
        literal(0).label("manual_count"),
        literal(0).label("edited_count"),
    ).where(*conditions, effective.c.status != "identified")
    grouped = union_all(identified, unresolved).subquery()
    query = (
        select(TargetActionDate, TargetActionDateEdit, grouped.c.source_count, grouped.c.occurrence_count,
               grouped.c.manual_count, grouped.c.edited_count, Company.name)
        .select_from(TargetActionDate)
        .join(grouped, grouped.c.id == TargetActionDate.id)
        .join(effective, effective.c.id == TargetActionDate.id)
        .outerjoin(TargetActionDateEdit, TargetActionDateEdit.id == effective.c.edit_id)
        .outerjoin(Company, Company.ticker == effective.c.symbol)
    )
    search = q.strip().lower()
    if search:
        effective_context = case((TargetActionDateEdit.id.is_not(None), TargetActionDateEdit.notes),
                                 else_=TargetActionDate.context)
        query = query.where(or_(*[
            func.lower(func.coalesce(column, "")).contains(search, autoescape=True)
            for column in (effective.c.symbol, effective.c.tad, effective.c.status, Company.name,
                           TargetActionDate.accession_no, effective_context, TargetActionDate.context,
                           TargetActionDate.matched_text, TargetActionDate.reason)
        ]))
    filtered = query.with_only_columns(grouped.c.status, grouped.c.precision).subquery()
    counts = db.execute(select(
        func.count().label("total"),
        func.sum(case((filtered.c.status == "identified", 1), else_=0)).label("identified"),
        func.sum(case((filtered.c.precision == "month", 1), else_=0)).label("month"),
        func.sum(case((filtered.c.status != "identified", 1), else_=0)).label("unresolved"),
    ).select_from(filtered)).mappings().one()
    query = query.order_by(
            case((effective.c.status == "identified", 0), else_=1),
            effective.c.tad.desc(), effective.c.symbol, TargetActionDate.id,
        ).offset(offset).limit(limit)
    items = []
    for row, edit, source_count, occurrence_count, manual_count, edited_count, company_name in db.execute(query):
        values = _effective_values(row, edit)
        result_fields = {key: getattr(row, key) for key in RESULT_FIELDS}
        if edit is not None:
            result_fields.update(
                tad=edit.tad, precision=edit.precision, status="identified", matched_text="",
                reason="Manually corrected by an administrator; original evidence is retained.",
                context=edit.notes, start=0, end=0, context_start=0, context_end=0,
            )
        items.append({
            "id": row.id, "symbol": values["ticker"], "ticker": values["ticker"],
            "company": company_name or values["ticker"],
            "form_type": row.form_type,
            "filing_sha256": None if row.entry_method == "manual" else row.filing_sha256,
            "entry_method": "mixed" if source_count and manual_count else "manual" if manual_count else "filing",
            "evidence_method": "edited" if edit is not None else row.entry_method,
            "is_edited": edit is not None, "edited_count": edited_count,
            "edited_at": edit.edited_at if edit is not None else None,
            "original_tad": row.tad if edit is not None else None,
            "original_context": row.context if edit is not None else None,
            "original_filing_link": row.filing_link if edit is not None else None,
            "accession_no": row.accession_no, "filed_at": row.filed_at,
            "filing_link": values["source_url"] or None,
            "imported_at": row.imported_at, "source_count": source_count,
            "occurrence_count": occurrence_count,
            "manual_count": manual_count,
            **({"selection_identity": edit.revision_token if edit is not None else row.identity} if include_identity else {}),
            **result_fields,
        })
    return {
        "items": items, "limit": limit, "offset": offset,
        **{key: value or 0 for key, value in counts.items()},
    }


def main():
    """Explicit local administrator command; app initialization creates tables."""
    result = import_target_action_dates()
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
