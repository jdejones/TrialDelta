import hashlib
import json
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text, update

from .db import (
    initialize,
    get_db,
    Session,
    Company,
    Study,
    Snapshot,
    Evidence,
    Change,
    User,
    Login,
    Follow,
    Job,
    now,
)
from .auth import (
    MODE,
    ORIGIN,
    SECURE,
    bootstrap,
    current_user,
    admin,
    verify_password,
    password_hash,
    new_session,
)
from .seed import seed
from .services import (
    as_dict,
    period,
    CATEGORIES,
    published_changes,
    change_dict,
    approve_evidence,
    reconciliation,
)
from .registry import sync_all
from .inbox import SourceCandidate, import_news

ROOT = Path(__file__).parent


@asynccontextmanager
async def lifespan(app):
    initialize()
    seed()
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


@app.get("/api/me")
def me(user=Depends(current_user)):
    return {
        "id": user.id,
        "email": user.email,
        "is_admin": user.is_admin,
        "mode": MODE,
        "digest_enabled": user.digest_enabled,
    }


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


_attempts = {}
_auth_lock = threading.Lock()


def throttle(request):
    key = request.client.host
    with _auth_lock:
        cutoff = time.monotonic() - 900
        for address in list(_attempts):
            _attempts[address] = [t for t in _attempts[address] if t > cutoff]
            if not _attempts[address]:
                del _attempts[address]
        attempts = _attempts.setdefault(key, [])
        if len(attempts) >= 15:
            raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
        attempts.append(time.monotonic())


@app.post("/api/login")
def login(body: Credentials, request: Request, response: Response, db=Depends(get_db)):
    throttle(request)
    user = db.scalar(select(User).where(User.email == body.email.lower().strip()))
    if (
        not user
        or user.email == "local@trialdelta.local"
        or not verify_password(body.password, user.password_hash)
    ):
        raise HTTPException(401, "Email or password is incorrect")
    token = new_session(db, user)
    response.set_cookie(
        "td_session",
        token,
        httponly=True,
        secure=SECURE,
        samesite="lax",
        max_age=604800,
        path="/",
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response, db=Depends(get_db)):
    token = request.cookies.get("td_session", "")
    row = db.get(Login, hashlib.sha256(token.encode()).hexdigest())
    if row:
        db.delete(row)
        db.commit()
    response.delete_cookie("td_session", path="/")
    return {"ok": True}


@app.post("/api/users")
def create_user(body: Credentials, user=Depends(admin), db=Depends(get_db)):
    if len(body.password) < 14 or not re.fullmatch(
        r"[^\s@]+@[^\s@]+\.[^\s@]+", body.email
    ):
        raise HTTPException(
            422, "Valid email and a password of at least 14 characters required"
        )
    if db.scalar(select(User).where(User.email == body.email.lower().strip())):
        raise HTTPException(409, "Account already exists")
    row = User(
        email=body.email.lower().strip(), password_hash=password_hash(body.password)
    )
    db.add(row)
    db.commit()
    return {"id": row.id, "email": row.email}


@app.get("/api/overview")
def overview(user=Depends(current_user), db=Depends(get_db)):
    counts = {
        key: db.scalar(select(func.count()).select_from(model))
        for key, model in [
            ("companies", Company),
            ("studies", Study),
            ("snapshots", Snapshot),
        ]
    }
    counts["changes"] = db.scalar(
        select(func.count())
        .select_from(Change)
        .where(Change.review_status == "approved")
    )
    counts["pending"] = db.scalar(
        select(func.count())
        .select_from(Change)
        .where(Change.review_status == "pending")
    ) + db.scalar(
        select(func.count())
        .select_from(Evidence)
        .where(Evidence.review_status == "pending")
    )
    counts["follows"] = db.scalar(
        select(func.count()).select_from(Follow).where(Follow.user_id == user.id)
    )
    job = db.scalar(select(Job).order_by(Job.id.desc()).limit(1))
    return {
        **counts,
        "last_job": as_dict(job) if job else None,
        "today": datetime.now(timezone.utc).date().isoformat(),
    }


@app.get("/api/companies")
def companies(user=Depends(current_user), db=Depends(get_db)):
    follows = {
        f.value
        for f in db.scalars(
            select(Follow).where(Follow.user_id == user.id, Follow.kind == "company")
        )
    }
    totals = dict(
        db.execute(
            select(Study.company_id, func.count()).group_by(Study.company_id)
        ).all()
    )
    return [
        {
            **as_dict(c),
            "followed": str(c.id) in follows,
            "study_count": totals.get(c.id, 0),
        }
        for c in db.scalars(select(Company).order_by(Company.name))
    ]


@app.get("/api/studies")
def studies(
    q: str = "",
    company_id: int | None = None,
    user=Depends(current_user),
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
        }
        for s in rows
    ]


@app.get("/api/studies/{study_id}")
def study_detail(study_id: str, user=Depends(current_user), db=Depends(get_db)):
    row = db.get(Study, study_id)
    if not row:
        raise HTTPException(404, "Trial not found")
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
        "reconciliation": reconciliation(db, study_id),
        "snapshots": [
            {k: v for k, v in as_dict(s).items() if k not in ("raw_json", "normalized")}
            for s in snapshots
        ],
        "guidance": [as_dict(e) for e in evidence],
        "changes": [
            c for c in published_changes(db, user.id) if c["study_id"] == study_id
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
    followed: bool = False,
    category: str = "",
    q: str = "",
    user=Depends(current_user),
    db=Depends(get_db),
):
    rows = published_changes(db, user.id, followed)
    if category:
        rows = [r for r in rows if r["category"] == category]
    if q:
        rows = [r for r in rows if q.casefold() in json.dumps(r).casefold()]
    return rows


@app.get("/api/changes/{change_id}")
def change_detail(change_id: int, user=Depends(current_user), db=Depends(get_db)):
    row = db.get(Change, change_id)
    if not row or (row.review_status != "approved" and not user.is_admin):
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
def snapshot(snapshot_id: int, user=Depends(current_user), db=Depends(get_db)):
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
def add_evidence(body: EvidenceBody, user=Depends(admin), db=Depends(get_db)):
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
                .where(Change.review_status == "pending")
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
    row = db.get(Evidence if kind == "evidence" else Change, item_id)
    if not row:
        raise HTTPException(404, "Item not found")
    if row.review_status != "pending":
        raise HTTPException(409, "Item already reviewed")
    if kind == "evidence" and body.action == "approve":
        return approve_evidence(db, row, user.id)
    model = Evidence if kind == "evidence" else Change
    status = "approved" if body.action == "approve" else "rejected"
    values = {"review_status": status, "reviewed_by": user.id, "reviewed_at": now()}
    if kind == "change":
        values.update(reviewed=body.action == "approve", review_note=body.note)
    result = db.execute(
        update(model)
        .where(model.id == item_id, model.review_status == "pending")
        .values(**values)
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "Item already reviewed")
    db.commit()
    return {"status": status}


@app.post("/api/sync")
def sync(
    background: BackgroundTasks,
    company_id: int | None = None,
    user=Depends(admin),
    db=Depends(get_db),
):
    if company_id and not db.get(Company, company_id):
        raise HTTPException(404, "Company not found")
    background.add_task(sync_all, company_id)
    return {"status": "queued"}


@app.get("/api/jobs")
def jobs(user=Depends(current_user), db=Depends(get_db)):
    return [
        as_dict(j) for j in db.scalars(select(Job).order_by(Job.id.desc()).limit(20))
    ]


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


@app.get("/api/inbox")
def inbox(user=Depends(admin), db=Depends(get_db)):
    companies = {c.id: c for c in db.scalars(select(Company))}
    return [
        {
            **as_dict(r),
            "company": companies[r.company_id].name,
            "ticker": companies[r.company_id].ticker,
        }
        for r in db.scalars(
            select(SourceCandidate)
            .where(SourceCandidate.status == "unreviewed")
            .order_by(SourceCandidate.published_at.desc())
            .limit(500)
        )
    ]


@app.post("/api/inbox/import")
def import_sources(user=Depends(admin)):
    try:
        return import_news()
    except Exception:
        raise HTTPException(
            503,
            "The optional news database is unavailable. Registry collection and manual evidence still work.",
        )


@app.post("/api/inbox/{candidate_id}/dismiss")
def dismiss_candidate(candidate_id: int, user=Depends(admin), db=Depends(get_db)):
    row = db.get(SourceCandidate, candidate_id)
    if not row:
        raise HTTPException(404, "Source candidate not found")
    row.status = "dismissed"
    db.commit()
    return {"ok": True}
