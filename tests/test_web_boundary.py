"""The browser consumes curated data; collection belongs to local operators."""

from copy import deepcopy

import pytest
from sqlalchemy import func, select

from trialdelta.app import app
from trialdelta.auth import browsing_user, current_user, password_hash
from trialdelta.db import Change, Company, Evidence, Follow, Job, Snapshot, User, now
from trialdelta.registry import ingest


def reader_account(db):
    reader = User(
        email="reader@example.com",
        password_hash=password_hash("reader password for testing"),
        is_admin=False,
    )
    db.add(reader)
    db.commit()
    return reader


@pytest.mark.parametrize("as_reader", [False, True], ids=["local-admin", "reader"])
def test_collection_and_database_operations_have_no_web_routes(client, db, as_reader):
    if as_reader:
        reader = reader_account(db)
        app.dependency_overrides[current_user] = lambda: reader
        app.dependency_overrides[browsing_user] = lambda: reader

    for method, path in [
        ("POST", "/api/sync"),
        ("POST", "/api/sync?company_id=1"),
        ("GET", "/api/overview"),
        ("GET", "/api/jobs"),
        ("GET", "/api/inbox"),
        ("POST", "/api/inbox/import"),
        ("POST", "/api/inbox/1/dismiss"),
    ]:
        response = client.request(method, path)
        assert response.status_code == 404, (method, path, response.text)

    assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_reader_evidence_waits_for_admin_and_cannot_be_published_by_reader(client, db, raw):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    ingest(db, company, raw)
    db.commit()
    reader = reader_account(db)
    app.dependency_overrides[current_user] = lambda: reader
    app.dependency_overrides[browsing_user] = lambda: reader

    response = client.post(
        "/api/evidence",
        json={
            "company_id": company.id,
            "study_id": "NCT01234567",
            "source_type": "presentation",
            "title": "Company presentation, page 12",
            "url": "https://example.org/primary-document",
            "published_at": "2026-01-01",
            "quote": "We expect primary results in the first half of 2026.",
            "category": "timeline",
            "value": "H1 2026",
        },
    )
    assert response.status_code == 200, response.text
    submitted = response.json()
    assert submitted["review_status"] == "pending"
    assert submitted["created_by"] == reader.id
    assert submitted["reviewed_by"] is None
    assert client.get("/api/review").status_code == 403
    for action in ["approve", "reject"]:
        assert client.post(
            f'/api/review/evidence/{submitted["id"]}', json={"action": action}
        ).status_code == 403
    assert client.get("/api/studies/NCT01234567").json()["guidance"] == []
    assert client.get("/api/changes").json() == []
    assert client.get("/api/digest").json()["changes"] == []
    stored = db.get(Evidence, submitted["id"])
    assert stored.review_status == "pending"
    assert stored.reviewed_at is None

    app.dependency_overrides.clear()
    assert any(
        row["id"] == submitted["id"]
        for row in client.get("/api/review").json()["evidence"]
    )


@pytest.mark.parametrize("as_reader", [False, True], ids=["local-admin", "reader"])
def test_browsing_uses_imported_records_without_collection(client, db, raw, monkeypatch, as_reader):
    import trialdelta.app as web
    import trialdelta.inbox as inbox
    import trialdelta.registry as registry

    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    ingest(db, company, raw)
    updated = deepcopy(raw)
    updated["protocolSection"]["statusModule"]["overallStatus"] = "COMPLETED"
    ingest(db, company, updated)
    change = db.scalar(select(Change))
    assert change.review_status == "automatic"
    db.commit()
    snapshot_id = db.scalar(select(Snapshot.id))
    if as_reader:
        reader = reader_account(db)
        app.dependency_overrides[current_user] = lambda: reader
        app.dependency_overrides[browsing_user] = lambda: reader

    active_user = reader if as_reader else db.scalar(select(User))
    db.add(Follow(user_id=active_user.id, kind="company", value=str(company.id)))
    db.commit()

    def forbidden_collection(*args, **kwargs):
        pytest.fail("Browsing attempted to collect external source data")

    monkeypatch.setattr(registry, "fetch_company", forbidden_collection)
    monkeypatch.setattr(registry, "sync_all", forbidden_collection)
    monkeypatch.setattr(inbox, "import_news", forbidden_collection)
    # Also catch a regression that imports collector functions into the HTTP module.
    for name in ["sync_all", "import_news"]:
        if hasattr(web, name):
            monkeypatch.setattr(web, name, forbidden_collection)

    for path in [
        "/",
        "/api/health",
        "/api/me",
        "/api/companies",
        "/api/home/trials",
        "/api/home/news",
        "/api/studies",
        f"/api/studies?company_id={company.id}",
        "/api/studies?q=NCT01234567",
        "/api/studies/NCT01234567",
        f"/api/snapshots/{snapshot_id}",
        "/api/changes",
        f"/api/changes/{change.id}",
        "/api/follows",
        "/api/digest",
        "/api/digest/export",
    ]:
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)

    assert client.get("/api/studies").json()[0]["status"] == "COMPLETED"
    assert len(client.get("/api/changes").json()) == 1
    assert db.scalar(select(func.count()).select_from(Job)) == 0
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 2
