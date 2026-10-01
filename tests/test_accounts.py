"""Customer accounts use real, isolated database sessions without sending email."""

import hashlib
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from trialdelta import auth
from trialdelta.app import app
from trialdelta.db import Company, Follow, Login, Session, User, initialize


PASSWORD = "A sufficiently long password 123!"
NEW_PASSWORD = "A different long password 456!"


@pytest.fixture
def accounts(client, monkeypatch):
    from trialdelta import accounts as account_routes

    monkeypatch.setattr(auth, "AUTH_REQUIRED", True)
    monkeypatch.setattr(auth, "PUBLIC_BROWSING_ENABLED", False)
    monkeypatch.setattr(auth, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(account_routes, "email_delivery_available", lambda: False)
    return client


@pytest.fixture
def mail(monkeypatch):
    from trialdelta import accounts as account_routes

    sent = []

    def capture(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    monkeypatch.setattr(account_routes, "email_delivery_available", lambda: True)
    monkeypatch.setattr(account_routes, "send_account_email", capture)
    return sent


def register(client, email="customer@example.com", **extra):
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": "Customer", **extra},
    )
    assert response.status_code in (200, 201), response.text
    return response


def sign_in(client, email="customer@example.com", password=PASSWORD):
    response = client.post("/api/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


def session_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def mailed_token(sent, purpose):
    text = str(sent[-1])
    match = re.search(r"#" + re.escape(purpose) + r"=([A-Za-z0-9_-]+)", text)
    assert match, "The account email must carry the account action link"
    return match.group(1)


def assert_private(response, *secrets):
    assert response.headers["cache-control"] == "no-store"
    for secret in secrets:
        assert secret not in response.text
    for field in ("password_hash", "token_hash", "reset_token", "verification_token"):
        assert field not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/me", "/api/account", "/api/companies", "/api/home/trials",
        "/api/home/news", "/api/studies", "/api/changes", "/api/follows",
        "/api/digest", "/api/digest/export", "/api/review",
    ],
)
def test_customer_data_requires_a_session_even_on_local_server(accounts, path):
    response = accounts.get(path)
    assert response.status_code == 401, response.text
    assert_private(response)


def test_public_shell_and_auth_configuration_remain_available(accounts):
    assert accounts.get("/").status_code == 200
    assert accounts.get("/api/health").status_code == 200
    config = accounts.get("/api/auth/config").json()
    assert config["auth_required"] is True
    assert config["public_browsing_enabled"] is False
    assert config["registration_enabled"] is True
    assert config["password_min_length"] == 14
    assert config["email_delivery_available"] is False


def test_registration_normalizes_and_persists_customer_and_hashed_session(accounts):
    response = register(accounts, "  Customer@Example.COM  ")
    user = response.json()["user"]
    assert user["email"] == "customer@example.com"
    assert user["display_name"] == "Customer"
    assert user["is_admin"] is False
    assert user["email_verified"] is False
    assert response.json()["verification_sent"] is False
    assert response.json()["email_delivery_available"] is False
    token = accounts.cookies.get("td_session")
    assert token and len(token) >= 40
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert "max-age=" in cookie
    assert_private(response, token, PASSWORD)
    with Session() as db:
        stored = db.get(User, user["id"])
        assert stored.password_hash != PASSWORD
        assert auth.verify_password(PASSWORD, stored.password_hash)
        session = db.get(Login, session_hash(token))
        assert session and session.user_id == stored.id
        assert session.token_hash != token
        assert datetime.fromisoformat(session.expires_at) > datetime.now(timezone.utc)
    with TestClient(app) as fresh_browser:
        fresh_browser.cookies.set("td_session", token)
        assert fresh_browser.get("/api/account").json()["id"] == user["id"]
    assert accounts.get("/api/review").status_code == 403
    assert accounts.post("/api/users", json={"email": "other@example.com", "password": PASSWORD}).status_code == 403


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "missing-domain", "password": PASSWORD},
        {"email": "with space@example.com", "password": PASSWORD},
        {"email": "a@example.com", "password": "short"},
        {"email": "a@example.com", "password": "x" * 257},
        {"email": "a@example.com", "password": PASSWORD, "display_name": "x" * 101},
        {"email": "a@example.com", "password": PASSWORD, "is_admin": True},
        {"email": "a@example.com", "password": PASSWORD, "email_verified": True},
        {"email": "local@trialdelta.local", "password": PASSWORD},
    ],
)
def test_registration_rejects_invalid_and_privileged_fields(accounts, payload):
    response = accounts.post("/api/auth/register", json=payload)
    assert response.status_code in (400, 409, 422), response.text
    assert accounts.get("/api/me").status_code == 401


def test_normalized_duplicate_cannot_create_second_account(accounts):
    register(accounts)
    response = accounts.post(
        "/api/auth/register", json={"email": "  CUSTOMER@EXAMPLE.COM ", "password": PASSWORD}
    )
    assert response.status_code == 409, response.text
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(User).where(User.email == "customer@example.com")) == 1


def test_login_logout_expired_and_revoked_sessions(accounts):
    register(accounts)
    first_token = accounts.cookies.get("td_session")
    response = accounts.post("/api/logout")
    assert response.status_code == 200
    assert not accounts.cookies.get("td_session")
    with Session() as db:
        assert db.get(Login, session_hash(first_token)) is None
    accounts.cookies.set("td_session", first_token)
    assert accounts.get("/api/me").status_code == 401
    accounts.cookies.clear()
    invalid = accounts.post("/api/login", json={"email": "customer@example.com", "password": "incorrect"})
    unknown = accounts.post("/api/login", json={"email": "missing@example.com", "password": "incorrect"})
    assert invalid.status_code == unknown.status_code == 401
    assert invalid.json() == unknown.json()
    response = sign_in(accounts, email=" CUSTOMER@EXAMPLE.COM ")
    token = accounts.cookies.get("td_session")
    assert token != first_token
    assert_private(response, token, PASSWORD)
    with Session() as db:
        db.get(Login, session_hash(token)).expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        db.commit()
    assert accounts.get("/api/me").status_code == 401
    accounts.cookies.clear()
    accounts.cookies.set("td_session", "forged-session")
    assert accounts.get("/api/me").status_code == 401


def test_profiles_preferences_and_follows_are_isolated(accounts):
    first = register(accounts).json()["user"]
    follow = accounts.post("/api/follows", json={"kind": "target", "value": "MET"}).json()
    response = accounts.patch("/api/account", json={"display_name": "  Alice Example  ", "digest_enabled": True})
    assert response.status_code == 200, response.text
    assert response.json()["display_name"] == "Alice Example"
    assert response.json()["digest_enabled"] is True
    assert_private(response, PASSWORD)
    accounts.post("/api/logout")
    second = register(accounts, "second@example.com").json()["user"]
    assert second["id"] != first["id"]
    assert accounts.get("/api/account").json()["digest_enabled"] is False
    assert accounts.get("/api/follows").json() == []
    assert accounts.delete(f'/api/follows/{follow["id"]}').status_code == 404
    assert accounts.patch("/api/account", json={"email": "changed@example.com"}).status_code == 422
    assert accounts.patch("/api/account", json={"is_admin": True}).status_code == 422
    assert accounts.patch("/api/account", json={"user_id": first["id"], "display_name": "Changed"}).status_code == 422
    accounts.post("/api/logout")
    sign_in(accounts)
    profile = accounts.get("/api/account").json()
    assert profile["id"] == first["id"]
    assert profile["display_name"] == "Alice Example"
    assert profile["digest_enabled"] is True
    assert len(accounts.get("/api/follows").json()) == 1


def test_password_change_checks_current_password_and_rotates_all_sessions(accounts):
    register(accounts)
    other_token = accounts.cookies.get("td_session")
    sign_in(accounts)
    old_token = accounts.cookies.get("td_session")
    assert old_token != other_token
    rejected = accounts.post("/api/account/password", json={"current_password": "incorrect", "new_password": NEW_PASSWORD})
    assert rejected.status_code in (400, 401), rejected.text
    assert accounts.get("/api/me").status_code == 200
    response = accounts.post("/api/account/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert response.status_code == 200, response.text
    current_token = accounts.cookies.get("td_session")
    assert current_token not in (old_token, other_token)
    assert_private(response, current_token, PASSWORD, NEW_PASSWORD)
    with Session() as db:
        assert db.get(Login, session_hash(old_token)) is None
        assert db.get(Login, session_hash(other_token)) is None
        assert db.get(Login, session_hash(current_token)) is not None
    accounts.cookies.clear()
    assert accounts.post("/api/login", json={"email": "customer@example.com", "password": PASSWORD}).status_code == 401
    sign_in(accounts, password=NEW_PASSWORD)


def test_logout_all_revokes_persistent_sessions(accounts):
    register(accounts)
    first = accounts.cookies.get("td_session")
    sign_in(accounts)
    second = accounts.cookies.get("td_session")
    response = accounts.post("/api/logout-all")
    assert response.status_code == 200, response.text
    assert not accounts.cookies.get("td_session")
    with Session() as db:
        assert db.get(Login, session_hash(first)) is None
        assert db.get(Login, session_hash(second)) is None
    accounts.cookies.set("td_session", first)
    assert accounts.get("/api/me").status_code == 401


def test_account_mutations_require_browser_request_protection(accounts):
    response = accounts.post("/api/auth/register", headers={"X-TrialDelta-Client": ""}, json={"email": "customer@example.com", "password": PASSWORD})
    assert response.status_code == 403
    register(accounts)
    for path, payload in [
        ("/api/account/password", {"current_password": PASSWORD, "new_password": NEW_PASSWORD}),
        ("/api/logout", None),
    ]:
        assert accounts.post(path, headers={"Origin": "https://attacker.example"}, json=payload).status_code == 403
    assert accounts.get("/api/me").status_code == 200


def test_verification_uses_hashed_expiring_single_use_token(accounts, mail):
    from trialdelta.db import AccountProfile, AccountToken

    response = register(accounts)
    assert response.json()["verification_sent"] is True
    token = mailed_token(mail, "verify-email")
    assert_private(response, token)
    with Session() as db:
        saved = db.get(AccountToken, session_hash(token))
        assert saved and saved.user_id == response.json()["user"]["id"]
        assert datetime.fromisoformat(saved.expires_at) > datetime.now(timezone.utc)
    verified = accounts.post("/api/auth/verify-email", json={"token": token})
    assert verified.status_code == 200, verified.text
    assert_private(verified, token)
    assert accounts.get("/api/account").json()["email_verified"] is True
    assert accounts.post("/api/auth/verify-email", json={"token": token}).status_code in (400, 422)
    with Session() as db:
        profile = db.get(AccountProfile, response.json()["user"]["id"])
        assert profile.email_verified_at


def test_expired_verification_token_cannot_verify_account(accounts, mail):
    from trialdelta.db import AccountToken

    register(accounts)
    token = mailed_token(mail, "verify-email")
    with Session() as db:
        db.get(AccountToken, session_hash(token)).expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        db.commit()
    response = accounts.post("/api/auth/verify-email", json={"token": token})
    assert response.status_code in (400, 422), response.text
    assert accounts.get("/api/account").json()["email_verified"] is False


def test_password_reset_is_private_single_use_and_revokes_sessions(accounts, mail):
    from trialdelta.db import AccountToken

    register(accounts)
    old_session = accounts.cookies.get("td_session")
    response = accounts.post("/api/auth/forgot-password", json={"email": " CUSTOMER@EXAMPLE.COM "})
    assert response.status_code == 200, response.text
    token = mailed_token(mail, "reset-password")
    assert_private(response, token)
    with Session() as db:
        saved = db.get(AccountToken, session_hash(token))
        assert saved
        assert datetime.fromisoformat(saved.expires_at) > datetime.now(timezone.utc)
    unknown = accounts.post("/api/auth/forgot-password", json={"email": "missing@example.com"})
    assert unknown.status_code == response.status_code
    assert unknown.json() == response.json()
    # A verification link is never accepted as a reset link.
    verification = mailed_token(mail[:1], "verify-email")
    assert accounts.post("/api/auth/reset-password", json={"token": verification, "new_password": NEW_PASSWORD}).status_code in (400, 422)
    changed = accounts.post("/api/auth/reset-password", json={"token": token, "new_password": NEW_PASSWORD})
    assert changed.status_code == 200, changed.text
    assert_private(changed, token, NEW_PASSWORD)
    assert not accounts.cookies.get("td_session")
    with Session() as db:
        assert db.get(Login, session_hash(old_session)) is None
    assert accounts.post("/api/auth/reset-password", json={"token": token, "new_password": PASSWORD}).status_code in (400, 422)
    assert accounts.post("/api/login", json={"email": "customer@example.com", "password": PASSWORD}).status_code == 401
    sign_in(accounts, password=NEW_PASSWORD)


def test_expired_reset_token_is_rejected_without_changing_password(accounts, mail):
    from trialdelta.db import AccountToken

    register(accounts)
    accounts.post("/api/auth/forgot-password", json={"email": "customer@example.com"})
    token = mailed_token(mail, "reset-password")
    with Session() as db:
        db.get(AccountToken, session_hash(token)).expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        db.commit()
    response = accounts.post("/api/auth/reset-password", json={"token": token, "new_password": NEW_PASSWORD})
    assert response.status_code in (400, 422), response.text
    sign_in(accounts)


def test_missing_email_configuration_fails_honestly_without_account_enumeration(accounts):
    register(accounts)
    existing = accounts.post("/api/auth/forgot-password", json={"email": "customer@example.com"})
    missing = accounts.post("/api/auth/forgot-password", json={"email": "missing@example.com"})
    assert existing.status_code == missing.status_code == 503
    assert existing.json() == missing.json()
    assert accounts.post("/api/account/verification").status_code == 503


def test_additive_database_initialization_preserves_existing_users_and_follows(accounts):
    register(accounts)
    accounts.patch("/api/account", json={"display_name": "Preserved"})
    accounts.post("/api/follows", json={"kind": "target", "value": "MET"})
    with Session() as db:
        user_count = db.scalar(select(func.count()).select_from(User))
        company_count = db.scalar(select(func.count()).select_from(Company))
        follow_count = db.scalar(select(func.count()).select_from(Follow))
    initialize()
    initialize()
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(User)) == user_count
        assert db.scalar(select(func.count()).select_from(Company)) == company_count
        assert db.scalar(select(func.count()).select_from(Follow)) == follow_count
    assert accounts.get("/api/account").json()["display_name"] == "Preserved"


def test_registration_can_be_disabled_without_disabling_login(accounts, monkeypatch):
    register(accounts)
    accounts.post("/api/logout")
    monkeypatch.setattr(auth, "REGISTRATION_ENABLED", False)
    assert accounts.get("/api/auth/config").json()["registration_enabled"] is False
    response = accounts.post("/api/auth/register", json={"email": "new@example.com", "password": PASSWORD})
    assert response.status_code in (403, 404), response.text
    sign_in(accounts)
    with Session() as db:
        assert db.scalar(select(User).where(User.email == "new@example.com")) is None


def test_login_rate_limit_uses_persistent_hashed_database_counters(accounts):
    from trialdelta.db import AuthRateLimit

    for attempt in range(15):
        response = accounts.post("/api/login", json={"email": f"missing{attempt}@example.com", "password": "wrong password"})
        assert response.status_code == 401, response.text
    with Session() as db:
        counters = db.scalars(select(AuthRateLimit)).all()
        assert counters
        assert max(row.attempts for row in counters) >= 15
        for row in counters:
            assert re.fullmatch(r"[0-9a-f]{64}", row.bucket_hash)
    with TestClient(app) as fresh_browser:
        fresh_browser.headers["X-TrialDelta-Client"] = "web"
        response = fresh_browser.post("/api/login", json={"email": "another@example.com", "password": PASSWORD})
        assert response.status_code == 429, response.text
    # Advancing persisted windows restores attempts without restarting the process.
    with Session() as db:
        for row in db.scalars(select(AuthRateLimit)):
            row.window_started_at = 1
        db.commit()
    response = accounts.post("/api/login", json={"email": "another@example.com", "password": PASSWORD})
    assert response.status_code == 401, response.text


def test_mail_failure_does_not_expose_credentials_or_undo_registration(accounts, mail, monkeypatch):
    from trialdelta import accounts as account_routes

    def unavailable(*args, **kwargs):
        raise RuntimeError("smtp://private-credentials@mailer.example")

    monkeypatch.setattr(account_routes, "send_account_email", unavailable)
    response = register(accounts)
    assert response.json()["verification_sent"] is False
    assert_private(response, "private-credentials", PASSWORD)
    assert accounts.get("/api/account").status_code == 200


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("PATCH", "/api/account", {"display_name": "Attempted"}),
        ("POST", "/api/account/password", {"current_password": PASSWORD, "new_password": NEW_PASSWORD}),
        ("POST", "/api/account/verification", None),
        ("POST", "/api/logout-all", None),
        ("POST", "/api/follows", {"kind": "target", "value": "MET"}),
        ("PUT", "/api/digest/preferences", {"enabled": True}),
    ],
)
def test_account_and_preference_mutations_require_authentication(accounts, method, path, payload):
    assert accounts.request(method, path, json=payload).status_code == 401


def test_configured_secure_cookie_is_marked_secure(accounts, monkeypatch):
    monkeypatch.setattr(auth, "SECURE", True)
    response = register(accounts)
    assert "; secure" in response.headers["set-cookie"].lower()


def test_resending_verification_invalidates_earlier_link(accounts, mail):
    register(accounts)
    first = mailed_token(mail, "verify-email")
    response = accounts.post("/api/account/verification")
    assert response.status_code == 200, response.text
    second = mailed_token(mail, "verify-email")
    assert first != second
    assert accounts.post("/api/auth/verify-email", json={"token": first}).status_code in (400, 422)
    assert accounts.post("/api/auth/verify-email", json={"token": second}).status_code == 200


def test_new_reset_request_invalidates_earlier_link(accounts, mail):
    register(accounts)
    accounts.post("/api/auth/forgot-password", json={"email": "customer@example.com"})
    first = mailed_token(mail, "reset-password")
    accounts.post("/api/auth/forgot-password", json={"email": "customer@example.com"})
    second = mailed_token(mail, "reset-password")
    assert first != second
    assert accounts.post("/api/auth/reset-password", json={"token": first, "new_password": NEW_PASSWORD}).status_code in (400, 422)
    assert accounts.post("/api/auth/reset-password", json={"token": second, "new_password": NEW_PASSWORD}).status_code == 200


def test_account_tables_can_be_added_to_existing_customer_database(accounts, tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect
    from trialdelta import db as database

    legacy = create_engine("sqlite:///" + str(tmp_path / "legacy.db").replace("\\", "/"))
    try:
        # Reproduce the pre-account schema: the original users and follows remain intact.
        for table in (User.__table__, Company.__table__, Follow.__table__):
            table.create(legacy)
        with legacy.begin() as connection:
            connection.execute(User.__table__.insert().values(id=42, email="existing@example.com", password_hash="existing-hash", is_admin=False, digest_enabled=True))
            connection.execute(Company.__table__.insert().values(id=9, ticker="OLD", name="Existing company", sponsor="Existing sponsor"))
            connection.execute(Follow.__table__.insert().values(id=10, user_id=42, kind="target", value="MET"))
        assert "td_account_profiles" not in inspect(legacy).get_table_names()
        monkeypatch.setattr(database, "engine", legacy)
        database.initialize()
        database.initialize()
        assert {"td_account_profiles", "td_account_tokens", "td_auth_rate_limits"} <= set(inspect(legacy).get_table_names())
        with legacy.connect() as connection:
            original = connection.execute(select(User.__table__)).mappings().one()
            assert original["id"] == 42
            assert original["email"] == "existing@example.com"
            assert original["password_hash"] == "existing-hash"
            assert original["digest_enabled"] is True
            assert connection.scalar(select(func.count()).select_from(Company)) == 1
            follow = connection.execute(select(Follow.__table__)).mappings().one()
            assert follow["user_id"] == 42 and follow["value"] == "MET"
    finally:
        legacy.dispose()


@pytest.mark.parametrize(
    "path,payload,secret",
    [
        ("/api/auth/register", {"email": "a@example.com", "password": "private-value"}, "private-value"),
        ("/api/auth/verify-email", {"token": "private-token"}, "private-token"),
        ("/api/auth/reset-password", {"token": "private-token", "new_password": NEW_PASSWORD}, "private-token"),
        ("/api/auth/reset-password", {"token": "x" * 48, "new_password": "private-value"}, "private-value"),
    ],
)
def test_validation_errors_do_not_echo_credentials_or_tokens(accounts, path, payload, secret):
    response = accounts.post(path, json=payload)
    assert response.status_code == 422
    assert_private(response, secret)
    for error in response.json()["detail"]:
        assert "input" not in error and "ctx" not in error


def test_account_login_limit_applies_before_broader_ip_limit(accounts):
    for _ in range(10):
        response = accounts.post("/api/login", json={"email": "same-account@example.com", "password": PASSWORD})
        assert response.status_code == 401
    response = accounts.post("/api/login", json={"email": " SAME-ACCOUNT@EXAMPLE.COM ", "password": PASSWORD})
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    assert accounts.post("/api/login", json={"email": "other-account@example.com", "password": PASSWORD}).status_code == 401


def test_concurrent_requests_cannot_overspend_persistent_rate_limit(accounts):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from fastapi import HTTPException
    from trialdelta.accounts import throttle
    from trialdelta.db import AuthRateLimit

    request = SimpleNamespace(client=SimpleNamespace(host="testclient"))

    def attempt(_):
        try:
            throttle(request, "concurrent-security-test", limit=5)
            return 200
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=8) as workers:
        statuses = list(workers.map(attempt, range(20)))
    assert statuses.count(200) == 5
    assert statuses.count(429) == 15
    digest = session_hash("concurrent-security-test:ip:testclient")
    with Session() as db:
        assert db.get(AuthRateLimit, digest).attempts == 20


def test_login_cannot_create_session_after_concurrent_password_reset(accounts, monkeypatch):
    from sqlalchemy import delete

    user = register(accounts).json()["user"]
    accounts.post("/api/logout")
    original_verify = auth.verify_login_password
    replacement_hash = auth.password_hash(NEW_PASSWORD)

    def verify_then_reset(password, candidate):
        matched = original_verify(password, candidate)
        with Session() as db:
            db.get(User, user["id"]).password_hash = replacement_hash
            db.execute(delete(Login).where(Login.user_id == user["id"]))
            db.commit()
        return matched

    monkeypatch.setattr(auth, "verify_login_password", verify_then_reset)
    response = accounts.post("/api/login", json={"email": user["email"], "password": PASSWORD})
    assert response.status_code == 401, response.text
    assert not accounts.cookies.get("td_session")
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(Login).where(Login.user_id == user["id"])) == 0
        assert db.get(User, user["id"]).password_hash == replacement_hash


def test_password_change_cannot_overwrite_concurrent_password_reset(accounts, monkeypatch):
    from sqlalchemy import delete

    user = register(accounts).json()["user"]
    original_verify = auth.verify_password
    replacement_hash = auth.password_hash("Password from a concurrent reset 789!")

    def verify_then_reset(password, stored):
        matched = original_verify(password, stored)
        with Session() as db:
            db.get(User, user["id"]).password_hash = replacement_hash
            db.execute(delete(Login).where(Login.user_id == user["id"]))
            db.commit()
        return matched

    monkeypatch.setattr(auth, "verify_password", verify_then_reset)
    response = accounts.post("/api/account/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert response.status_code == 400, response.text
    assert accounts.get("/api/me").status_code == 401
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(Login).where(Login.user_id == user["id"])) == 0
        assert db.get(User, user["id"]).password_hash == replacement_hash
