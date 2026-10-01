"""Registry comparisons publish with evidence and respect each reader's follows."""

from copy import deepcopy

import pytest
from sqlalchemy import select

from trialdelta.app import app
from trialdelta.auth import browsing_user, current_user
from trialdelta.db import Change, Company, Follow, Snapshot, User
from trialdelta.registry import ingest


def registry_events(db, raw, company, study_id="NCT01234567"):
    baseline = deepcopy(raw)
    baseline["protocolSection"]["identificationModule"]["nctId"] = study_id
    assert ingest(db, company, baseline) == 0
    updated = deepcopy(baseline)
    updated["protocolSection"]["designModule"]["enrollmentInfo"]["count"] = 160
    updated["protocolSection"]["statusModule"]["overallStatus"] = "COMPLETED"
    assert ingest(db, company, updated) == 2
    return db.scalars(select(Change).where(Change.study_id == study_id).order_by(Change.id)).all()


def reader(db, email="feed-reader@example.com"):
    user = User(email=email, password_hash="unused", is_admin=False)
    db.add(user)
    db.flush()
    return user


@pytest.mark.parametrize("status", ["automatic", "pending", "approved"])
def test_registry_feed_and_detail_are_visible_without_review(client, db, raw, status):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    changes = registry_events(db, raw, company)
    for change in changes:
        change.review_status = status
    user = reader(db)
    db.add(Follow(user_id=user.id, kind="company", value=str(company.id)))
    db.commit()
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[browsing_user] = lambda: user

    response = client.get("/api/changes")
    assert response.status_code == 200
    assert {row["id"] for row in response.json()} == {row.id for row in changes}
    event = next(row for row in response.json() if row["category"] == "enrollment")
    assert event["review_status"] == status
    assert event["reviewed"] is False
    assert event["reviewed_at"] is None
    detail = client.get(f'/api/changes/{event["id"]}')
    assert detail.status_code == 200
    evidence = detail.json()["evidence"]
    assert [item["label"] for item in evidence] == ["Previous", "Current"]
    assert all(item["source_type"] == "registry" for item in evidence)
    assert all(item["url"] == "https://clinicaltrials.gov/study/NCT01234567" for item in evidence)
    assert "100" in evidence[0]["quote"] and "160" in evidence[1]["quote"]
    assert db.get(Change, event["id"]).review_status == status


@pytest.mark.parametrize(
    "invalid",
    ["rejected", "missing_before", "missing_after", "same_snapshot", "wrong_study"],
)
def test_registry_visibility_requires_valid_evidence_and_not_rejected(client, db, raw, invalid):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    changes = registry_events(db, raw, company)
    target = changes[0]
    if invalid == "rejected":
        target.review_status = "rejected"
    elif invalid == "missing_before":
        target.before_snapshot = None
    elif invalid == "missing_after":
        target.after_snapshot = None
    elif invalid == "same_snapshot":
        target.before_snapshot = target.after_snapshot
    else:
        other = deepcopy(raw)
        other["protocolSection"]["identificationModule"]["nctId"] = "NCT01234568"
        ingest(db, company, other)
        target.after_snapshot = db.scalar(select(Snapshot.id).where(Snapshot.study_id == "NCT01234568"))
    user = reader(db)
    db.commit()
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[browsing_user] = lambda: user

    assert target.id not in {row["id"] for row in client.get("/api/changes?followed=false").json()}
    assert client.get(f"/api/changes/{target.id}").status_code == 404


@pytest.mark.parametrize("status", ["pending", "automatic", "rejected"])
def test_company_statements_still_require_approval(client, db, status):
    company = db.scalar(select(Company))
    event = Change(
        company_id=company.id, source="company", category="timeline",
        title="Company guidance changed", before_value="H1 2026", after_value="H2 2026",
        review_status=status,
    )
    user = reader(db)
    db.add(event)
    db.commit()
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[browsing_user] = lambda: user

    assert client.get("/api/changes?followed=false").json() == []
    assert client.get(f"/api/changes/{event.id}").status_code == 404
    event.review_status = "approved"
    db.commit()
    assert [row["id"] for row in client.get("/api/changes?followed=false").json()] == [event.id]
    assert client.get(f"/api/changes/{event.id}").status_code == 200


def test_follow_scope_category_search_and_newest_first_order(client, db, raw):
    company_a = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    company_b = db.scalar(select(Company).where(Company.id != company_a.id))
    a = registry_events(db, raw, company_a, "NCT01234567")
    a += registry_events(db, raw, company_a, "NCT01234568")
    b = registry_events(db, raw, company_b, "NCT01234569")
    other_b = registry_events(db, raw, company_b, "NCT01234570")
    all_changes = a + b + other_b
    for i, event in enumerate(all_changes):
        event.detected_at = f"2026-09-{20 + i // 2:02}T12:00:00+00:00"

    company_reader = reader(db)
    program_reader = reader(db, "program-reader@example.com")
    empty_reader = reader(db, "empty-reader@example.com")
    db.add_all([
        Follow(user_id=company_reader.id, kind="company", value=str(company_a.id)),
        Follow(user_id=program_reader.id, kind="program", value="NCT01234569"),
    ])
    db.commit()

    def expected_ids(events):
        return [event.id for event in sorted(events, key=lambda e: (e.detected_at, e.id), reverse=True)]

    app.dependency_overrides[current_user] = lambda: company_reader
    app.dependency_overrides[browsing_user] = lambda: company_reader
    assert [row["id"] for row in client.get("/api/changes").json()] == expected_ids(a)
    assert [row["id"] for row in client.get("/api/changes?followed=false").json()] == expected_ids(all_changes)
    assert [row["id"] for row in client.get("/api/changes?category=enrollment").json()] == expected_ids(
        [event for event in a if event.category == "enrollment"]
    )
    assert all(row["study_id"] == "NCT01234568" for row in client.get("/api/changes?q=NCT01234568").json())

    app.dependency_overrides[current_user] = lambda: program_reader
    app.dependency_overrides[browsing_user] = lambda: program_reader
    assert [row["id"] for row in client.get("/api/changes").json()] == expected_ids(b)
    app.dependency_overrides[current_user] = lambda: empty_reader
    app.dependency_overrides[browsing_user] = lambda: empty_reader
    assert client.get("/api/changes").json() == []
    assert len(client.get("/api/changes?followed=false").json()) == len(all_changes)


@pytest.mark.parametrize("status", ["automatic", "pending"])
def test_review_api_excludes_registry_comparisons_but_keeps_manual_items(client, db, raw, status):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    changes = registry_events(db, raw, company)
    for event in changes:
        event.review_status = status
    manual = Change(
        company_id=company.id, source="company", category="timeline",
        title="Unreviewed company guidance", review_status="pending",
    )
    db.add(manual)
    db.commit()
    result = client.get("/api/review")
    assert result.status_code == 200
    assert [row["id"] for row in result.json()["changes"]] == [manual.id]
