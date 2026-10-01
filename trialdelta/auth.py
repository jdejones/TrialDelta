import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from .db import User, Login, Session, get_db

MODE = os.getenv("APP_MODE", "local")
ORIGIN = os.getenv("APP_ORIGIN", "http://127.0.0.1:8765").rstrip("/")
SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"


def password_hash(password):
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600000).hex()
    return f"pbkdf2_sha256${salt}${key}"


def verify_password(password, stored):
    try:
        _, salt, expected = stored.split("$")
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), 600000
        ).hex()
        return hmac.compare_digest(actual, expected)
    except ValueError:
        return False


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
            if not db.scalar(
                select(User).where(User.email == "local@trialdelta.local")
            ):
                db.add(
                    User(
                        email="local@trialdelta.local",
                        password_hash=password_hash(secrets.token_urlsafe(40)),
                        is_admin=True,
                    )
                )
        elif not db.scalar(
            select(User).where(
                User.is_admin == True, User.email != "local@trialdelta.local"
            )
        ):
            if (
                "@" not in email
                or len(password) < 14
                or password.startswith("REPLACE_")
            ):
                raise RuntimeError(
                    "Set ADMIN_EMAIL and ADMIN_PASSWORD (14+ characters) for first hosted start"
                )
            db.add(
                User(email=email, password_hash=password_hash(password), is_admin=True)
            )
        db.commit()


def current_user(request: Request, db=Depends(get_db)):
    if MODE == "local":
        if request.client and request.client.host not in (
            "127.0.0.1",
            "::1",
            "testclient",
        ):
            raise HTTPException(
                403,
                "Local mode only accepts loopback clients. Configure hosted mode for remote access.",
            )
        return db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    token = request.cookies.get("td_session", "")
    session = db.get(Login, hashlib.sha256(token.encode()).hexdigest())
    if not session or session.expires_at <= datetime.now(timezone.utc).isoformat():
        raise HTTPException(401, "Please sign in")
    user = db.get(User, session.user_id)
    if not user or user.email == "local@trialdelta.local":
        raise HTTPException(401, "Please sign in")
    return user


def admin(user=Depends(current_user)):
    if not user or not user.is_admin:
        raise HTTPException(403, "Reviewer access required")
    return user


def new_session(db, user):
    token = secrets.token_urlsafe(48)
    db.add(
        Login(
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            user_id=user.id,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        )
    )
    db.commit()
    return token
