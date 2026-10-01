"""Visitors can read published records without inheriting a customer identity."""

import hashlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from trialdelta import auth, home
from trialdelta.db import AccountProfile, Change, Company, Evidence, Follow, Job, Login, Snapshot, User
from trialdelta.registry import ingest


@pytest.fixture
def visitor(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_REQUIRED", True)
    monkeypatch.setattr(auth, "PUBLIC_BROWSING_ENABLED", True)
    # Never connect to a developer's configured news source in these tests.
    monkeypatch.setattr(home, "_source_engine", lambda: None)
    return client


@pytest.fixture
def public_records(visitor, db, raw):
    companies = db.scalars(select(Company).order_by(Company.id).limit(2)).all()
    events = []
    for index, company in enumerate(companies):
        baseline = deepcopy(raw)
        study_id = f"NCT0123456{index + 7}"
        baseline["protocolSection"]["identificationModule"]["nctId"] = study_id
        assert ingest(db, company, baseline) == 0
        updated = deepcopy(baseline)
        updated["protocolSection"]["statusModule"]["overallStatus"] = "COMPLETED"
        assert ingest(db, company, updated) == 1
        events.append(db.scalar(select(Change).where(Change.study_id == study_id)))
    owner = db.scalar(select(User).where(User.email == auth.LOCAL_EMAIL))
    db.add(Follow(user_id=owner.id, kind="company", value=str(companies[0].id)))
    db.commit()
    return companies, events


def public_paths(db, events):
    return [
        "/api/companies", "/api/home/trials", "/api/home/news", "/api/studies",
        "/api/studies/NCT01234567", "/api/changes", f"/api/changes/{events[0].id}",
        f"/api/snapshots/{db.scalar(select(Snapshot.id))}",
    ]


@pytest.mark.parametrize("mode", ["local", "hosted"])
def test_enabled_public_browsing_reads_without_creating_identity_or_running_collectors(
    visitor, db, public_records, monkeypatch, mode,
):
    import trialdelta.app as web
    import trialdelta.inbox as inbox
    import trialdelta.registry as registry

    monkeypatch.setattr(auth, "MODE", mode)
    if mode == "hosted":
        # Exercise the configured hosted boundary only inside this test client.
        monkeypatch.setattr(web, "MODE", "hosted")
        monkeypatch.setattr(web, "ORIGIN", "https://testserver")
        monkeypatch.setattr(auth, "ORIGIN", "https://testserver")
        monkeypatch.setattr(auth, "SECURE", True)

    def forbidden_collection(*args, **kwargs):
        pytest.fail("Public browsing must only read saved records")

    monkeypatch.setattr(registry, "fetch_company", forbidden_collection)
    monkeypatch.setattr(registry, "sync_all", forbidden_collection)
    monkeypatch.setattr(inbox, "import_news", forbidden_collection)
    models = (User, Login, Follow, AccountProfile, Snapshot, Job)
    before = {model: db.scalar(select(func.count()).select_from(model)) for model in models}
    _, events = public_records
    config = visitor.get("/api/auth/config").json()
    assert config["public_browsing_enabled"] is True
    assert config["auth_required"] is True
    for path in public_paths(db, events):
        response = visitor.get(path)
        assert response.status_code == 200, (path, response.text)
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers
    assert not visitor.cookies
    assert all(row["followed"] is False for row in visitor.get("/api/companies").json())
    assert {row["id"] for row in visitor.get("/api/changes").json()} == {row.id for row in events}
    assert visitor.get("/api/changes?followed=true").json() == []
    assert {model: db.scalar(select(func.count()).select_from(model)) for model in models} == before


@pytest.mark.parametrize("path", [
    "/api/me", "/api/account", "/api/follows", "/api/digest", "/api/digest/export", "/api/review",
])
def test_public_browsing_keeps_customer_and_review_reads_private(visitor, path):
    response = visitor.get(path)
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": "Please sign in"}
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("method,path,payload", [
    ("PATCH", "/api/account", {"display_name": "Guest attempt"}),
    ("POST", "/api/account/password", {
        "current_password": "A sufficiently long password 123!",
        "new_password": "A different long password 456!",
    }),
    ("POST", "/api/account/verification", None),
    ("POST", "/api/logout-all", None),
    ("POST", "/api/follows", {"kind": "target", "value": "MET"}),
    ("DELETE", "/api/follows/1", None),
    ("PUT", "/api/digest/preferences", {"enabled": True}),
    ("PUT", "/api/studies/NCT01234567/targets", {"targets": ["MET"]}),
    ("POST", "/api/review/change/1", {"action": "approve"}),
    ("POST", "/api/users", {"email": "guest@example.com", "password": "A sufficiently long password 123!"}),
    ("POST", "/api/evidence", {
        "company_id": 1, "source_type": "presentation", "title": "Guest submission",
        "url": "https://example.org/evidence", "published_at": "2026-01-01",
        "quote": "A sufficiently long source excerpt", "category": "timeline", "value": "H2 2026",
    }),
])
def test_public_browsing_does_not_authorize_personal_or_reviewer_writes(visitor, method, path, payload):
    response = visitor.request(method, path, json=payload)
    assert response.status_code == 401, response.text


@pytest.mark.parametrize("cookie_kind", ["forged", "oversized", "expired"])
def test_invalid_sessions_fall_back_to_guest_only_for_public_reads(visitor, db, public_records, cookie_kind):
    token = "forged-token" if cookie_kind == "forged" else "x" * 257
    if cookie_kind == "expired":
        customer = User(email="expired@example.com", password_hash="unused", is_admin=False)
        db.add(customer)
        db.commit()
        token = auth.new_session(db, customer)
        login = db.get(Login, hashlib.sha256(token.encode()).hexdigest())
        login.expires_at = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        db.commit()
    visitor.cookies.set("td_session", token)
    assert visitor.get("/api/companies").status_code == 200
    assert all(row["followed"] is False for row in visitor.get("/api/companies").json())
    assert visitor.get("/api/changes?followed=true").json() == []
    assert visitor.get("/api/account").status_code == 401
    assert visitor.post("/api/follows", json={"kind": "target", "value": "MET"}).status_code == 401


def test_signed_in_browsing_preserves_personal_scope_then_logout_returns_to_public(
    visitor, db, public_records,
):
    companies, events = public_records
    customer = User(email="reader@example.com", password_hash="unused", is_admin=False)
    db.add(customer)
    db.flush()
    db.add(Follow(user_id=customer.id, kind="company", value=str(companies[1].id)))
    db.commit()
    visitor.cookies.set("td_session", auth.new_session(db, customer))
    assert [row["id"] for row in visitor.get("/api/changes").json()] == [events[1].id]
    assert {row["id"] for row in visitor.get("/api/changes?followed=false").json()} == {row.id for row in events}
    assert [row["id"] for row in visitor.get("/api/companies").json() if row["followed"]] == [companies[1].id]
    assert visitor.get("/api/account").json()["id"] == customer.id
    assert visitor.post("/api/logout").status_code == 200
    # Clear any domainless cookie set directly by this test rather than a server.
    visitor.cookies.clear()
    assert {row["id"] for row in visitor.get("/api/changes").json()} == {row.id for row in events}
    assert visitor.get("/api/account").status_code == 401


def test_disabling_public_browsing_restores_the_gate(visitor, db, public_records, monkeypatch):
    monkeypatch.setattr(auth, "PUBLIC_BROWSING_ENABLED", False)
    assert visitor.get("/api/auth/config").json()["public_browsing_enabled"] is False
    _, events = public_records
    for path in public_paths(db, events):
        assert visitor.get(path).status_code == 401, path
    assert visitor.get("/").status_code == 200
    assert visitor.get("/api/health").status_code == 200
    customer = User(email="gated-reader@example.com", password_hash="unused", is_admin=False)
    db.add(customer)
    db.commit()
    visitor.cookies.set("td_session", auth.new_session(db, customer))
    for path in public_paths(db, events):
        assert visitor.get(path).status_code == 200, path


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_public_changes_and_trial_detail_do_not_expose_unapproved_company_evidence(
    visitor, db, public_records, status,
):
    companies, _ = public_records
    owner = db.scalar(select(User).where(User.email == auth.LOCAL_EMAIL))
    hidden = Evidence(
        company_id=companies[0].id, study_id="NCT01234567", source_type="presentation",
        title="Private unreviewed statement", quote="Private source excerpt for review only",
        url="https://example.org/private-evidence", published_at="2026-01-01",
        category="timeline", value="H2 2026", review_status=status, created_by=owner.id,
    )
    db.add(hidden)
    db.flush()
    hidden_change = Change(
        company_id=companies[0].id, study_id="NCT01234567", source="company",
        category="timeline", title="Private company guidance", review_status=status,
        after_evidence=hidden.id,
    )
    db.add(hidden_change)
    db.commit()
    assert hidden_change.id not in {row["id"] for row in visitor.get("/api/changes").json()}
    assert visitor.get(f"/api/changes/{hidden_change.id}").status_code == 404
    detail = visitor.get("/api/studies/NCT01234567")
    assert detail.status_code == 200
    assert detail.json()["guidance"] == []
    assert "Private source excerpt" not in detail.text
    # An inconsistent legacy approval must not reveal an unapproved linked source.
    hidden_change.review_status = "approved"
    db.commit()
    detail = visitor.get(f"/api/changes/{hidden_change.id}")
    assert detail.status_code in (200, 404)
    assert "Private source excerpt" not in detail.text
    assert "private-evidence" not in detail.text
    if detail.status_code == 200:
        assert detail.json()["evidence"] == []


def test_local_public_browsing_still_rejects_non_loopback_clients(visitor, db):
    request = SimpleNamespace(client=SimpleNamespace(host="203.0.113.4"), cookies={})
    with pytest.raises(HTTPException) as error:
        auth.optional_user(request, db)
    assert error.value.status_code == 403
