from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from trialdelta.db import User, AccountProfile, Company, Change, Follow, DigestDelivery, now
from trialdelta.digest import build_digest, deliver_daily


def test_delayed_review_enters_approval_day(db):
    user = db.scalar(select(User))
    company = db.scalar(select(Company))
    db.add(Follow(user_id=user.id, kind="company", value=str(company.id)))
    db.add(
        Change(
            company_id=company.id,
            category="timeline",
            title="Guidance changed",
            source="company",
            before_value="H1",
            after_value="H2",
            detected_at="2026-01-01T00:00:00+00:00",
            reviewed_at="2026-09-20T00:00:00+00:00",
            review_status="approved",
        )
    )
    db.commit()
    assert len(build_digest(db, user, "2026-09-20")["changes"]) == 1
    assert build_digest(db, user, "2026-01-01")["changes"] == []


def test_daily_delivery_previous_utc_day_and_retry_before_send(db, monkeypatch):
    import trialdelta.digest as digest

    user = db.scalar(select(User))
    user.email = "reader@example.com"
    user.digest_enabled = True
    profile = db.get(AccountProfile, user.id)
    if profile is None:
        profile = AccountProfile(user_id=user.id)
        db.add(profile)
    profile.email_verified_at = now()
    company = db.scalar(select(Company))
    day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    db.add(Follow(user_id=user.id, kind="company", value=str(company.id)))
    db.add(
        Change(
            company_id=company.id,
            category="timeline",
            title="Guidance changed",
            source="company",
            reviewed_at=day + "T20:00:00+00:00",
            review_status="approved",
        )
    )
    db.commit()
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "alerts@example.com")
    monkeypatch.setattr(
        digest.smtplib,
        "SMTP",
        lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("offline")),
    )
    assert deliver_daily() == 0
    assert db.scalar(select(DigestDelivery)) is None
    messages = []

    class SMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def starttls(self, **kw):
            pass

        def send_message(self, msg):
            messages.append(msg)

    monkeypatch.setattr(digest.smtplib, "SMTP", SMTP)
    assert deliver_daily() == 1
    assert day in messages[0]["Subject"]
    assert deliver_daily() == 0
    assert len(messages) == 1


def test_unverified_opted_in_accounts_are_not_emailed(db, monkeypatch):
    import trialdelta.digest as digest

    user = db.scalar(select(User))
    user.email = "unverified@example.com"
    user.digest_enabled = True
    profile = db.get(AccountProfile, user.id)
    if profile is not None:
        profile.email_verified_at = None
    db.commit()
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "alerts@example.com")

    def should_not_send(*args, **kwargs):
        raise AssertionError("Unverified accounts must never reach email delivery")

    monkeypatch.setattr(digest, "build_digest", should_not_send)
    monkeypatch.setattr(digest.smtplib, "SMTP", should_not_send)
    assert deliver_daily() == 0
    assert db.scalar(select(DigestDelivery)) is None


def test_registry_digest_uses_detection_day_without_review(db, raw):
    from copy import deepcopy

    from trialdelta.digest import digest_text
    from trialdelta.registry import ingest

    user = db.scalar(select(User))
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    db.add(Follow(user_id=user.id, kind="company", value=str(company.id)))
    ingest(db, company, raw)
    updated = deepcopy(raw)
    updated["protocolSection"]["designModule"]["enrollmentInfo"]["count"] = 160
    ingest(db, company, updated)
    event = db.scalar(select(Change))
    event.detected_at = "2026-09-20T12:00:00+00:00"
    db.commit()

    for status in ["automatic", "pending", "approved"]:
        event.review_status = status
        event.reviewed_at = "2026-09-22T12:00:00+00:00" if status == "approved" else None
        db.commit()
        digest = build_digest(db, user, "2026-09-20")
        assert [row["id"] for row in digest["changes"]] == [event.id]
        assert build_digest(db, user, "2026-09-22")["changes"] == []
        text = digest_text(digest)
        assert "100 participants" in text and "160 participants" in text
        assert "reviewed changes" not in text
    event.review_status = "rejected"
    db.commit()
    assert build_digest(db, user, "2026-09-20")["changes"] == []
