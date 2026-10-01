import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from .db import (
    initialize,
    get_db,
    Company,
    Study,
    Snapshot,
    Evidence,
    Change,
    User,
    Follow,
)
from .auth import (
    MODE,
    ORIGIN,
    bootstrap,
    current_user,
    browsing_user,
    admin,
)
from .services import (
    as_dict,
    period,
    CATEGORIES,
    published_changes,
    change_visible,
    change_needs_review,
    change_dict,
    review_item as review_saved_item,
    reconciliation,
)

from .home import FILING_PAGE_SIZES, recent_filings, recent_news, recent_trials
from .tad import list_target_action_dates

ROOT = Path(__file__).parent


@asynccontextmanager
async def lifespan(app):
    initialize()
    bootstrap()
    yield


app = FastAPI(
    title="TrialDelta",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs" if MODE == "local" else None,
    redoc_url=None,
)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def boundaries(request, call_next):
    allowed = {urlparse(ORIGIN).netloc}
    if MODE == "local":
        allowed |= {"127.0.0.1:8765", "localhost:8765", "testserver"}
    if request.headers.get("host") not in allowed:
        return JSONResponse({"detail": "Untrusted host"}, 403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if (
            origin
            and origin.rstrip("/") != ORIGIN
            and not (
                MODE == "local"
                and origin in ("http://localhost:8765", "http://127.0.0.1:8765")
            )
        ) or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-origin request rejected"}, 403)
        if request.headers.get("x-trialdelta-client") != "web":
            return JSONResponse({"detail": "Missing request protection header"}, 403)
        try:
            if int(request.headers.get("content-length", "0")) > 100000:
                return JSONResponse({"detail": "Request too large"}, 413)
        except ValueError:
            return JSONResponse({"detail": "Invalid content length"}, 400)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    if request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health(db=Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "version": "0.1.0"}


from .accounts import router as account_router

app.include_router(account_router)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Pydantic's default error includes the submitted value, which could be a
    # password or a one-time token. Keep useful field messages without secrets.
    return JSONResponse(
        {"detail": [
            {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]},
        status_code=422,
    )


@app.get("/api/companies")
def companies(user=Depends(browsing_user), db=Depends(get_db)):
    follows = {
        f.value
        for f in db.scalars(
            select(Follow).where(Follow.user_id == user.id, Follow.kind == "company")
        )
    } if user else set()
    totals = dict(
        db.execute(
            select(Study.company_id, func.count()).group_by(Study.company_id)
        ).all()
    )
    return [
        {
            **{k: v for k, v in as_dict(c).items() if k not in ("last_checked", "last_error")},
            "followed": str(c.id) in follows,
            "study_count": totals.get(c.id, 0),
            "coverage_label": _coverage_label(c.coverage_status),
        }
        for c in db.scalars(select(Company).order_by(Company.name))
    ]


def _coverage_label(status):
    return {"active": "Active", "delisted": "Delisted", "out_of_sector": "Outside Healthcare"}.get(status, "Active")


@app.get("/api/home/trials")
def home_trials(user=Depends(browsing_user), db=Depends(get_db)):
    return recent_trials(db)


@app.get("/api/home/news")
def home_news(user=Depends(browsing_user), db=Depends(get_db)):
    try:
        return recent_news(db)
    except SQLAlchemyError:
        # Connection details and raw source errors belong outside the browser.
        raise HTTPException(503, "Biotechnology news is temporarily unavailable.") from None


@app.get("/api/home/tads")
def home_tads(user=Depends(browsing_user), db=Depends(get_db)):
    """Read saved results only; source imports belong to the administrator."""
    try:
        return list_target_action_dates(db, limit=500)["items"]
    except SQLAlchemyError:
        raise HTTPException(503, "Target action dates are temporarily unavailable.") from None


@app.get("/api/home/filings")
def home_filings(
    page: int = Query(default=1, ge=1, le=2147483647),
    page_size: int = Query(default=10, ge=1, le=100),
    q: str = Query(default="", max_length=200),
    user=Depends(browsing_user),
    db=Depends(get_db),
):
    if page_size not in FILING_PAGE_SIZES:
        raise HTTPException(422, "Select 10, 25, 50, or 100 filings per page.")
    try:
        return recent_filings(db, page=page, page_size=page_size, q=q)
    except SQLAlchemyError:
        raise HTTPException(503, "8-K filings are temporarily unavailable.") from None


@app.get("/api/studies")
def studies(
    q: str = "",
    company_id: int | None = None,
    user=Depends(browsing_user),
    db=Depends(get_db),
):
    query = select(Study).order_by(Study.last_checked.desc(), Study.id)
    if company_id:
        query = query.where(Study.company_id == company_id)
    rows = db.scalars(query).all()
    q = q.strip().casefold()
    if q:
        rows = [
            s
            for s in rows
            if q
            in (" ".join([s.id, s.title, s.drug, s.conditions, s.targets])).casefold()
        ]
    companies = {c.id: c for c in db.scalars(select(Company))}
    return [
        {
            **as_dict(s),
            "conditions": json.loads(s.conditions),
            "targets": json.loads(s.targets),
            "ticker": companies[s.company_id].ticker,
            "company": companies[s.company_id].name,
            "coverage_status": companies[s.company_id].coverage_status,
            "coverage_label": _coverage_label(companies[s.company_id].coverage_status),
        }
        for s in rows
    ]


@app.get("/api/studies/{study_id}")
def study_detail(study_id: str, user=Depends(browsing_user), db=Depends(get_db)):
    row = db.get(Study, study_id)
    if not row:
        raise HTTPException(404, "Trial not found")
    company = db.get(Company, row.company_id)
    snapshots = db.scalars(
        select(Snapshot)
        .where(Snapshot.study_id == study_id)
        .order_by(Snapshot.id.desc())
    ).all()
    evidence = db.scalars(
        select(Evidence)
        .where(Evidence.study_id == study_id, Evidence.review_status == "approved")
        .order_by(Evidence.published_at.desc())
    ).all()
    return {
        "study": as_dict(row),
        "company": {
            "id": company.id, "ticker": company.ticker, "name": company.name,
            "coverage_status": company.coverage_status,
            "coverage_label": _coverage_label(company.coverage_status),
        },
        "reconciliation": reconciliation(db, study_id),
        "snapshots": [
            {k: v for k, v in as_dict(s).items() if k not in ("raw_json", "normalized")}
            for s in snapshots
        ],
        "guidance": [as_dict(e) for e in evidence],
        "changes": [
            c for c in published_changes(db, user.id if user else None) if c["study_id"] == study_id
        ],
    }


class TargetBody(BaseModel):
    targets: list[str] = Field(max_length=30)

    @field_validator("targets")
    @classmethod
    def validate(cls, values):
        if any(not v.strip() or len(v) > 100 for v in values):
            raise ValueError("Targets must be 1–100 characters")
        return sorted(set(v.strip() for v in values))


@app.put("/api/studies/{study_id}/targets")
def targets(study_id: str, body: TargetBody, user=Depends(admin), db=Depends(get_db)):
    row = db.get(Study, study_id)
    if not row:
        raise HTTPException(404, "Trial not found")
    row.targets = json.dumps(body.targets)
    db.commit()
    return {"targets": body.targets}


@app.get("/api/changes")
def changes(
    followed: bool | None = None,
    category: str = "",
    q: str = "",
    user=Depends(browsing_user),
    db=Depends(get_db),
):
    # Members default to their watchlist; visitors explore published changes.
    if followed is None:
        followed = user is not None
    if followed and user is None:
        return []
    rows = published_changes(db, user.id if user else None, followed)
    if category:
        rows = [r for r in rows if r["category"] == category]
    if q:
        rows = [r for r in rows if q.casefold() in json.dumps(r).casefold()]
    return rows


@app.get("/api/changes/{change_id}")
def change_detail(change_id: int, user=Depends(browsing_user), db=Depends(get_db)):
    query = select(Change).where(Change.id == change_id)
    if not user or not user.is_admin:
        query = query.where(change_visible())
    row = db.scalar(query)
    if not row:
        raise HTTPException(404, "Change not found")
    evidence = []
    for label, key, model in [
        ("Previous", "before_snapshot", Snapshot),
        ("Current", "after_snapshot", Snapshot),
        ("Previous", "before_evidence", Evidence),
        ("Current", "after_evidence", Evidence),
    ]:
        pk = getattr(row, key)
        if pk:
            e = db.get(model, pk)
            if e is None:
                continue
            if isinstance(e, Evidence) and e.review_status != "approved" and (not user or not user.is_admin):
                continue
            item = as_dict(e)
            item.pop("raw_json", None)
            if isinstance(e, Snapshot):
                item.update(
                    url=f"https://clinicaltrials.gov/study/{row.study_id}",
                    title="ClinicalTrials.gov snapshot",
                    quote=json.dumps(json.loads(e.normalized), indent=2),
                    source_type="registry",
                )
            item["label"] = label
            evidence.append(item)
    return {
        **change_dict(db, row),
        "evidence": evidence,
        "reconciliation": reconciliation(db, row.study_id),
    }


@app.get("/api/snapshots/{snapshot_id}")
def snapshot(snapshot_id: int, user=Depends(browsing_user), db=Depends(get_db)):
    row = db.get(Snapshot, snapshot_id)
    if not row:
        raise HTTPException(404, "Snapshot not found")
    return JSONResponse(
        json.loads(row.raw_json),
        headers={
            "Content-Disposition": f'attachment; filename="{row.study_id}-{row.id}.json"'
        },
    )


class FollowBody(BaseModel):
    kind: Literal["company", "program", "drug", "target", "indication"]
    value: str = Field(min_length=1, max_length=200)


@app.get("/api/follows")
def follows(user=Depends(current_user), db=Depends(get_db)):
    rows = db.scalars(select(Follow).where(Follow.user_id == user.id)).all()
    return [as_dict(r) for r in rows]


@app.post("/api/follows")
def follow(body: FollowBody, user=Depends(current_user), db=Depends(get_db)):
    value = body.value.strip()
    if not value:
        raise HTTPException(422, "Enter a value to follow")
    if body.kind == "company" and (
        not value.isdigit() or not db.get(Company, int(value))
    ):
        raise HTTPException(404, "Company not found")
    if body.kind == "program" and not db.get(Study, value):
        raise HTTPException(404, "Trial not found")
    existing = db.scalar(
        select(Follow).where(
            Follow.user_id == user.id,
            Follow.kind == body.kind,
            func.lower(Follow.value) == value.lower(),
        )
    )
    if existing:
        return as_dict(existing)
    row = Follow(user_id=user.id, kind=body.kind, value=value)
    db.add(row)
    db.commit()
    return as_dict(row)


@app.delete("/api/follows/{follow_id}")
def unfollow(follow_id: int, user=Depends(current_user), db=Depends(get_db)):
    row = db.get(Follow, follow_id)
    if not row or row.user_id != user.id:
        raise HTTPException(404, "Follow not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


class EvidenceBody(BaseModel):
    company_id: int
    study_id: str | None = None
    source_type: Literal["presentation", "press_release", "filing", "publication"]
    title: str = Field(min_length=3, max_length=500)
    url: str = Field(min_length=10, max_length=2000)
    published_at: date
    quote: str = Field(min_length=10, max_length=20000)
    category: str
    value: str = Field(min_length=1, max_length=1000)
    reason: str | None = Field(default=None, max_length=2000)
    reason_checked: bool = False

    @field_validator("url")
    @classmethod
    def safe_url(cls, v):
        u = urlparse(v)
        if (
            u.scheme not in ("https", "http")
            or not u.hostname
            or u.username
            or u.password
        ):
            raise ValueError("Use a public HTTP(S) source URL without credentials")
        return v

    @field_validator("category")
    @classmethod
    def category_known(cls, v):
        if v not in CATEGORIES:
            raise ValueError("Unknown change category")
        return v


@app.post("/api/evidence")
def add_evidence(body: EvidenceBody, user=Depends(current_user), db=Depends(get_db)):
    if not db.get(Company, body.company_id):
        raise HTTPException(404, "Company not found")
    if body.published_at > datetime.now(timezone.utc).date():
        raise HTTPException(422, "Source publication date cannot be in the future")
    if body.study_id:
        s = db.get(Study, body.study_id)
        if not s or s.company_id != body.company_id:
            raise HTTPException(422, "Trial must belong to the selected company")
    data = body.model_dump()
    data["published_at"] = body.published_at.isoformat()
    start, end, precision = (
        period(body.value)
        if body.category == "timeline"
        else (None, None, "not_applicable")
    )
    row = Evidence(
        **data,
        created_by=user.id,
        period_start=start,
        period_end=end,
        precision=precision,
    )
    db.add(row)
    db.commit()
    return as_dict(row)


@app.get("/api/review")
def review(user=Depends(admin), db=Depends(get_db)):
    company_map = {c.id: c for c in db.scalars(select(Company))}
    return {
        "changes": [
            change_dict(db, c)
            for c in db.scalars(
                select(Change)
                .where(change_needs_review())
                .order_by(Change.id.desc())
            )
        ],
        "evidence": [
            {
                **as_dict(e),
                "company": company_map[e.company_id].name,
                "ticker": company_map[e.company_id].ticker,
            }
            for e in db.scalars(
                select(Evidence)
                .where(Evidence.review_status == "pending")
                .order_by(Evidence.id)
            )
        ],
    }


class ReviewBody(BaseModel):
    action: Literal["approve", "reject"]
    note: str = Field(default="", max_length=2000)


@app.post("/api/review/{kind}/{item_id}")
def review_item(
    kind: Literal["evidence", "change"],
    item_id: int,
    body: ReviewBody,
    user=Depends(admin),
    db=Depends(get_db),
):
    return review_saved_item(db, kind, item_id, body.action, body.note, user.id)


@app.get("/api/digest")
def digest(day: date | None = None, user=Depends(current_user), db=Depends(get_db)):
    from .digest import build_digest

    return build_digest(
        db,
        user,
        day.isoformat() if day else datetime.now(timezone.utc).date().isoformat(),
    )


@app.get("/api/digest/export")
def digest_export(
    day: date | None = None, user=Depends(current_user), db=Depends(get_db)
):
    from .digest import build_digest, digest_text

    result = build_digest(
        db,
        user,
        day.isoformat() if day else datetime.now(timezone.utc).date().isoformat(),
    )
    return PlainTextResponse(
        digest_text(result),
        headers={
            "Content-Disposition": f'attachment; filename="trialdelta-{result["day"]}.txt"'
        },
    )


class DigestPrefs(BaseModel):
    enabled: bool


@app.put("/api/digest/preferences")
def digest_prefs(body: DigestPrefs, user=Depends(current_user), db=Depends(get_db)):
    row = db.get(User, user.id)
    row.digest_enabled = body.enabled
    db.commit()
    return {
        "enabled": row.digest_enabled,
        "smtp_configured": bool(os.getenv("SMTP_HOST")),
    }
