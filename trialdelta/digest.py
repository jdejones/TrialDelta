import os
import smtplib
import ssl
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from sqlalchemy import select, func
from .db import Session, User, AccountProfile, Follow, DigestDelivery, now
from .services import published_changes


def build_digest(db, user, day):
    changes = published_changes(db, user.id, followed=True, day=day)
    return {
        "day": day,
        "changes": changes,
        "follow_count": db.scalar(
            select(func.count()).select_from(Follow).where(Follow.user_id == user.id)
        ),
        "note": "Registry changes detected on this date (UTC) and company evidence approved on this date enter the brief. No update does not establish that a company or trial is unchanged. Coverage is limited to captured sources.",
    }


def digest_text(digest):
    lines = [
        f'TrialDelta | {digest["day"]}',
        f'{len(digest["changes"])} changes',
        "",
    ]
    origin = os.getenv("APP_ORIGIN", "http://127.0.0.1:8765")
    for c in digest["changes"]:
        lines.extend(
            [
                f'{c["ticker"]} | {c["title"]}',
                f'Previous: {c["before_value"]}',
                f'Current: {c["after_value"]}',
                f'Evidence: {origin}/?change={c["id"]}',
                (
                    f'Registry: https://clinicaltrials.gov/study/{c["study_id"]}'
                    if c["study_id"]
                    else "Company-level statement"
                ),
                "",
            ]
        )
    lines.append(digest["note"])
    return "\n".join(lines)


def deliver_daily(day=None):
    """Explicit worker option only; send digests to verified, opted-in users."""
    day = day or (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    host = os.environ.get("SMTP_HOST")
    sender = os.environ.get("SMTP_FROM")
    if not host or not sender:
        raise RuntimeError("SMTP_HOST and SMTP_FROM are required for email delivery")
    sent = 0
    with Session() as db:
        users = db.scalars(select(User).where(User.digest_enabled == True)).all()
        for user in users:
            if user.email.endswith(".local"):
                continue
            profile = db.get(AccountProfile, user.id)
            if not profile or not profile.email_verified_at:
                continue
            if db.scalar(
                select(DigestDelivery).where(
                    DigestDelivery.user_id == user.id, DigestDelivery.day == day
                )
            ):
                continue
            digest = build_digest(db, user, day)
            if not digest["changes"]:
                continue
            # Reserve before delivery to prevent duplicate mail after a process crash.
            # An uncertain reservation needs operator reconciliation, never an automatic retry.
            delivery = DigestDelivery(user_id=user.id, day=day)
            db.add(delivery)
            db.commit()
            msg = EmailMessage()
            msg["From"] = sender
            msg["To"] = user.email
            msg["Subject"] = (
                f'TrialDelta | {len(digest["changes"])} changes | {day}'
            )
            msg.set_content(digest_text(digest))
            sending = False
            try:
                with smtplib.SMTP(
                    host, int(os.getenv("SMTP_PORT", "587")), timeout=30
                ) as smtp:
                    smtp.starttls(context=ssl.create_default_context())
                    if os.getenv("SMTP_USER"):
                        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
                    sending = True
                    smtp.send_message(msg)
                delivery.sent_at = now()
                db.commit()
                sent += 1
            except Exception as exc:
                if not sending:
                    db.delete(delivery)
                    db.commit()  # Known pre-send failure is safe to retry.
                print(
                    f"Email delivery failed for account {user.id} ({type(exc).__name__}); "
                    + (
                        "outcome uncertain, operator reconciliation required."
                        if sending
                        else "safe to retry."
                    ),
                    flush=True,
                )
    return sent
