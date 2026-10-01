import copy
import json
from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import select, func
from trialdelta.db import Company, Study, Snapshot, Change, Evidence, User, Follow
from trialdelta.registry import ingest, normalize, sponsor_key
from trialdelta.services import (
    period,
    approve_evidence,
    published_changes,
    matches_follow,
    reconciliation,
)
from trialdelta.auth import password_hash


def company(db):
    return db.scalar(select(Company).where(Company.ticker == "ARVN"))


def test_seed_idempotent(client, db):
    from trialdelta.seed import seed

    seed()
    seed()
    assert db.scalar(select(func.count()).select_from(Company)) == 25


def test_baseline_unchanged_no_alert(db, raw):
    c = company(db)
    assert ingest(db, c, raw) == 0
    assert ingest(db, c, raw) == 0
    db.commit()
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 1
    assert db.scalar(select(func.count()).select_from(Change)) == 0
    assert db.get(Study, "NCT01234567").primary_completion == "2028-06"


def test_field_diffs_and_immutable_source(db, raw):
    c = company(db)
    ingest(db, c, raw)
    changed = copy.deepcopy(raw)
    changed["protocolSection"]["designModule"]["enrollmentInfo"]["count"] = 160
    changed["protocolSection"]["statusModule"]["primaryCompletionDateStruct"][
        "date"
    ] = "2028-09"
    assert ingest(db, c, changed) == 2
    db.commit()
    rows = db.scalars(select(Change)).all()
    assert {r.category for r in rows} == {"enrollment", "registry_date"}
    assert all(r.before_snapshot and r.after_snapshot and not r.reviewed for r in rows)
    assert all(r.review_status == "automatic" and r.reviewed_at is None for r in rows)
    assert {row["id"] for row in published_changes(db, 1)} == {r.id for r in rows}
    first = db.scalar(select(Snapshot).order_by(Snapshot.id))
    assert (
        json.loads(first.raw_json)["protocolSection"]["designModule"]["enrollmentInfo"][
            "count"
        ]
        == 100
    )


def test_metadata_only_no_nuisance(db, raw):
    c = company(db)
    ingest(db, c, raw)
    raw["protocolSection"]["statusModule"]["lastUpdatePostDateStruct"][
        "date"
    ] = "2026-09-02"
    assert ingest(db, c, raw) == 0


def test_registry_unknown_is_not_precise_date(db, raw):
    c = company(db)
    ingest(db, c, raw)
    del raw["protocolSection"]["statusModule"]["primaryCompletionDateStruct"]
    ingest(db, c, raw)
    db.commit()
    event = db.scalar(select(Change))
    assert event.after_value == "Not reported"
    assert db.get(Study, "NCT01234567").primary_completion is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("H2 2026", ("2026-07-01", "2026-12-31", "half-year")),
        ("Q1 2028", ("2028-01-01", "2028-03-31", "quarter")),
        ("2028-02", ("2028-02-01", "2028-02-29", "month")),
        ("2026", ("2026-01-01", "2026-12-31", "year")),
        ("when mature", (None, None, "uncertain")),
    ],
)
def test_precision(value, expected):
    assert period(value) == expected


def evidence(db, c, value, day, study_id=None):
    row = Evidence(
        company_id=c.id,
        study_id=study_id,
        source_type="presentation",
        title="Source, page 1",
        url="https://example.org/source",
        published_at=day,
        quote="The source supports this statement.",
        category="timeline",
        value=value,
        created_by=1,
    )
    db.add(row)
    db.commit()
    return row


def test_guidance_baseline_review_and_diff(db, raw):
    c = company(db)
    ingest(db, c, raw)
    db.commit()
    a = evidence(db, c, "H1 2026", "2026-01-01", "NCT01234567")
    assert not approve_evidence(db, a, 1)["change_created"]
    b = evidence(db, c, "H2 2026", "2026-03-01", "NCT01234567")
    assert published_changes(db, 1) == []
    assert approve_evidence(db, b, 1)["change_created"]
    changes = published_changes(db, 1)
    assert len(changes) == 1 and changes[0]["before_value"] == "H1 2026"
    with pytest.raises(Exception):
        approve_evidence(db, b, 1)
    assert len(published_changes(db, 1)) == 1


def test_unlinked_statements_do_not_create_false_changes(db):
    c = company(db)
    approve_evidence(db, evidence(db, c, "Drug A H1 2026", "2026-01-01"), 1)
    approve_evidence(db, evidence(db, c, "Drug B H2 2026", "2026-02-01"), 1)
    assert published_changes(db, 1) == []


def test_backdated_evidence_not_reversed_change(db, raw):
    c = company(db)
    ingest(db, c, raw)
    db.commit()
    approve_evidence(db, evidence(db, c, "H2 2026", "2026-04-01", "NCT01234567"), 1)
    with pytest.raises(Exception):
        approve_evidence(db, evidence(db, c, "H1 2026", "2026-01-01", "NCT01234567"), 1)
    assert published_changes(db, 1) == []


def test_exact_target_matching(db, raw):
    c = company(db)
    ingest(db, c, raw)
    db.commit()
    s = db.get(Study, "NCT01234567")
    s.targets = '["MET"]'
    event = Change(company_id=c.id, study_id=s.id)
    assert matches_follow(event, s, [Follow(kind="target", value="met")])
    assert not matches_follow(event, s, [Follow(kind="target", value="RET")])
    assert not matches_follow(event, s, [Follow(kind="indication", value="Tumor")])


def test_reconciliation_stale_and_baseline(db, raw):
    c = company(db)
    ingest(db, c, raw)
    db.commit()
    assert "baseline" in reconciliation(db, "NCT01234567")["message"]
    s = db.get(Study, "NCT01234567")
    s.last_checked = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    db.commit()
    assert reconciliation(db, s.id)["status"] == "stale"


def test_failed_refresh_retains_good_snapshot(db, raw, monkeypatch):
    import trialdelta.registry as registry

    c = company(db)
    ingest(db, c, raw)
    db.commit()
    monkeypatch.setattr(
        registry,
        "fetch_company",
        lambda _: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    result = registry.sync_all(c.id)
    assert result["status"] == "partial"
    db.expire_all()
    assert db.get(Study, "NCT01234567").status == "RECRUITING"
    assert db.scalar(select(func.count()).select_from(Change)) == 0


def test_sponsor_matching():
    assert sponsor_key("Revolution Medicines, Inc.") == sponsor_key(
        "Revolution Medicines"
    )
    assert sponsor_key("Other Medicines") != sponsor_key("Revolution Medicines")
