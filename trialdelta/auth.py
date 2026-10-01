"""Password and persistent-session primitives shared by the web and desktop apps."""

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, select

from .db import AccountProfile, Login, Session, User, get_db, now

MODE = os.getenv("APP_MODE", "local")
ORIGIN = os.getenv("APP_ORIGIN", "http://127.0.0.1:8765").rstrip("/")
SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "true").lower() != "false"
# Local development permits research browsing; hosted access stays gated by default.
PUBLIC_BROWSING_ENABLED = os.getenv("PUBLIC_BROWSING_ENABLED", "true" if MODE == "local" else "false").lower() == "true"
REGISTRATION_ENABLED = os.getenv("REGISTRATION_ENABLED", "true").lower() != "false"
LOCAL_EMAIL = "local@trialdelta.local"
SESSION_SECONDS = 7 * 24 * 60 * 60
PASSWORD_MIN_LENGTH = 14


def authentication_required():
    # Hosted deployments can never opt into the loopback development shortcut.
    return MODE != "local" or AUTH_REQUIRED


def normalize_email(value):
    value = value.strip().lower()
    try:
        local, domain = value.rsplit("@", 1)
        domain = domain.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        raise ValueError("Enter a valid email address") from None
    labels = domain.split(".")
    if (
        not 1 <= len(local) <= 64
        or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or len(labels) < 2
        or len(labels[-1]) < 2
        or labels[-1].isdigit()
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        )
        or len(local + "@" + domain) > 254
    ):
        raise ValueError("Enter a valid email address")
    return local + "@" + domain


def password_hash(password):
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000).hex()
    return f"pbkdf2_sha256${salt}${key}"


# An unknown login still performs the same expensive password derivation.
_DUMMY_PASSWORD_HASH = (
    "pbkdf2_sha256$00000000000000000000000000000000$" + "0" * 64
)


def verify_password(password, stored):
    try:
        algorithm, salt, expected = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), 600000
        ).hex()
        return hmac.compare_digest(actual, expected)
    except (AttributeError, TypeError, ValueError):
        return False


def verify_login_password(password, user):
    stored = (
        user.password_hash
        if user is not None and user.email != LOCAL_EMAIL
        else _DUMMY_PASSWORD_HASH
    )
    matches = verify_password(password, stored)
    return bool(user is not None and user.email != LOCAL_EMAIL and matches)


def bootstrap():
    if MODE not in ("local", "hosted"):
        raise RuntimeError("APP_MODE must be local or hosted")
    email, password = os.getenv("ADMIN_EMAIL", "").lower().strip(), os.getenv(
        "ADMIN_PASSWORD", ""
    )
    if MODE == "hosted" and (not ORIGIN.startswith("https://") or not SECURE):
        raise RuntimeError(
            "Hosted mode requires HTTPS APP_ORIGIN and COOKIE_SECURE=true"
        )
    with Session() as db:
        if MODE == "local":
            if not db.scalar(select(User).where(User.email == LOCAL_EMAIL)):
                db.add(
                    User(
                        email=LOCAL_EMAIL,
                        password_hash=password_hash(secrets.token_urlsafe(40)),
                        is_admin=True,
                    )
                )
        elif not db.scalar(
            select(User).where(User.is_admin == True, User.email != LOCAL_EMAIL)
        ):
            try:
                email = normalize_email(email)
            except ValueError:
                email = ""
            if (
                not email
                or email == LOCAL_EMAIL
                or len(password) < PASSWORD_MIN_LENGTH
                or len(password) > 256
                or not password.strip()
                or password.startswith("REPLACE_")
            ):
                raise RuntimeError(
                    "Set ADMIN_EMAIL and ADMIN_PASSWORD (14+ characters) for first hosted start"
                )
            operator = User(email=email, password_hash=password_hash(password), is_admin=True)
            db.add(operator)
            db.flush()
            # The deployment operator supplies this address out of band.
            db.add(AccountProfile(user_id=operator.id, email_verified_at=now()))
        db.commit()


def optional_user(request: Request, db=Depends(get_db)):
    if MODE == "local":
        if request.client and request.client.host not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(
                403,
                "Local mode only accepts loopback clients. Configure hosted mode for remote access.",
            )
        if not authentication_required():
            user = db.scalar(select(User).where(User.email == LOCAL_EMAIL))
            if user:
                return user
    token = request.cookies.get("td_session", "")
    if not token or len(token) > 256:
        return None
    session = db.get(Login, hashlib.sha256(token.encode()).hexdigest())
    if not session or session.expires_at <= now():
        return None
    user = db.get(User, session.user_id)
    if not user or user.email == LOCAL_EMAIL:
        return None
    return user


def current_user(request: Request, db=Depends(get_db)):
    user = optional_user(request, db)
    if user is None:
        raise HTTPException(401, "Please sign in")
    return user


def browsing_user(user=Depends(optional_user)):
    """Shared visitor policy for published research reads; grants no write access."""
    if user is None and not PUBLIC_BROWSING_ENABLED:
        raise HTTPException(401, "Please sign in")
    return user


def admin(user=Depends(current_user)):
    if not user or not user.is_admin:
        raise HTTPException(403, "Reviewer access required")
    return user


def new_session(db, user, *, commit=True):
    token = secrets.token_urlsafe(48)
    db.execute(delete(Login).where(Login.expires_at <= now()))
    db.add(
        Login(
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            user_id=user.id,
            expires_at=(datetime.now(timezone.utc) + timedelta(seconds=SESSION_SECONDS)).isoformat(timespec="seconds"),
        )
    )
    if commit:
        db.commit()
    return token


def set_session_cookie(response, token):
    response.set_cookie(
        "td_session", token, httponly=True, secure=SECURE, samesite="lax",
        max_age=SESSION_SECONDS, path="/",
    )


def clear_session_cookie(response):
    response.delete_cookie(
        "td_session", path="/", httponly=True, secure=SECURE, samesite="lax"
    )
