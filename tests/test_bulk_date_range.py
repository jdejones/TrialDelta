"""Inclusive registry update windows remain fixed across pages and resumes."""

import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from trialdelta import admin_ops as ops
from trialdelta import bulk_download as bulk
from trialdelta import registry
from trialdelta.db import BulkCheckpoint, Company, Job, Snapshot, Study, User


@pytest.fixture(autouse=True)
def no_request_delay(monkeypatch):
    monkeypatch.setattr(bulk, "REQUEST_INTERVAL", 0)


def company(db):
    return db.scalar(select(Company).where(Company.ticker == "ARVN"))


def administrator(db):
    return db.scalar(select(User).where(User.email == "local@trialdelta.local"))


def scripted_pages(monkeypatch, pages):
    calls = []
    responses = iter(pages)

    def fetch(client, params, cancel, emit):
        calls.append(dict(params))
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    monkeypatch.setattr(bulk, "_fetch_page", fetch)
    return calls


@pytest.mark.parametrize("start,end,expected", [
    ("2026-09-01", "2026-09-24", "AREA[LastUpdatePostDate]RANGE[2026-09-01,2026-09-24]"),
    ("2026-09-01", None, "AREA[LastUpdatePostDate]RANGE[2026-09-01,MAX]"),
    (None, "2026-09-24", "AREA[LastUpdatePostDate]RANGE[MIN,2026-09-24]"),
    ("2026-09-01", "2026-09-01", "AREA[LastUpdatePostDate]RANGE[2026-09-01,2026-09-01]"),
    (None, None, None),
])
@pytest.mark.parametrize("oncology_only", [False, True])
def test_range_query_is_inclusive_and_stable_across_pages(
    db, raw, monkeypatch, start, end, expected, oncology_only
):
    calls = scripted_pages(monkeypatch, [
        {"studies": [], "nextPageToken": "second-page"},
        {"studies": [raw]},
    ])

    result = bulk.run_bulk_download(
        company_ids=[company(db).id], updated_since=start,
        updated_through=end, oncology_only=oncology_only,
    )

    assert result["status"] == "complete"
    assert result["studies"] == 1
    assert len(calls) == 2
    expected_term = "AREA[StudyType]INTERVENTIONAL" if oncology_only else ""
    if expected:
        expected_term += (" AND " if expected_term else "") + expected
    assert calls[0].get("query.term", "") == expected_term
    if not expected_term:
        assert "query.term" not in calls[0]
    assert bool(calls[0].get("query.cond")) is oncology_only
    if oncology_only:
        assert "cancer" in calls[0]["query.cond"]
    assert calls[1]["pageToken"] == "second-page"
    assert {k: v for k, v in calls[1].items() if k != "pageToken"} == calls[0]
    db.expire_all()
    saved = json.loads(db.get(BulkCheckpoint, result["job_id"]).payload)
    summary = json.loads(db.get(Job, result["job_id"]).summary)
    for state in [saved, summary]:
        assert state["updated_since"] == start
        assert state["updated_through"] == end


@pytest.mark.parametrize("start,end,expected", [
    (None, None, (None, None)),
    (" ", "\t", (None, None)),
    (" 2026-09-01 ", " 2026-09-24 ", ("2026-09-01", "2026-09-24")),
    (None, "2026-09-24", (None, "2026-09-24")),
    ("2026-09-01", "2026-09-01", ("2026-09-01", "2026-09-01")),
])
def test_date_range_normalizes_optional_bounds(start, end, expected):
    assert bulk.validate_date_range(start, end) == expected


def test_date_range_allows_today():
    today = datetime.now(timezone.utc).date().isoformat()
    assert bulk.validate_date_range(today, today) == (today, today)


@pytest.mark.parametrize("start,end", [
    (None, "yesterday"),
    (None, "2026-02-30"),
    (None, "2026-9-01"),
    ("2026-09-25", "2026-09-01"),
    ("bad-date", "2026-09-24"),
    (None, (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()),
])
def test_invalid_range_fails_before_job_or_network(db, monkeypatch, start, end):
    calls = scripted_pages(monkeypatch, [])

    with pytest.raises(ValueError):
        bulk.run_bulk_download(
            company_ids=[company(db).id], updated_since=start, updated_through=end,
        )

    assert calls == []
    assert db.scalar(select(func.count()).select_from(Job)) == 0
    assert db.scalar(select(func.count()).select_from(BulkCheckpoint)) == 0


def test_admin_range_validation_precedes_company_discovery(db, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid date windows must be rejected before discovery or acquisition")

    monkeypatch.setattr(ops.company_discovery, "discover_companies", unexpected)
    monkeypatch.setattr(ops.bulk_download, "run_bulk_download", unexpected)

    for start, end in [(None, "bad-date"), ("2026-09-25", "2026-09-01")]:
        with pytest.raises(ValueError):
            ops.run_bulk_download(
                administrator(db).id, discover_companies=True,
                updated_since=start, updated_through=end,
            )

    assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_admin_resume_preserves_saved_range_despite_invalid_current_form(db, raw, monkeypatch):
    calls = scripted_pages(monkeypatch, [
        {"studies": [raw], "nextPageToken": "saved-range-page"},
        httpx.ReadTimeout("temporary upstream timeout"),
    ])
    discovered_ids = [company(db).id]
    monkeypatch.setattr(
        ops.company_discovery, "discover_companies", lambda emit: {"company_ids": discovered_ids},
    )
    first = ops.run_bulk_download(
        administrator(db).id, discover_companies=True,
        updated_since=" 2026-09-01 ", updated_through=" 2026-09-24 ", oncology_only=True,
    )
    assert first["status"] == "partial"
    original_query = calls[0]

    def unexpected_discovery(*args, **kwargs):
        pytest.fail("Resuming must reuse saved company discovery and dates")

    monkeypatch.setattr(ops.company_discovery, "discover_companies", unexpected_discovery)
    resumed_calls = scripted_pages(monkeypatch, [{"studies": [raw]}])
    resumed = ops.run_bulk_download(
        administrator(db).id, resume_job_id=first["job_id"], discover_companies=True,
        company_ids=[], updated_since="invalid current start", updated_through="invalid current end",
        oncology_only=False,
    )

    assert resumed["status"] == "complete"
    assert resumed["job_id"] == first["job_id"]
    assert resumed_calls[0]["pageToken"] == "saved-range-page"
    assert {k: v for k, v in resumed_calls[0].items() if k != "pageToken"} == original_query
    db.expire_all()
    summary = json.loads(db.get(Job, first["job_id"]).summary)
    assert summary["updated_since"] == "2026-09-01"
    assert summary["updated_through"] == "2026-09-24"
    assert summary["oncology_only"] is True
    assert db.scalar(select(func.count()).select_from(Job)) == 1
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 1


@pytest.mark.parametrize("start", [None, "2026-09-01"])
def test_legacy_checkpoint_without_upper_bound_resumes_original_query(db, raw, monkeypatch, start):
    calls = scripted_pages(monkeypatch, [
        {"studies": [raw], "nextPageToken": "legacy-page"},
        httpx.ReadTimeout("temporary upstream timeout"),
    ])
    first = bulk.run_bulk_download(company_ids=[company(db).id], updated_since=start)
    assert first["status"] == "partial"
    db.expire_all()
    checkpoint = db.get(BulkCheckpoint, first["job_id"])
    job = db.get(Job, first["job_id"])
    state = json.loads(checkpoint.payload)
    state.pop("updated_through", None)
    summary = json.loads(job.summary)
    summary.pop("updated_through", None)
    checkpoint.payload, job.summary = json.dumps(state), json.dumps(summary)
    db.commit()

    resumed_calls = scripted_pages(monkeypatch, [{"studies": []}])
    resumed = bulk.run_bulk_download(
        resume_job_id=first["job_id"], updated_since="bad-date", updated_through="2026-09-24",
    )

    assert resumed["status"] == "complete"
    assert resumed_calls[0]["pageToken"] == "legacy-page"
    assert {k: v for k, v in resumed_calls[0].items() if k != "pageToken"} == calls[0]
    db.expire_all()
    assert json.loads(db.get(Job, first["job_id"]).summary)["updated_through"] is None


@pytest.mark.parametrize("start", [None, "2026-09-01"])
def test_upper_bounded_download_preserves_company_freshness(db, raw, monkeypatch, start):
    arvinas = company(db)
    registry.ingest(db, arvinas, raw)
    old_check = "2025-01-01T00:00:00+00:00"
    old_error = "An earlier full-company refresh failed"
    arvinas.last_checked, arvinas.last_error = old_check, old_error
    db.get(Study, "NCT01234567").last_checked = old_check
    db.commit()
    scripted_pages(monkeypatch, [{"studies": [raw]}])

    result = bulk.run_bulk_download(
        company_ids=[arvinas.id], updated_since=start, updated_through="2026-09-24",
    )

    assert result["status"] == "complete"
    db.expire_all()
    assert db.get(Company, arvinas.id).last_checked == old_check
    assert db.get(Company, arvinas.id).last_error == old_error
    assert db.get(Study, "NCT01234567").last_checked != old_check
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 1


@pytest.mark.parametrize("end,expected", [
    ("2026-09-24", "2026-09-24"),
    (None, None),
    ("not-a-date-with-private-data", None),
    ({"private": "never show arbitrary summary content"}, None),
])
def test_bulk_history_exposes_only_valid_upper_date(end, expected):
    job = Job(status="partial", summary=json.dumps({
        "kind": "bulk", "updated_since": "2026-09-01", "updated_through": end,
    }))

    result = ops._job_dict(job)

    assert result["updated_since"] == "2026-09-01"
    assert result["updated_through"] == expected
    assert "private" not in json.dumps(result)
