"""Bulk acquisition preserves registry evidence while resuming paged imports."""

import copy
import json
import threading
from contextlib import contextmanager

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DataError

from trialdelta import bulk_download as bulk
from trialdelta import registry
from trialdelta.db import Change, Company, Job, Snapshot, Study


@pytest.fixture(autouse=True)
def no_request_delay(monkeypatch):
    monkeypatch.setattr(bulk, "REQUEST_INTERVAL", 0)


def company(db, ticker="ARVN"):
    return db.scalar(select(Company).where(Company.ticker == ticker))


def count(db, model):
    db.expire_all()
    return db.scalar(select(func.count()).select_from(model))


def trial(raw, nct_id="NCT01234568", sponsor=None):
    result = copy.deepcopy(raw)
    result["protocolSection"]["identificationModule"]["nctId"] = nct_id
    if sponsor is not None:
        result["protocolSection"]["sponsorCollaboratorsModule"]["leadSponsor"]["name"] = sponsor
    return result


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


@pytest.mark.parametrize("saved_pages", [0, 1])
def test_database_size_error_retains_committed_pages_and_resumes(db, raw, monkeypatch, saved_pages):
    from trialdelta.db import BulkCheckpoint

    first_new, second_new = trial(raw), trial(raw, "NCT01234569")
    pages = [{"studies": [first_new, second_new]}]
    if saved_pages:
        pages.insert(0, {"studies": [raw], "nextPageToken": "pending-page"})
    scripted_pages(monkeypatch, pages)
    ingest = registry.ingest

    def oversized_record(session, sponsor, record):
        result = ingest(session, sponsor, record)
        if record["protocolSection"]["identificationModule"]["nctId"] == "NCT01234569":
            raise DataError("INSERT private-sql", {"secret": "private-parameter"},
                            Exception(1406, "private-driver-details"))
        return result

    monkeypatch.setattr(registry, "ingest", oversized_record)
    messages = []
    initial = bulk.run_bulk_download(company_ids=[company(db).id], emit=messages.append)

    assert initial["status"] == "partial"
    assert initial["pages"] == saved_pages
    assert initial["scanned"] == initial["studies"] == saved_pages
    assert initial["errors"] == [bulk.DATABASE_STORAGE_ERROR]
    assert count(db, Study) == count(db, Snapshot) == saved_pages
    assert count(db, Change) == 0
    job = db.get(Job, initial["job_id"])
    saved = db.get(BulkCheckpoint, job.id)
    expected_token = "pending-page" if saved_pages else None
    assert json.loads(saved.payload)["next_page_token"] == expected_token
    assert json.loads(job.summary)["errors"] == initial["errors"]
    assert json.loads(saved.payload)["errors"] == initial["errors"]
    assert "private-" not in json.dumps([messages, initial, job.summary, saved.payload])

    monkeypatch.setattr(registry, "ingest", ingest)
    calls = scripted_pages(monkeypatch, [{"studies": [first_new, second_new]}])
    resumed = bulk.run_bulk_download(resume_job_id=job.id)
    assert resumed["status"] == "complete"
    assert resumed["errors"] == []
    assert calls[0].get("pageToken") == expected_token
    assert count(db, Study) == count(db, Snapshot) == saved_pages + 2


@pytest.mark.parametrize("driver_args,expected", [
    ((1406, "private-driver-text"), bulk.DATABASE_STORAGE_ERROR),
    ((1264, "private-driver-text"), bulk.DATABASE_DATA_ERROR),
    (("private-driver-text",), bulk.DATABASE_DATA_ERROR),
    ((), bulk.DATABASE_DATA_ERROR),
])
def test_database_data_errors_hide_sql_parameters_and_driver_text(driver_args, expected):
    error = DataError("private-sql", {"password": "private-secret"}, Exception(*driver_args))
    assert bulk._error(error) == expected
    assert bulk.safe_error_messages([expected]) == [expected]


def test_saved_bulk_error_messages_allow_only_known_application_text():
    legacy = "Bulk download stopped (DataError). Saved pages are retained; resume to retry the unfinished page."
    known = "The registry repeated a page cursor. Start a new bulk download."
    interrupted = "Previous collection process stopped before completion."
    timeout = "Bulk download stopped (ReadTimeout). Saved pages are retained; resume to retry the unfinished page."
    result = bulk.safe_error_messages([
        legacy, known, timeout, bulk.DATABASE_STORAGE_ERROR,
        "private-sql", known + " private-credentials", {"password": "private-secret"},
        "Bulk download stopped (private-class). Saved pages are retained; resume to retry the unfinished page.",
    ])
    assert result == [
        bulk.DATABASE_DATA_ERROR, known, timeout, bulk.DATABASE_STORAGE_ERROR,
        *[bulk.UNKNOWN_BULK_ERROR] * 4,
    ]
    assert bulk.safe_error_messages(result) == result
    assert bulk.safe_error_messages([interrupted]) == [interrupted]
    assert bulk.safe_error_messages([]) == []
    assert bulk.safe_error_messages(None) == []
    assert bulk.safe_error_messages({"errors": "private-value"}) == [bulk.UNKNOWN_BULK_ERROR]
    assert bulk._error(bulk.BulkDownloadError("private-error")) == bulk.UNKNOWN_BULK_ERROR
    private_exception = type("private-class", (Exception,), {})
    assert bulk._error(private_exception("private-message")) == bulk.UNKNOWN_BULK_ERROR


def test_large_outcomes_are_preserved_in_snapshots_and_changes(db, raw, monkeypatch):
    baseline = copy.deepcopy(raw)
    original_outcomes = [{"measure": "Response", "description": "測" * 25000, "timeFrame": "12 months"}]
    baseline["protocolSection"]["outcomesModule"]["primaryOutcomes"] = original_outcomes
    scripted_pages(monkeypatch, [{"studies": [baseline]}])
    first = bulk.run_bulk_download(company_ids=[company(db).id])
    assert first["status"] == "complete"
    assert first["changes"] == 0

    revised = copy.deepcopy(baseline)
    revised_outcomes = revised["protocolSection"]["outcomesModule"]["primaryOutcomes"]
    revised_outcomes[0]["description"] += " revised"
    scripted_pages(monkeypatch, [{"studies": [revised]}])
    second = bulk.run_bulk_download(company_ids=[company(db).id])
    assert second["status"] == "complete"
    assert second["changes"] == 1
    assert count(db, Snapshot) == 2
    change = db.scalar(select(Change))
    before, after = db.get(Snapshot, change.before_snapshot), db.get(Snapshot, change.after_snapshot)
    assert json.loads(before.raw_json) == baseline
    assert json.loads(after.raw_json) == revised
    assert json.loads(before.normalized)["primary_outcomes"] == original_outcomes
    assert json.loads(after.normalized)["primary_outcomes"] == revised_outcomes
    assert json.loads(change.before_value) == original_outcomes
    assert json.loads(change.after_value) == revised_outcomes
    assert all(len(value.encode("utf-8")) > 65535 for value in (
        before.raw_json, after.raw_json, before.normalized, after.normalized,
        change.before_value, change.after_value,
    ))


def test_bulk_batches_sponsors_preserves_scope_and_stores_raw_evidence(db, raw, monkeypatch):
    arvinas, beam = company(db), company(db, "BEAM")
    beam_trial = trial(raw, sponsor="Beam Therapeutics, Inc.")
    collaborator_hit = trial(raw, "NCT01234569", "Pfizer")
    calls = scripted_pages(monkeypatch, [
        {"studies": [raw, beam_trial, collaborator_hit]},
    ])

    result = bulk.run_bulk_download(
        company_ids=[arvinas.id, beam.id], updated_since="2026-09-01", oncology_only=True
    )

    assert result["status"] == "complete"
    assert result["companies"] == 2
    assert result["studies"] == 2
    assert result["changes"] == 0
    assert result["pages"] == 1
    assert result["scanned"] == 3
    assert result["skipped"] == 1
    assert len(calls) == 1
    params = calls[0]
    assert params["pageSize"] == 1000
    assert "Arvinas" in params["query.spons"]
    assert "Beam Therapeutics" in params["query.spons"]
    assert " OR " in params["query.spons"]
    assert "cancer" in params["query.cond"]
    query = params.get("query.term", "") + " " + params.get("filter.advanced", "")
    assert "AREA[StudyType]INTERVENTIONAL" in query
    assert "AREA[LastUpdatePostDate]RANGE[2026-09-01,MAX]" in query
    assert count(db, Snapshot) == 2
    assert count(db, Change) == 0
    snapshots = db.scalars(select(Snapshot).order_by(Snapshot.study_id)).all()
    assert json.loads(snapshots[0].raw_json) == raw
    assert db.get(Study, "NCT01234569") is None
    assert db.get(Study, "NCT01234568").company_id == beam.id
    job = db.get(Job, result["job_id"])
    assert job.status == "complete"
    assert json.loads(job.summary)["kind"] == "bulk"


def test_empty_pages_continue_and_pagination_has_no_thirty_page_cap(db, raw, monkeypatch):
    pages = [{"studies": [], "nextPageToken": f"page-{i}"} for i in range(31)]
    pages.append({"studies": [raw]})
    calls = scripted_pages(monkeypatch, pages)

    result = bulk.run_bulk_download(company_ids=[company(db).id])

    assert result["status"] == "complete"
    assert result["pages"] == 32
    assert result["studies"] == 1
    assert calls[-1]["pageToken"] == "page-30"
    first_query = {k: v for k, v in calls[0].items() if k not in {"pageToken", "pageSize", "countTotal"}}
    for params in calls[1:]:
        assert {k: v for k, v in params.items() if k not in {"pageToken", "pageSize", "countTotal"}} == first_query


def test_duplicate_records_in_page_create_one_baseline(db, raw, monkeypatch):
    scripted_pages(monkeypatch, [{"studies": [raw, copy.deepcopy(raw)]}])

    result = bulk.run_bulk_download(company_ids=[company(db).id])

    assert result["status"] == "complete"
    assert result["studies"] == 1
    assert count(db, Study) == 1
    assert count(db, Snapshot) == 1
    assert count(db, Change) == 0


def test_failed_page_retains_checkpoint_and_resume_uses_frozen_filters(db, raw, monkeypatch):
    arvinas = company(db)
    calls = scripted_pages(monkeypatch, [
        {"studies": [raw], "nextPageToken": "second-page"},
        httpx.ReadTimeout("temporary upstream timeout"),
    ])
    initial = bulk.run_bulk_download(company_ids=[arvinas.id], updated_since="2026-09-01")
    assert initial["status"] in {"partial", "failed"}
    assert initial["errors"]
    assert count(db, Snapshot) == 1
    assert len(calls) == 2

    resumed_calls = scripted_pages(monkeypatch, [{"studies": [raw, trial(raw)]}])
    resumed = bulk.run_bulk_download(
        resume_job_id=initial["job_id"],
        company_ids=[company(db, "BEAM").id],
        updated_since="2025-01-01",
    )

    assert resumed["status"] == "complete"
    assert resumed["job_id"] == initial["job_id"]
    assert resumed_calls[0]["pageToken"] == "second-page"
    assert resumed_calls[0]["query.spons"] == calls[0]["query.spons"]
    assert resumed_calls[0].get("query.term") == calls[0].get("query.term")
    assert resumed_calls[0].get("filter.advanced") == calls[0].get("filter.advanced")
    assert count(db, Job) == 1
    assert count(db, Study) == 2
    assert count(db, Snapshot) == 2
    assert count(db, Change) == 0


def test_invalid_record_rolls_back_entire_page_and_can_resume(db, raw, monkeypatch):
    invalid = trial(raw)
    del invalid["protocolSection"]["statusModule"]["overallStatus"]
    scripted_pages(monkeypatch, [{"studies": [raw, invalid]}])
    initial = bulk.run_bulk_download(company_ids=[company(db).id])
    assert initial["status"] in {"partial", "failed"}
    assert initial["errors"]
    assert count(db, Study) == 0
    assert count(db, Snapshot) == 0

    calls = scripted_pages(monkeypatch, [{"studies": [raw, trial(raw)]}])
    resumed = bulk.run_bulk_download(resume_job_id=initial["job_id"])
    assert resumed["status"] == "complete"
    assert not calls[0].get("pageToken")
    assert count(db, Snapshot) == 2


@pytest.mark.parametrize("value", ["yesterday", "2026-02-30", "2026-1-01"])
def test_invalid_dates_fail_before_job_creation_or_network(db, value, monkeypatch):
    calls = scripted_pages(monkeypatch, [])
    with pytest.raises(ValueError):
        bulk.run_bulk_download(company_ids=[company(db).id], updated_since=value)
    assert calls == []
    assert count(db, Job) == 0


def test_ambiguous_normalized_sponsors_are_rejected(db, monkeypatch):
    arvinas, beam = company(db), company(db, "BEAM")
    beam.sponsor = "Arvinas, Inc."
    db.commit()
    calls = scripted_pages(monkeypatch, [])

    with pytest.raises(ValueError):
        bulk.run_bulk_download(company_ids=[arvinas.id, beam.id])

    assert calls == []
    assert count(db, Job) == 0


def test_resume_rejects_changed_company_scope(db, raw, monkeypatch):
    arvinas = company(db)
    scripted_pages(monkeypatch, [
        {"studies": [raw], "nextPageToken": "next"},
        httpx.ReadTimeout("temporary upstream timeout"),
    ])
    initial = bulk.run_bulk_download(company_ids=[arvinas.id])
    arvinas.sponsor = "A different sponsor"
    db.commit()
    calls = scripted_pages(monkeypatch, [])

    with pytest.raises(ValueError):
        bulk.run_bulk_download(resume_job_id=initial["job_id"])

    assert calls == []
    assert count(db, Snapshot) == 1


def test_busy_lock_does_not_create_job_or_download(db, monkeypatch):
    @contextmanager
    def busy_lock():
        yield False

    monkeypatch.setattr(registry, "sync_lock", busy_lock)
    calls = scripted_pages(monkeypatch, [])
    result = bulk.run_bulk_download(company_ids=[company(db).id])
    assert result["status"] == "busy"
    assert calls == []
    assert count(db, Job) == 0


def test_cancellation_between_pages_preserves_committed_work_for_resume(db, raw, monkeypatch):
    cancel = threading.Event()

    scripted_pages(monkeypatch, [{"studies": [raw], "nextPageToken": "next"}])

    def stop_after_page(message):
        if message.startswith("Page "):
            cancel.set()

    initial = bulk.run_bulk_download(company_ids=[company(db).id], cancel=cancel, emit=stop_after_page)
    assert initial["status"] == "cancelled"
    assert count(db, Snapshot) == 1

    calls = scripted_pages(monkeypatch, [{"studies": [trial(raw)]}])
    resumed = bulk.run_bulk_download(resume_job_id=initial["job_id"])
    assert resumed["status"] == "complete"
    assert calls[0]["pageToken"] == "next"
    assert count(db, Snapshot) == 2


def test_cancel_after_final_page_does_not_turn_completion_into_cancellation(db, raw, monkeypatch):
    cancel = threading.Event()

    scripted_pages(monkeypatch, [{"studies": [raw]}])

    def stop_after_page(message):
        if message.startswith("Page "):
            cancel.set()

    result = bulk.run_bulk_download(company_ids=[company(db).id], cancel=cancel, emit=stop_after_page)
    assert result["status"] == "complete"
    assert count(db, Snapshot) == 1


def test_pre_cancelled_run_makes_no_network_calls(db, monkeypatch):
    cancel = threading.Event()
    cancel.set()
    calls = scripted_pages(monkeypatch, [])
    result = bulk.run_bulk_download(company_ids=[company(db).id], cancel=cancel)
    assert result["status"] == "cancelled"
    assert calls == []
    assert count(db, Snapshot) == 0


def test_repeated_page_token_stops_with_resumable_error(db, raw, monkeypatch):
    calls = scripted_pages(monkeypatch, [
        {"studies": [raw], "nextPageToken": "same-token"},
        {"studies": [], "nextPageToken": "same-token"},
    ])
    result = bulk.run_bulk_download(company_ids=[company(db).id])
    assert result["status"] in {"partial", "failed"}
    assert result["errors"]
    assert len(calls) == 2
    assert count(db, Snapshot) == 1


def test_existing_study_ownership_is_not_reassigned(db, raw, monkeypatch):
    arvinas, beam = company(db), company(db, "BEAM")
    registry.ingest(db, beam, raw)
    db.commit()
    scripted_pages(monkeypatch, [{"studies": [raw]}])

    result = bulk.run_bulk_download(company_ids=[arvinas.id])

    assert result["status"] == "complete"
    assert result["studies"] == 0
    assert result["skipped"] == 1
    assert db.get(Study, "NCT01234567").company_id == beam.id
    assert count(db, Snapshot) == 1


def test_reimport_is_idempotent_and_updates_use_existing_diff_pipeline(db, raw, monkeypatch):
    arvinas = company(db)
    scripted_pages(monkeypatch, [{"studies": [raw]}])
    assert bulk.run_bulk_download(company_ids=[arvinas.id])["changes"] == 0
    scripted_pages(monkeypatch, [{"studies": [raw]}])
    assert bulk.run_bulk_download(company_ids=[arvinas.id])["changes"] == 0
    assert count(db, Snapshot) == 1

    updated = copy.deepcopy(raw)
    updated["protocolSection"]["designModule"]["enrollmentInfo"]["count"] = 160
    scripted_pages(monkeypatch, [{"studies": [updated]}])
    result = bulk.run_bulk_download(company_ids=[arvinas.id])

    assert result["changes"] == 1
    assert count(db, Snapshot) == 2
    assert count(db, Change) == 1
    change = db.scalar(select(Change))
    assert change.category == "enrollment"
    assert change.before_snapshot and change.after_snapshot
    assert change.review_status == "automatic"
    first = db.get(Snapshot, change.before_snapshot)
    assert json.loads(first.raw_json) == raw


def test_default_bulk_scope_includes_all_trial_types_and_conditions(db, raw, monkeypatch):
    calls = scripted_pages(monkeypatch, [{"studies": [raw]}])
    result = bulk.run_bulk_download(company_ids=[company(db).id])
    assert result["status"] == "complete"
    assert not calls[0].get("query.cond")
    query = calls[0].get("query.term", "") + " " + calls[0].get("filter.advanced", "")
    assert "AREA[StudyType]INTERVENTIONAL" not in query



def test_page_fetch_retries_transient_responses_and_honors_retry_after(monkeypatch):
    waits, requests = [], []

    class Cancellation:
        def wait(self, seconds):
            waits.append(seconds)
            return False

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        if len(requests) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"studies": [], "nextPageToken": "next"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = bulk._fetch_page(client, {"pageSize": 1000}, Cancellation(), lambda _: None)

    assert result == {"studies": [], "nextPageToken": "next"}
    assert len(requests) == 3
    assert 3 in waits
    assert 2 in waits


def test_page_fetch_does_not_retry_invalid_query(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(400, text="Invalid query")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            bulk._fetch_page(client, {}, threading.Event(), lambda _: None)

    assert len(requests) == 1


@pytest.mark.parametrize("payload", [[], {"studies": {}}, {"studies": [], "nextPageToken": 3}])
def test_page_fetch_rejects_malformed_responses(payload):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(ValueError):
            bulk._fetch_page(client, {}, threading.Event(), lambda _: None)


def test_large_company_scope_is_split_into_hundred_sponsor_batches(db, monkeypatch):
    companies = [
        Company(ticker=f"BULK{i}", name=f"Research Sponsor {i}", sponsor=f"Research Sponsor {i}")
        for i in range(101)
    ]
    db.add_all(companies)
    db.commit()
    calls = scripted_pages(monkeypatch, [{"studies": []}, {"studies": []}])

    result = bulk.run_bulk_download(company_ids=[row.id for row in companies])

    assert result["status"] == "complete"
    assert result["companies"] == 101
    assert result["pages"] == 2
    assert [row["query.spons"].count("COVERAGE[Contains]") for row in calls] == [100, 1]
    assert not calls[1].get("pageToken")


def test_reader_cannot_discover_download_or_resume(db, monkeypatch):
    from trialdelta import admin_ops as ops
    from trialdelta.db import User

    reader = User(email="bulk-reader@example.org", password_hash="unused", is_admin=False)
    db.add(reader)
    db.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("Unauthorized bulk operations must not reach discovery or acquisition")

    monkeypatch.setattr(ops.company_discovery, "discover_companies", forbidden)
    monkeypatch.setattr(ops.bulk_download, "run_bulk_download", forbidden)
    actions = [
        lambda: ops.discover_bulk_companies(reader.id),
        lambda: ops.run_bulk_download(reader.id, discover_companies=True),
        lambda: ops.run_bulk_download(reader.id, resume_job_id=1),
    ]
    for action in actions:
        with pytest.raises(ops.AdminOperationError, match="Administrator access"):
            action()


def test_hosted_mode_rejects_local_admin_for_bulk_operations(db, monkeypatch):
    from trialdelta import admin_ops as ops
    from trialdelta.db import User

    administrator = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    monkeypatch.setattr(ops.auth, "MODE", "hosted")
    monkeypatch.setattr(ops.company_discovery, "discover_companies", lambda *_: pytest.fail("Unexpected discovery"))
    monkeypatch.setattr(ops.bulk_download, "run_bulk_download", lambda **_: pytest.fail("Unexpected download"))
    with pytest.raises(ops.AdminOperationError):
        ops.discover_bulk_companies(administrator.id)
    with pytest.raises(ops.AdminOperationError):
        ops.run_bulk_download(administrator.id)


def test_admin_requests_locked_coverage_refresh_and_resume_skips_discovery(db, monkeypatch):
    from trialdelta import admin_ops as ops
    from trialdelta.db import User

    administrator = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    ids = [company(db).id]
    calls = []

    def discover(emit):
        pytest.fail("Discovery must run inside the bulk collector's shared lock")

    def download(**kwargs):
        calls.append(kwargs)
        return {"status": "complete", "job_id": 7}

    monkeypatch.setattr(ops.company_discovery, "discover_companies", discover)
    monkeypatch.setattr(ops.bulk_download, "run_bulk_download", download)
    result = ops.run_bulk_download(administrator.id, company_ids=ids, discover_companies=True, updated_since="2026-09-01")
    assert result["job_id"] == 7
    assert len(calls) == 1
    assert calls[0]["company_ids"] == ids
    assert calls[0]["updated_since"] == "2026-09-01"
    assert calls[0]["oncology_only"] is False
    assert calls[0]["refresh_coverage"] is True

    calls.clear()
    ops.run_bulk_download(administrator.id, resume_job_id=7, discover_companies=True)
    assert len(calls) == 1
    assert calls[0]["resume_job_id"] == 7
    assert calls[0]["refresh_coverage"] is False


def test_admin_invalid_date_is_rejected_before_discovery(db, monkeypatch):
    from trialdelta import admin_ops as ops
    from trialdelta.db import User

    administrator = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    monkeypatch.setattr(ops.company_discovery, "discover_companies", lambda *_: pytest.fail("Unexpected discovery"))
    with pytest.raises(ValueError):
        ops.run_bulk_download(administrator.id, discover_companies=True, updated_since="bad-date")


def test_dashboard_bulk_history_never_exposes_checkpoint_or_arbitrary_errors(db):
    from trialdelta import admin_ops as ops
    from trialdelta.db import BulkCheckpoint, User

    administrator = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    job = Job(status="partial", summary=json.dumps({
        "kind": "bulk", "companies": 2, "studies": 5, "changes": 1,
        "pages": 3, "scanned": 7, "skipped": 2,
        "updated_since": "2026-09-01", "oncology_only": False,
        "errors": ["driver-secret-must-stay-hidden"],
        "next_page_token": "cursor-must-stay-hidden",
        "company_scope": [{"id": 1, "sponsor": "scope-must-stay-hidden"}],
    }))
    db.add(job)
    db.flush()
    db.add(BulkCheckpoint(job_id=job.id, payload=json.dumps({"private": "checkpoint-must-stay-hidden"})))
    db.commit()

    result = ops.dashboard_snapshot(administrator.id)
    encoded = json.dumps(result)
    for marker in ["driver-secret", "cursor-must", "scope-must", "checkpoint-must"]:
        assert marker not in encoded
    assert result["bulk_jobs"][0]["id"] == job.id
    assert result["bulk_jobs"][0]["kind"] == "bulk"
    assert result["bulk_jobs"][0]["errors"] == 1
    assert result["bulk_jobs"][0]["pages"] == 3

    db.add_all(Job(status="complete", summary="{}") for _ in range(101))
    db.commit()
    result = ops.dashboard_snapshot(administrator.id)
    assert job.id not in {row["id"] for row in result["jobs"]}
    assert job.id in {row["id"] for row in result["bulk_jobs"]}


def test_regular_sync_recovers_stale_bulk_job_without_losing_resume_state(db, raw, monkeypatch):
    from trialdelta.db import BulkCheckpoint

    arvinas = company(db)
    scripted_pages(monkeypatch, [
        {"studies": [raw], "nextPageToken": "resume-after-stale-run"},
        httpx.ReadTimeout("temporary upstream timeout"),
    ])
    initial = bulk.run_bulk_download(company_ids=[arvinas.id])
    job = db.get(Job, initial["job_id"])
    job.status = "running"
    checkpoint = db.get(BulkCheckpoint, job.id).payload
    db.commit()
    monkeypatch.setattr(registry, "fetch_company", lambda _: [])
    monkeypatch.setattr(registry.time, "sleep", lambda _: None)

    assert registry.sync_all(company(db, "BEAM").id)["status"] == "complete"

    db.expire_all()
    assert db.get(Job, job.id).status == "interrupted"
    assert json.loads(db.get(Job, job.id).summary)["pages"] == 1
    assert db.get(BulkCheckpoint, job.id).payload == checkpoint
    calls = scripted_pages(monkeypatch, [{"studies": [trial(raw)]}])
    resumed = bulk.run_bulk_download(resume_job_id=job.id)
    assert resumed["status"] == "complete"
    assert calls[0]["pageToken"] == "resume-after-stale-run"
    assert count(db, Snapshot) == 2


def test_mysql_checkpoint_storage_supports_large_universe_without_inflating_job_summary(db, monkeypatch):
    from sqlalchemy.dialects.mysql import dialect
    from trialdelta.db import BulkCheckpoint

    assert str(BulkCheckpoint.__table__.c.payload.type.compile(dialect=dialect())) == "LONGTEXT"
    companies = [
        Company(ticker=f"LONG{i}", name=f"Company {i}", sponsor=f"Research {i} " + "Long name " * 15)
        for i in range(400)
    ]
    db.add_all(companies)
    db.commit()
    cancel = threading.Event()
    cancel.set()
    result = bulk.run_bulk_download(company_ids=[row.id for row in companies], cancel=cancel)
    db.expire_all()
    job = db.get(Job, result["job_id"])
    checkpoint = db.get(BulkCheckpoint, job.id)
    assert len(checkpoint.payload.encode()) > 65535
    assert len(job.summary.encode()) < 2048
    assert result["status"] == "cancelled"


def test_ingestion_error_rolls_back_page_rows_and_cursor_together(db, raw, monkeypatch):
    from trialdelta.db import BulkCheckpoint

    first_new = trial(raw, "NCT01234568")
    second_new = trial(raw, "NCT01234569")
    scripted_pages(monkeypatch, [
        {"studies": [raw], "nextPageToken": "pending-page"},
        {"studies": [first_new, second_new]},
    ])
    ingest = registry.ingest

    def fail_during_second_page(session, company, record):
        if record["protocolSection"]["identificationModule"]["nctId"] == "NCT01234569":
            raise RuntimeError("simulated transaction failure")
        return ingest(session, company, record)

    monkeypatch.setattr(registry, "ingest", fail_during_second_page)
    initial = bulk.run_bulk_download(company_ids=[company(db).id])
    assert initial["status"] == "partial"
    assert initial["pages"] == 1
    assert count(db, Study) == 1
    assert count(db, Snapshot) == 1
    saved = json.loads(db.get(BulkCheckpoint, initial["job_id"]).payload)
    assert saved["next_page_token"] == "pending-page"

    monkeypatch.setattr(registry, "ingest", ingest)
    calls = scripted_pages(monkeypatch, [{"studies": [first_new, second_new]}])
    resumed = bulk.run_bulk_download(resume_job_id=initial["job_id"])
    assert resumed["status"] == "complete"
    assert calls[0]["pageToken"] == "pending-page"
    assert count(db, Study) == 3
    assert count(db, Snapshot) == 3


@pytest.mark.parametrize("filters", [{"oncology_only": True}, {"updated_since": "2026-09-01"}])
def test_filtered_bulk_refresh_preserves_company_freshness_while_refreshing_study(db, raw, monkeypatch, filters):
    arvinas = company(db)
    registry.ingest(db, arvinas, raw)
    arvinas.last_checked = "2025-01-01T00:00:00+00:00"
    arvinas.last_error = "An earlier full-company refresh failed"
    study = db.get(Study, "NCT01234567")
    study.last_checked = "2025-01-01T00:00:00+00:00"
    db.commit()
    scripted_pages(monkeypatch, [{"studies": [raw]}])

    result = bulk.run_bulk_download(company_ids=[arvinas.id], **filters)

    assert result["status"] == "complete"
    db.expire_all()
    assert db.get(Company, arvinas.id).last_checked == "2025-01-01T00:00:00+00:00"
    assert db.get(Company, arvinas.id).last_error == "An earlier full-company refresh failed"
    assert db.get(Study, "NCT01234567").last_checked != "2025-01-01T00:00:00+00:00"
    assert count(db, Snapshot) == 1
