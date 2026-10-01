"""All-condition collection stays compatible with existing snapshots and resumes."""

import copy
import json
import threading

import httpx
import pytest
from sqlalchemy import func, select

from trialdelta import bulk_download as bulk
from trialdelta import registry
from trialdelta.db import BulkCheckpoint, Change, Company, Follow, Snapshot, Study
from trialdelta.services import matches_follow


@pytest.fixture(autouse=True)
def isolated_collection_lock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def company(db):
    return db.scalar(select(Company).where(Company.ticker == "ARVN"))


def other_condition(raw, *, study_type="OBSERVATIONAL", intervention_type="DEVICE"):
    record = copy.deepcopy(raw)
    protocol = record["protocolSection"]
    protocol["conditionsModule"]["conditions"] = ["Type 2 Diabetes Mellitus"]
    protocol["designModule"] = {
        "studyType": study_type,
        "enrollmentInfo": {"count": 100, "type": "ESTIMATED"},
        "designInfo": {"observationalModel": "COHORT", "timePerspective": "PROSPECTIVE"},
    }
    protocol["armsInterventionsModule"]["interventions"] = [
        {"type": intervention_type, "name": "Continuous glucose monitor"},
    ]
    return record


def registry_pages(monkeypatch, pages):
    requests = []
    responses = iter(pages)
    client_class = httpx.Client

    def handler(request):
        requests.append(dict(request.url.params))
        return httpx.Response(200, json=next(responses))

    monkeypatch.setattr(registry.httpx, "Client", lambda **kwargs: client_class(
        transport=httpx.MockTransport(handler), **kwargs
    ))
    monkeypatch.setattr(registry.time, "sleep", lambda _: None)
    return requests


def bulk_pages(monkeypatch, pages):
    requests = []
    responses = iter(pages)

    def fetch(client, params, cancel, emit):
        requests.append(dict(params))
        return copy.deepcopy(next(responses))

    monkeypatch.setattr(bulk, "_fetch_page", fetch)
    monkeypatch.setattr(bulk, "REQUEST_INTERVAL", 0)
    return requests


@pytest.mark.parametrize("intervention_type", [
    "DRUG", "BIOLOGICAL", "GENETIC", "DEVICE", "PROCEDURE", "BEHAVIORAL",
    "DIETARY_SUPPLEMENT", "DIAGNOSTIC_TEST", "RADIATION", "OTHER",
])
def test_every_named_intervention_can_be_saved_and_followed(db, raw, intervention_type):
    record = other_condition(raw, intervention_type=intervention_type)
    sponsor = company(db)
    assert registry.ingest(db, sponsor, record) == 0
    study = db.get(Study, "NCT01234567")
    change = Change(company_id=sponsor.id, study_id=study.id)

    assert study.drug == "Continuous glucose monitor"
    assert matches_follow(change, study, [Follow(kind="drug", value="continuous glucose monitor")])
    assert matches_follow(change, study, [Follow(kind="indication", value="type 2 diabetes mellitus")])
    assert study.phase == ""
    assert json.loads(study.conditions) == ["Type 2 Diabetes Mellitus"]
    assert json.loads(db.scalar(select(Snapshot)).raw_json) == record


def test_observational_record_without_phases_or_interventions_is_valid(raw):
    record = other_condition(raw)
    del record["protocolSection"]["armsInterventionsModule"]
    normalized = registry.normalize(record)
    assert normalized["phase"] == []
    assert normalized["drug"] == ""
    assert normalized["design"] == {"observationalModel": "COHORT", "timePerspective": "PROSPECTIVE"}


def test_regular_collection_covers_all_conditions_and_types_beyond_thirty_pages(db, raw, monkeypatch):
    record = other_condition(raw)
    collaborator_only = copy.deepcopy(record)
    collaborator_only["protocolSection"]["identificationModule"]["nctId"] = "NCT01234568"
    collaborator_only["protocolSection"]["sponsorCollaboratorsModule"]["leadSponsor"]["name"] = "Unrelated Sponsor"
    pages = [{"studies": [], "nextPageToken": f"page-{page}"} for page in range(31)]
    pages.append({"studies": [record, collaborator_only]})
    requests = registry_pages(monkeypatch, pages)

    assert registry.fetch_company(company(db)) == [record]
    assert len(requests) == 32
    assert requests[-1]["pageToken"] == "page-30"
    assert all("query.cond" not in request and "query.term" not in request for request in requests)


@pytest.mark.parametrize("bad_page", [
    [], {}, {"studies": {}}, {"studies": [], "nextPageToken": 3},
    {"studies": [], "nextPageToken": ""},
    {"studies": [], "nextPageToken": "already-seen"},
])
def test_bad_or_repeated_page_cursor_prevents_partial_company_import(db, raw, monkeypatch, bad_page):
    registry_pages(monkeypatch, [
        {"studies": [other_condition(raw)], "nextPageToken": "already-seen"}, bad_page,
    ])
    result = registry.sync_all(company(db).id)
    assert result["status"] == "partial"
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 0
    assert db.scalar(select(func.count()).select_from(Study)) == 0


def test_bulk_baseline_receives_non_oncology_observational_updates_on_regular_sync(db, raw, monkeypatch):
    record = other_condition(raw)
    sponsor = company(db)
    bulk_requests = bulk_pages(monkeypatch, [{"studies": [record]}])
    initial = bulk.run_bulk_download(company_ids=[sponsor.id])
    assert initial["status"] == "complete"
    assert initial["changes"] == 0
    assert "query.cond" not in bulk_requests[0]
    assert "query.term" not in bulk_requests[0]

    updated = copy.deepcopy(record)
    updated["protocolSection"]["designModule"]["enrollmentInfo"]["count"] = 125
    updated["protocolSection"]["designModule"]["designInfo"]["timePerspective"] = "RETROSPECTIVE"
    requests = registry_pages(monkeypatch, [{"studies": [updated]}])
    refreshed = registry.sync_all(sponsor.id)

    assert refreshed["status"] == "complete"
    assert refreshed["studies"] == 1
    assert refreshed["changes"] == 2
    assert "query.cond" not in requests[0] and "query.term" not in requests[0]
    db.expire_all()
    changes = db.scalars(select(Change)).all()
    assert {row.category for row in changes} == {"enrollment", "trial_design"}
    assert all(row.review_status == "automatic" and row.before_snapshot and row.after_snapshot for row in changes)
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 2
    assert db.get(Study, "NCT01234567").drug == "Continuous glucose monitor"
    assert db.get(Company, sponsor.id).last_checked


def test_reimport_repairs_intervention_index_without_changing_original_evidence(db, raw):
    record = other_condition(raw)
    sponsor = company(db)
    registry.ingest(db, sponsor, record)
    study = db.get(Study, "NCT01234567")
    snapshot = db.scalar(select(Snapshot))
    # Simulate the former drug-only index and normalized snapshot.
    study.drug = ""
    original = json.loads(snapshot.normalized)
    original["drug"] = ""
    snapshot.normalized = registry.canonical(original)
    original_normalized = snapshot.normalized
    db.commit()

    assert registry.ingest(db, sponsor, record) == 0
    assert study.drug == "Continuous glucose monitor"
    assert snapshot.normalized == original_normalized
    assert json.loads(snapshot.raw_json) == record
    assert db.scalar(select(func.count()).select_from(Snapshot)) == 1


def test_legacy_filtered_bulk_resume_keeps_original_query_and_freshness(db, raw, monkeypatch):
    sponsor = company(db)
    sponsor.last_checked = "2025-01-01T00:00:00+00:00"
    sponsor.last_error = "Previous full refresh failed"
    db.commit()
    cancel = threading.Event()
    initial_requests = bulk_pages(monkeypatch, [{"studies": [raw], "nextPageToken": "legacy-oncology-cursor"}])

    def cancel_after_saved_page(message):
        if message.startswith("Page "):
            cancel.set()

    initial = bulk.run_bulk_download(
        company_ids=[sponsor.id], oncology_only=True, cancel=cancel, emit=cancel_after_saved_page,
    )
    assert initial["status"] == "cancelled"
    resumed_requests = bulk_pages(monkeypatch, [{"studies": []}])
    result = bulk.run_bulk_download(resume_job_id=initial["job_id"])

    assert result["status"] == "complete"
    assert resumed_requests[0]["pageToken"] == "legacy-oncology-cursor"
    for key in ("query.spons", "query.cond", "query.term"):
        assert resumed_requests[0][key] == initial_requests[0][key]
    db.expire_all()
    assert json.loads(db.get(BulkCheckpoint, initial["job_id"]).payload)["oncology_only"] is True
    assert db.get(Company, sponsor.id).last_checked == "2025-01-01T00:00:00+00:00"
    assert db.get(Company, sponsor.id).last_error == "Previous full refresh failed"
