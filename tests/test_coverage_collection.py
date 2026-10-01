"""Coverage refresh, collection locks, and checkpoint scope use isolated SQLite."""

import json
from contextlib import contextmanager

import pytest
from sqlalchemy import select

from trialdelta import admin_ops, bulk_download as bulk, company_discovery, registry
from trialdelta.db import BulkCheckpoint, Company, Job, Session, User


@pytest.fixture
def coverage(db, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(registry.time, "sleep", lambda _: None)
    monkeypatch.setattr(bulk, "REQUEST_INTERVAL", 0)
    rows = db.scalars(select(Company).order_by(Company.id)).all()
    for row in rows:
        row.coverage_status = "delisted"
    active, retiring, outside = rows[:3]
    active.coverage_status = retiring.coverage_status = "active"
    outside.coverage_status = "out_of_sector"
    db.commit()
    return active.id, retiring.id, outside.id


@pytest.fixture
def held_lock(monkeypatch):
    state = {"held": False, "entries": 0}

    @contextmanager
    def lock():
        assert not state["held"], "Coverage refresh must not acquire a nested collection lock"
        state["held"] = True
        state["entries"] += 1
        try:
            yield True
        finally:
            state["held"] = False

    monkeypatch.setattr(registry, "sync_lock", lock)
    return state


def scripted_refresh(monkeypatch, retiring_id, held_lock):
    calls = []

    def discover(emit):
        assert held_lock["held"]
        with Session() as session:
            session.get(Company, retiring_id).coverage_status = "delisted"
            new = Company(ticker="NEWLIST", name="New Listing, Inc.", sponsor="New Listing", coverage_status="active")
            session.add(new)
            session.commit()
            calls.append(new.id)
        return {"status": "complete", "created": 1, "delisted": 1, "company_ids": calls[:]}

    monkeypatch.setattr(company_discovery, "discover_companies", discover)
    return calls


def empty_pages(monkeypatch, held_lock):
    requests = []

    def fetch(client, params, cancel, emit):
        assert held_lock["held"]
        requests.append(params.copy())
        return {"studies": []}

    monkeypatch.setattr(bulk, "_fetch_page", fetch)
    return requests


def test_worker_collection_refreshes_before_selecting_active_scope(db, coverage, held_lock, monkeypatch):
    active_id, retiring_id, _ = coverage
    refreshed = scripted_refresh(monkeypatch, retiring_id, held_lock)
    collected = []

    def fetch(company):
        assert held_lock["held"] and refreshed
        collected.append(company.id)
        return []

    monkeypatch.setattr(registry, "fetch_company", fetch)
    result = registry.sync_all(refresh_coverage=True)
    assert result["status"] == "complete"
    assert result["companies"] == 2
    assert set(collected) == {active_id, refreshed[0]}
    assert held_lock["entries"] == 1
    assert result["coverage"] == {"status": "complete", "created": 1, "delisted": 1}


def test_selected_worker_refreshes_then_skips_newly_delisted_company(db, coverage, held_lock, monkeypatch):
    _, retiring_id, _ = coverage
    scripted_refresh(monkeypatch, retiring_id, held_lock)
    monkeypatch.setattr(registry, "fetch_company", lambda _: pytest.fail("Inactive company must not be collected"))
    result = registry.sync_all(retiring_id, refresh_coverage=True)
    assert result["companies"] == 0
    assert result["warnings"] == [registry.INACTIVE_SELECTION_WARNING]


@pytest.mark.parametrize("selected", [False, True])
def test_new_bulk_refreshes_inside_collection_lock_then_preserves_selection(db, coverage, held_lock, monkeypatch, selected):
    active_id, retiring_id, outside_id = coverage
    refreshed = scripted_refresh(monkeypatch, retiring_id, held_lock)
    empty_pages(monkeypatch, held_lock)
    logs = []
    result = bulk.run_bulk_download(
        company_ids=[active_id, retiring_id, outside_id] if selected else None,
        refresh_coverage=True, emit=logs.append,
    )
    assert result["status"] == "complete"
    assert held_lock["entries"] == 1
    db.expire_all()
    state = json.loads(db.get(BulkCheckpoint, result["job_id"]).payload)
    expected = {active_id} if selected else {active_id, refreshed[0]}
    assert {row["id"] for row in state["company_scope"]} == expected
    assert result["companies"] == len(expected)
    assert result["coverage"]["created"] == 1
    if selected:
        assert result["warnings"] == [registry.INACTIVE_SELECTION_WARNING]
        assert registry.INACTIVE_SELECTION_WARNING in logs


@pytest.mark.parametrize("collector", ["worker", "bulk"])
def test_source_failure_retains_active_coverage_and_reports_safe_warning(db, coverage, held_lock, monkeypatch, collector):
    active_id, retiring_id, _ = coverage

    def unavailable(emit):
        assert held_lock["held"]
        raise company_discovery.CompanyDiscoveryError("mysql://private-user:private-password@source/private-table")

    monkeypatch.setattr(company_discovery, "discover_companies", unavailable)
    logs = []
    if collector == "worker":
        collected = []
        monkeypatch.setattr(registry, "fetch_company", lambda company: collected.append(company.id) or [])
        result = registry.sync_all(refresh_coverage=True)
        assert set(collected) == {active_id, retiring_id}
    else:
        empty_pages(monkeypatch, held_lock)
        result = bulk.run_bulk_download(refresh_coverage=True, emit=logs.append)
        assert registry.COVERAGE_REFRESH_WARNING in logs
    assert result["status"] == "complete"
    assert result["companies"] == 2
    assert result["coverage"] == {"status": "unavailable"}
    assert result["warnings"] == [registry.COVERAGE_REFRESH_WARNING]
    db.expire_all()
    job = db.scalar(select(Job).order_by(Job.id.desc()))
    assert json.loads(job.summary)["warnings"] == result["warnings"]
    assert "private-" not in json.dumps([result, logs, job.summary])
    assert db.get(Company, retiring_id).coverage_status == "active"


def test_resume_never_refreshes_or_replaces_original_scope(db, coverage, held_lock, monkeypatch):
    active_id, _, _ = coverage
    calls = []

    def first_page(client, params, cancel, emit):
        calls.append(params.copy())
        if len(calls) == 1:
            return {"studies": [], "nextPageToken": "saved-cursor"}
        raise RuntimeError("simulate interrupted page")

    monkeypatch.setattr(bulk, "_fetch_page", first_page)
    initial = bulk.run_bulk_download(company_ids=[active_id])
    assert initial["status"] == "partial"
    db.get(Company, active_id).coverage_status = "delisted"
    db.commit()
    monkeypatch.setattr(company_discovery, "discover_companies", lambda *_: pytest.fail("Resume must not refresh coverage"))
    requests = empty_pages(monkeypatch, held_lock)
    resumed = bulk.run_bulk_download(resume_job_id=initial["job_id"], refresh_coverage=True)
    assert resumed["status"] == "complete"
    assert resumed["companies"] == 1
    assert requests[0]["pageToken"] == "saved-cursor"
    assert requests[0]["query.spons"] == calls[0]["query.spons"]
    db.expire_all()
    saved = json.loads(db.get(BulkCheckpoint, initial["job_id"]).payload)
    assert [row["id"] for row in saved["company_scope"]] == [active_id]


def test_low_level_bulk_does_not_refresh_without_opt_in(db, coverage, held_lock, monkeypatch):
    monkeypatch.setattr(company_discovery, "discover_companies", lambda *_: pytest.fail("Low-level caller did not opt in"))
    empty_pages(monkeypatch, held_lock)
    result = bulk.run_bulk_download()
    assert result["companies"] == 2
    assert "coverage" not in result


@pytest.mark.parametrize("discover,selected", [(True, False), (False, False), (False, True)])
def test_every_new_admin_bulk_scope_requests_refresh(db, coverage, monkeypatch, discover, selected):
    user = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    calls = []
    monkeypatch.setattr(bulk, "run_bulk_download", lambda **kwargs: calls.append(kwargs) or {"status": "complete"})
    ids = [coverage[0]] if selected else None
    admin_ops.run_bulk_download(user.id, company_ids=ids, discover_companies=discover)
    assert calls[0]["refresh_coverage"] is True
    assert calls[0]["company_ids"] == ids


def test_all_inactive_bulk_selection_is_explicit_and_does_not_collect(db, coverage, held_lock, monkeypatch):
    monkeypatch.setattr(bulk, "_fetch_page", lambda *_: pytest.fail("Inactive companies must not be collected"))
    with pytest.raises(bulk.BulkDownloadError, match="No selected companies are active"):
        bulk.run_bulk_download(company_ids=[coverage[2]])


@pytest.mark.parametrize("collector", ["worker", "bulk"])
def test_busy_collector_does_not_refresh_coverage(db, coverage, monkeypatch, collector):
    @contextmanager
    def busy():
        yield False

    monkeypatch.setattr(registry, "sync_lock", busy)
    monkeypatch.setattr(company_discovery, "discover_companies", lambda *_: pytest.fail("Busy collector must not refresh coverage"))
    if collector == "worker":
        result = registry.sync_all(refresh_coverage=True)
    else:
        result = bulk.run_bulk_download(refresh_coverage=True)
    assert result["status"] == "busy"


def test_manual_company_collection_skips_inactive_selection(db, coverage, monkeypatch):
    user = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    collected, logs = [], []
    monkeypatch.setattr(registry, "sync_all", lambda company_id: collected.append(company_id) or {"status": "complete"})
    result = admin_ops.run_registry_sync(user.id, [coverage[0], coverage[2]], logs.append)
    assert result["status"] == "complete"
    assert collected == [coverage[0]]
    assert registry.INACTIVE_SELECTION_WARNING in logs


def test_admin_snapshot_tags_retained_companies_and_sanitizes_warnings(db, coverage):
    user = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    db.add(Job(status="complete", summary=json.dumps({"warnings": [registry.COVERAGE_REFRESH_WARNING, "private-driver-message"]})))
    db.commit()
    snapshot = admin_ops.dashboard_snapshot(user.id)
    by_id = {row["id"]: row for row in snapshot["companies"]}
    assert by_id[coverage[0]]["coverage_label"] == "Active"
    assert by_id[coverage[2]]["coverage_label"] == "Outside Healthcare"
    assert snapshot["jobs"][0]["warning_messages"] == [registry.COVERAGE_REFRESH_WARNING]
    assert "private-driver-message" not in json.dumps(snapshot)
