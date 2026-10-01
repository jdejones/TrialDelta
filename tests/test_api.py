from sqlalchemy import select
from trialdelta.db import User, Follow, Company
from trialdelta.auth import password_hash, current_user
from trialdelta.app import app


def test_home_and_protection(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/health").json()["status"] == "ok"
    assert (
        client.post(
            "/api/follows",
            headers={"Origin": "https://attacker.test"},
            json={"kind": "target", "value": "MET"},
        ).status_code
        == 403
    )
    assert client.get("/", headers={"Host": "attacker.test"}).status_code == 403
    assert (
        client.post(
            "/api/follows",
            headers={"X-TrialDelta-Client": ""},
            json={"kind": "target", "value": "MET"},
        ).status_code
        == 403
    )


def test_follow_idempotent_and_scoped(client, db):
    a = client.post("/api/follows", json={"kind": "target", "value": "MET"}).json()
    b = client.post("/api/follows", json={"kind": "target", "value": "met"}).json()
    assert a["id"] == b["id"]
    other = User(
        email="other@example.com",
        password_hash=password_hash("a long password 123"),
        is_admin=False,
    )
    db.add(other)
    db.commit()
    app.dependency_overrides[current_user] = lambda: other
    assert client.get("/api/follows").json() == []
    assert client.delete("/api/follows/" + str(a["id"])).status_code == 404
    assert client.get("/api/review").status_code == 403
    assert client.post("/api/sync").status_code == 404


def test_invalid_evidence_url(client, db):
    c = db.scalar(select(Company))
    payload = {
        "company_id": c.id,
        "source_type": "presentation",
        "title": "Source title",
        "url": "javascript:alert(1)",
        "published_at": "2026-01-01",
        "quote": "A sufficiently long excerpt",
        "category": "timeline",
        "value": "H2 2026",
    }
    assert client.post("/api/evidence", json=payload).status_code == 422
    payload["url"] = "https://example.com"
    payload["study_id"] = "NCT99999999"
    assert client.post("/api/evidence", json=payload).status_code == 422


def test_hosted_bootstrap_ignores_local_admin(client, db, monkeypatch):
    import trialdelta.auth as auth

    monkeypatch.setattr(auth, "MODE", "hosted")
    monkeypatch.setattr(auth, "ORIGIN", "https://trialdelta.test")
    monkeypatch.setattr(auth, "SECURE", True)
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "Very long password 1234")
    auth.bootstrap()
    assert db.scalar(select(User).where(User.email == "owner@example.com")).is_admin


def test_local_admin_cannot_login_with_hosted_credentials(client):
    assert (
        client.post(
            "/api/login", json={"email": "local@trialdelta.local", "password": "random"}
        ).status_code
        == 401
    )


def test_empty_digest_honest(client):
    result = client.get("/api/digest").json()
    assert result["changes"] == [] and result["follow_count"] == 0
    assert "No update does not establish" in result["note"]
