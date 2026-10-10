"""Customer account routes. Secrets are stored only as password/token hashes."""

import hashlib
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import case, delete, select, update
from sqlalchemy.exc import IntegrityError

from . import auth
from .account_mail import email_delivery_available, send_account_email
from .db import AccountProfile, AccountToken, AuthRateLimit, Login, Session, User, get_db, now

router = APIRouter()
logger = logging.getLogger(__name__)
EMAIL_UNAVAILABLE = "Email delivery is not available. Please contact the site operator."
RECOVERY_MESSAGE = "If an account matches that email, a password reset link will be sent."
TOKEN_INVALID = "This link is invalid or has expired. Request a new link."


class AccountBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(AccountBody):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def email_valid(cls, value):
        return auth.normalize_email(value)


class Registration(Credentials):
    password: str = Field(min_length=auth.PASSWORD_MIN_LENGTH, max_length=256)
    display_name: str = Field(default="", max_length=100)

    @field_validator("password")
    @classmethod
    def meaningful_password(cls, value):
        if not value.strip():
            raise ValueError("Choose a password containing more than spaces")
        return value

    @field_validator("display_name")
    @classmethod
    def name_valid(cls, value):
        return clean_display_name(value)


def clean_display_name(value):
    value = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Display name cannot contain control characters")
    return value


class ProfileUpdate(AccountBody):
    display_name: str = Field(default="", max_length=100)
    digest_enabled: bool = False

    @field_validator("display_name")
    @classmethod
    def name_valid(cls, value):
        return clean_display_name(value)


class PasswordChange(AccountBody):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=auth.PASSWORD_MIN_LENGTH, max_length=256)

    @field_validator("new_password")
    @classmethod
    def meaningful_password(cls, value):
        if not value.strip():
            raise ValueError("Choose a password containing more than spaces")
        return value


class EmailRequest(AccountBody):
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def email_valid(cls, value):
        return auth.normalize_email(value)


class TokenRequest(AccountBody):
    token: str = Field(min_length=16, max_length=256)


class PasswordReset(TokenRequest):
    new_password: str = Field(min_length=auth.PASSWORD_MIN_LENGTH, max_length=256)

    @field_validator("new_password")
    @classmethod
    def meaningful_password(cls, value):
        if not value.strip():
            raise ValueError("Choose a password containing more than spaces")
        return value


def throttle(request, scope, identity=None, *, limit=15, identity_limit=10, window=900):
    """Atomic database counters survive restarts and work across application workers.

    Forwarded headers are deliberately ignored; only the ASGI client address is used.
    Each upsert owns a short transaction, serializing parallel spends of one bucket.
    """
    host = request.client.host if request.client else "unknown"
    buckets = [(f"{scope}:ip:{host}", limit)]
    if identity is not None:
        buckets.append((f"{scope}:identity:{identity}", identity_limit))
    timestamp = int(time.time())
    for key, maximum in buckets:
        digest = hashlib.sha256(key.encode()).hexdigest()
        with Session() as db:
            expired = AuthRateLimit.window_started_at <= timestamp - window
            values = {
                "attempts": case((expired, 1), else_=AuthRateLimit.attempts + 1),
                "window_started_at": case((expired, timestamp), else_=AuthRateLimit.window_started_at),
            }
            dialect = db.get_bind().dialect.name
            if dialect == "sqlite":
                from sqlalchemy.dialects.sqlite import insert
                statement = insert(AuthRateLimit).values(
                    bucket_hash=digest, window_started_at=timestamp, attempts=1
                ).on_conflict_do_update(index_elements=[AuthRateLimit.bucket_hash], set_=values)
                db.execute(statement)
            elif dialect == "mysql":
                from sqlalchemy.dialects.mysql import insert
                # MySQL evaluates assignments in order: update the count while
                # the original window timestamp is still available.
                statement = insert(AuthRateLimit).values(
                    bucket_hash=digest, window_started_at=timestamp, attempts=1
                ).on_duplicate_key_update(list(values.items()))
                db.execute(statement)
            else:
                try:
                    with db.begin_nested():
                        db.add(AuthRateLimit(bucket_hash=digest, window_started_at=timestamp, attempts=0))
                        db.flush()
                except IntegrityError:
                    pass
                db.execute(update(AuthRateLimit).where(AuthRateLimit.bucket_hash == digest).values(**values))
            attempts = db.scalar(select(AuthRateLimit.attempts).where(AuthRateLimit.bucket_hash == digest))
            db.commit()
        if attempts > maximum:
            raise HTTPException(
                429, "Too many attempts. Please try again later.",
                headers={"Retry-After": str(window)},
            )
    # Cleanup owns a separate transaction: an InnoDB range/gap lock during
    # pruning must never be held while another transaction upserts a bucket.
    if secrets.randbelow(100) == 0:
        with Session() as db:
            db.execute(delete(AuthRateLimit).where(AuthRateLimit.window_started_at < timestamp - 86400))
            db.commit()


def profile_for(db, user, *, create=False):
    profile = db.get(AccountProfile, user.id)
    if not profile and create:
        try:
            with db.begin_nested():
                profile = AccountProfile(user_id=user.id, display_name="")
                db.add(profile)
                db.flush()
        except IntegrityError:
            profile = db.scalar(select(AccountProfile).where(AccountProfile.user_id == user.id).with_for_update())
    return profile


def account_data(db, user):
    profile = profile_for(db, user)
    return {
        "id": user.id,
        "email": user.email,
        "display_name": profile.display_name if profile else "",
        "is_admin": bool(user.is_admin),
        "mode": auth.MODE,
        "digest_enabled": bool(user.digest_enabled),
        "created_at": user.created_at,
        "email_verified": bool(profile and profile.email_verified_at),
        "email_verified_at": profile.email_verified_at if profile else None,
        "email_delivery_available": email_delivery_available(),
        "auth_required": auth.authentication_required(),
    }


def locked_user(db, user_id):
    return db.scalar(
        select(User).where(User.id == user_id).with_for_update()
        .execution_options(populate_existing=True)
    )


def retain_password_lock(db, user):
    """Reject stale password checks and hold a writer lock until session commit.

    SQLite ignores SELECT FOR UPDATE. This conditional no-op write ensures a
    concurrent password reset cannot commit between verification and a new
    session, and also works with MySQL's matched-row UPDATE semantics.
    """
    result = db.execute(
        update(User).where(User.id == user.id, User.password_hash == user.password_hash)
        .values(password_hash=User.password_hash)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def customer_user(user):
    if not user or user.email == auth.LOCAL_EMAIL:
        raise HTTPException(403, "Sign in with a customer account to use this feature")


def _send_token_email(db, user, purpose):
    """Issue one active link per purpose; never return or log its plaintext."""
    token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db.execute(delete(AccountToken).where(AccountToken.expires_at <= now()))
    db.execute(delete(AccountToken).where(AccountToken.user_id == user.id, AccountToken.purpose == purpose))
    hours = 24 if purpose == "verify_email" else 1
    db.add(AccountToken(
        token_hash=token_hash, user_id=user.id, purpose=purpose,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds"),
    ))
    db.commit()
    action = "verify-email" if purpose == "verify_email" else "reset-password"
    link = f"{auth.ORIGIN}/#{action}={token}"
    if purpose == "verify_email":
        subject = "Verify your TrialDelta email address"
        body = f"Confirm this email address for your TrialDelta account:\n\n{link}\n\nThis link expires in 24 hours. If you did not create this account, you can ignore this email."
    else:
        subject = "Reset your TrialDelta password"
        body = f"Use this link to choose a new TrialDelta password:\n\n{link}\n\nThis link expires in 1 hour. If you did not request a password reset, you can ignore this email. Your password has not changed."
    try:
        send_account_email(user.email, subject, body)
        return True
    except Exception as exc:
        # An uncertain delivery is safe to invalidate; a customer can request a new link.
        db.execute(delete(AccountToken).where(AccountToken.token_hash == token_hash))
        db.commit()
        logger.warning("Account email delivery failed (%s)", type(exc).__name__)
        return False


def _send_recovery_email(email):
    # This task is queued for known and unknown addresses alike, after the response.
    try:
        with Session() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user and user.email != auth.LOCAL_EMAIL:
                _send_token_email(db, user, "reset_password")
    except Exception as exc:
        logger.warning("Account recovery could not be delivered (%s)", type(exc).__name__)


def valid_token(db, token, purpose):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = db.get(AccountToken, token_hash)
    if not row or row.purpose != purpose or row.expires_at <= now():
        raise HTTPException(400, TOKEN_INVALID)
    user = locked_user(db, row.user_id)
    if not user or user.email == auth.LOCAL_EMAIL:
        raise HTTPException(400, TOKEN_INVALID)
    # Delete is conditional and transactional, so concurrent submissions cannot both win.
    consumed = db.execute(delete(AccountToken).where(
        AccountToken.token_hash == token_hash,
        AccountToken.purpose == purpose,
        AccountToken.expires_at > now(),
    ))
    if consumed.rowcount != 1:
        raise HTTPException(400, TOKEN_INVALID)
    return user


@router.get("/api/auth/config")
def auth_config():
    return {
        "portfolio_snapshot_date": os.getenv("PORTFOLIO_SNAPSHOT_DATE", ""),
        "auth_required": auth.authentication_required(),
        "registration_enabled": auth.REGISTRATION_ENABLED,
        "public_browsing_enabled": auth.PUBLIC_BROWSING_ENABLED,
        "email_delivery_available": email_delivery_available(),
        "password_min_length": auth.PASSWORD_MIN_LENGTH,
    }


@router.get("/api/me")
@router.get("/api/account")
def account(user=Depends(auth.current_user), db=Depends(get_db)):
    return account_data(db, user)


@router.post("/api/auth/register", status_code=201)
def register(body: Registration, request: Request, response: Response, db=Depends(get_db)):
    if not auth.REGISTRATION_ENABLED:
        raise HTTPException(403, "Account registration is currently closed")
    throttle(request, "register", limit=10)
    hashed_password = auth.password_hash(body.password)
    if body.email == auth.LOCAL_EMAIL:
        raise HTTPException(409, "Unable to create this account. Try signing in or resetting your password.")
    user = User(email=body.email, password_hash=hashed_password, is_admin=False, digest_enabled=False)
    try:
        db.add(user)
        db.flush()
        db.add(AccountProfile(user_id=user.id, display_name=body.display_name))
        token = auth.new_session(db, user, commit=False)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Unable to create this account. Try signing in or resetting your password.") from None
    auth.set_session_cookie(response, token)
    verification_sent = email_delivery_available() and _send_token_email(db, user, "verify_email")
    return {
        "ok": True,
        "user": account_data(db, user),
        "verification_sent": bool(verification_sent),
        "email_delivery_available": email_delivery_available(),
    }


@router.post("/api/login")
def login(body: Credentials, request: Request, response: Response, db=Depends(get_db)):
    throttle(request, "login", body.email)
    user = db.scalar(select(User).where(User.email == body.email).with_for_update())
    if not auth.verify_login_password(body.password, user) or not retain_password_lock(db, user):
        raise HTTPException(401, "Email or password is incorrect")
    token = auth.new_session(db, user)
    auth.set_session_cookie(response, token)
    return {"ok": True, "user": account_data(db, user)}


@router.post("/api/logout")
def logout(request: Request, response: Response, db=Depends(get_db)):
    token = request.cookies.get("td_session", "")
    db.execute(delete(Login).where(Login.token_hash == hashlib.sha256(token.encode()).hexdigest()))
    db.commit()
    auth.clear_session_cookie(response)
    return {"ok": True}


@router.post("/api/logout-all")
def logout_all(response: Response, user=Depends(auth.current_user), db=Depends(get_db)):
    customer_user(user)
    locked_user(db, user.id)
    db.execute(delete(Login).where(Login.user_id == user.id))
    db.commit()
    auth.clear_session_cookie(response)
    return {"ok": True}


@router.patch("/api/account")
def update_account(body: ProfileUpdate, user=Depends(auth.current_user), db=Depends(get_db)):
    row = locked_user(db, user.id)
    profile = profile_for(db, row, create=True)
    if "display_name" in body.model_fields_set:
        profile.display_name = body.display_name
    if "digest_enabled" in body.model_fields_set:
        row.digest_enabled = body.digest_enabled
    db.commit()
    return account_data(db, row)


@router.post("/api/account/password")
def change_password(body: PasswordChange, request: Request, response: Response, user=Depends(auth.current_user), db=Depends(get_db)):
    customer_user(user)
    throttle(request, "change_password", str(user.id), limit=15, identity_limit=10)
    row = locked_user(db, user.id)
    if not auth.verify_password(body.current_password, row.password_hash) or not retain_password_lock(db, row):
        raise HTTPException(400, "Current password is incorrect")
    row.password_hash = auth.password_hash(body.new_password)
    db.execute(delete(Login).where(Login.user_id == row.id))
    db.execute(delete(AccountToken).where(AccountToken.user_id == row.id, AccountToken.purpose == "reset_password"))
    token = auth.new_session(db, row, commit=False)
    db.commit()
    auth.set_session_cookie(response, token)
    return {"ok": True}


@router.post("/api/account/verification")
def request_verification(request: Request, user=Depends(auth.current_user), db=Depends(get_db)):
    customer_user(user)
    throttle(request, "verification_mail", str(user.id), limit=10, identity_limit=3, window=3600)
    profile = profile_for(db, user)
    if profile and profile.email_verified_at:
        return {"ok": True, "message": "Your email address is already verified."}
    if not email_delivery_available():
        raise HTTPException(503, EMAIL_UNAVAILABLE)
    if not _send_token_email(db, user, "verify_email"):
        raise HTTPException(503, "We could not send the verification email. Please try again later.")
    return {"ok": True, "message": "A verification link has been sent to your email address."}


@router.post("/api/auth/verify-email")
def verify_email(body: TokenRequest, request: Request, db=Depends(get_db)):
    throttle(request, "verify_email", limit=15)
    user = valid_token(db, body.token, "verify_email")
    profile_for(db, user, create=True).email_verified_at = now()
    db.commit()
    return {"ok": True}


@router.post("/api/auth/forgot-password")
def forgot_password(body: EmailRequest, request: Request, background_tasks: BackgroundTasks):
    throttle(request, "forgot_password", body.email, limit=10, identity_limit=3, window=3600)
    if not email_delivery_available():
        # This is an environment-level response, identical for every address.
        raise HTTPException(503, EMAIL_UNAVAILABLE)
    background_tasks.add_task(_send_recovery_email, body.email)
    return {"ok": True, "message": RECOVERY_MESSAGE, "email_delivery_available": True}


@router.post("/api/auth/reset-password")
def reset_password(body: PasswordReset, request: Request, response: Response, db=Depends(get_db)):
    throttle(request, "reset_password", limit=15)
    # Derive before token lookup, giving invalid and valid submissions comparable work.
    hashed_password = auth.password_hash(body.new_password)
    user = valid_token(db, body.token, "reset_password")
    user.password_hash = hashed_password
    db.execute(delete(Login).where(Login.user_id == user.id))
    db.execute(delete(AccountToken).where(AccountToken.user_id == user.id, AccountToken.purpose == "reset_password"))
    db.commit()
    auth.clear_session_cookie(response)
    return {"ok": True}


@router.post("/api/users")
def create_user(body: Registration, user=Depends(auth.admin), db=Depends(get_db)):
    """Compatibility route for administrator-created reader accounts."""
    row = User(email=body.email, password_hash=auth.password_hash(body.password), is_admin=False, digest_enabled=False)
    if body.email == auth.LOCAL_EMAIL:
        raise HTTPException(409, "Account already exists")
    try:
        db.add(row)
        db.flush()
        db.add(AccountProfile(user_id=row.id, display_name=body.display_name))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Account already exists") from None
    return {"id": row.id, "email": row.email}
