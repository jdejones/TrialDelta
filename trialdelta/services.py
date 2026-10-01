import calendar
import json
import re
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import select, func, update
from fastapi import HTTPException
from .db import Company, Study, Snapshot, Evidence, Change, Follow, now

CATEGORIES = {
    "timeline",
    "development_status",
    "trial_design",
    "enrollment",
    "partnership",
    "milestone",
    "results",
    "registry_date",
}


def as_dict(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def period(text):
    """Intervals retain the supplied precision; boundaries are not catalyst forecasts."""
    value = text.strip().upper()
    m = re.fullmatch(r"(H[12]|Q[1-4])\s+(20\d{2})", value)
    if m:
        label, year = m[1], int(m[2])
        width = 6 if label[0] == "H" else 3
        start = (int(label[1]) - 1) * width + 1
        end = start + width - 1
        return (
            f"{year}-{start:02}-01",
            f"{year}-{end:02}-{calendar.monthrange(year,end)[1]}",
            "half-year" if width == 6 else "quarter",
        )
    if re.fullmatch(r"20\d{2}", value):
        return value + "-01-01", value + "-12-31", "year"
    if re.fullmatch(r"20\d{2}-\d{2}", value):
        year, month = map(int, value.split("-"))
        try:
            return (
                value + "-01",
                value + f"-{calendar.monthrange(year,month)[1]}",
                "month",
            )
        except (ValueError, calendar.IllegalMonthError):
            pass
    try:
        parsed = date.fromisoformat(value)
        return parsed.isoformat(), parsed.isoformat(), "day"
    except ValueError:
        return None, None, "uncertain"


def matches_follow(change, study, follows):
    for follow in follows:
        v = follow.value.casefold()
        if follow.kind == "company" and str(change.company_id) == v:
            return True
        if (
            follow.kind == "program"
            and change.study_id
            and change.study_id.casefold() == v
        ):
            return True
        if (
            study
            and follow.kind == "drug"
            and v in [x.strip().casefold() for x in study.drug.split(";")]
        ):
            return True
        if study and follow.kind in ("target", "indication"):
            values = json.loads(
                study.targets if follow.kind == "target" else study.conditions
            )
            if v in [x.casefold() for x in values]:
                return True
    return False


def change_dict(db, change):
    item = as_dict(change)
    company = db.get(Company, change.company_id)
    study = db.get(Study, change.study_id) if change.study_id else None
    item.update(
        company=company.name,
        ticker=company.ticker,
        drug=study.drug if study else "",
        study_title=study.title if study else "",
    )
    return item


def published_changes(db, user_id, followed=False, day=None):
    query = (
        select(Change)
        .where(Change.review_status == "approved")
        .order_by(Change.detected_at.desc(), Change.id.desc())
    )
    rows = db.scalars(query).all()
    if day:
        # Approval date makes delayed reviews enter the day's digest.
        rows = [r for r in rows if (r.reviewed_at or r.detected_at).startswith(day)]
    if followed:
        follows = db.scalars(select(Follow).where(Follow.user_id == user_id)).all()
        rows = [
            r
            for r in rows
            if matches_follow(
                r, db.get(Study, r.study_id) if r.study_id else None, follows
            )
        ]
    return [change_dict(db, r) for r in rows]


def approve_evidence(db, record, user_id):
    if record.review_status != "pending":
        raise HTTPException(409, "This evidence has already been reviewed")
    # Source chronology is mandatory for a truthful before/after. Older evidence remains a baseline.
    db.execute(select(Company).where(Company.id == record.company_id).with_for_update())
    existing = db.scalars(
        select(Evidence)
        .where(
            Evidence.company_id == record.company_id,
            Evidence.study_id == record.study_id,
            Evidence.category == record.category,
            Evidence.review_status == "approved",
        )
        .order_by(Evidence.published_at.desc(), Evidence.id.desc())
    ).all()
    if record.study_id and existing and record.published_at <= existing[0].published_at:
        raise HTTPException(
            409,
            "This source is not newer than the approved baseline. Add a newer source; historical backfill must not create a reversed timeline.",
        )
    # Company-level statements can refer to unrelated programs. Preserve them without automatic diffs.
    previous = existing[0] if existing and record.study_id else None
    claimed = db.execute(
        update(Evidence)
        .where(Evidence.id == record.id, Evidence.review_status == "pending")
        .values(review_status="approved", reviewed_by=user_id, reviewed_at=now())
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "This evidence has already been reviewed")
    db.refresh(record)
    if previous and previous.value.strip() != record.value.strip():
        db.add(
            Change(
                company_id=record.company_id,
                study_id=record.study_id,
                category=record.category,
                title={
                    "timeline": "Company readout guidance changed",
                    "partnership": "Partnership statement changed",
                    "milestone": "Expected milestone changed",
                }.get(record.category, "Company statement changed"),
                before_value=previous.value,
                after_value=record.value,
                source="company",
                before_evidence=previous.id,
                after_evidence=record.id,
                reviewed=True,
                review_status="approved",
                reviewed_by=user_id,
                reviewed_at=now(),
            )
        )
    db.commit()
    return {
        "status": "approved",
        "change_created": bool(
            previous and previous.value.strip() != record.value.strip()
        ),
    }


def reconciliation(db, study_id):
    if not study_id:
        return {
            "status": "unlinked",
            "message": "No trial linked. Registry comparison unavailable.",
        }
    study = db.get(Study, study_id)
    count = db.scalar(
        select(func.count()).select_from(Snapshot).where(Snapshot.study_id == study_id)
    )
    latest = db.scalar(
        select(Snapshot)
        .where(Snapshot.study_id == study_id)
        .order_by(Snapshot.id.desc())
        .limit(1)
    )
    n = json.loads(latest.normalized) if latest else {}
    fresh = study.last_checked and datetime.fromisoformat(
        study.last_checked
    ) > datetime.now(timezone.utc) - timedelta(hours=48)
    status = "current" if fresh else "stale"
    message = (
        "Registry baseline recorded; no prior collected version to compare."
        if count <= 1
        else "Registry versions are available for comparison. Check the field-level change log."
    )
    if not fresh:
        message = "Registry check is stale or unavailable. Refresh before reconciling."
    company = db.get(Company, study.company_id)
    if company.last_error:
        status = "error"
        message = "The latest registry refresh failed. The last successful snapshot is preserved; refresh before reconciling."
    return {
        "status": status,
        "message": message,
        "last_checked": study.last_checked,
        "primary_completion": n.get("primary_completion"),
        "completion": n.get("completion"),
        "results_posted": n.get("results_posted"),
        "snapshot_count": count,
        "note": "Primary completion, company readout guidance, and results publication are different milestones. Different dates alone do not establish a conflict.",
    }
