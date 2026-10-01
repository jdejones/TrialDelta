"""Saved records across therapeutic areas reach the same customer workflows."""

import json
from copy import deepcopy

import pytest
from sqlalchemy import select

from trialdelta import auth
from trialdelta.db import Change, Company, Snapshot
from trialdelta.registry import ingest
from trialdelta.seed import seed


def save_trial(db, raw, condition="Type 2 Diabetes Mellitus", area="Metabolic"):
    company = Company(
        ticker="MULTI", name="Multispecialty Research", sponsor="Multispecialty Research",
        area=area,
    )
    db.add(company)
    db.flush()
    record = deepcopy(raw)
    protocol = record["protocolSection"]
    protocol["identificationModule"]["briefTitle"] = f"A study in {condition}"
    protocol["sponsorCollaboratorsModule"]["leadSponsor"]["name"] = company.sponsor
    protocol["conditionsModule"]["conditions"] = [condition]
    protocol["armsInterventionsModule"]["interventions"] = [
        {"type": "DRUG", "name": "MULTI-1"}
    ]
    assert ingest(db, company, record) == 0
    db.commit()
    return company, record


def save_change(db, company, record):
    updated = deepcopy(record)
    updated["protocolSection"]["designModule"]["enrollmentInfo"]["count"] = 160
    assert ingest(db, company, updated) == 1
    db.commit()
    return db.scalar(select(Change).where(Change.company_id == company.id))


def test_neutral_company_default_and_seed_preserve_explicit_classifications(db):
    unclassified = Company(ticker="NEW", name="New Research", sponsor="New Research")
    classified = Company(
        ticker="CURATED", name="Curated Research", sponsor="Curated Research", area="Oncology"
    )
    seeded = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    assert seeded.area == "Healthcare"
    seeded.area = "Oncology"
    db.add_all([unclassified, classified])
    db.commit()
    seed()
    db.expire_all()
    assert unclassified.area == "Healthcare"
    assert classified.area == "Oncology"
    assert seeded.area == "Oncology"


@pytest.mark.parametrize("area,condition", [
    ("Metabolic", "Type 2 Diabetes Mellitus"),
    ("Neurology", "Alzheimer Disease"),
    ("Immunology", "Rheumatoid Arthritis"),
    ("Infectious disease", "Influenza"),
    ("Cardiology", "Heart Failure"),
])
def test_non_oncology_records_reach_public_directory_search_home_and_history(
    client, db, raw, monkeypatch, area, condition,
):
    company, record = save_trial(db, raw, condition, area)
    event = save_change(db, company, record)
    study_id = record["protocolSection"]["identificationModule"]["nctId"]
    # Exercise visitor reads, which must not gain a private watchlist or require review.
    monkeypatch.setattr(auth, "AUTH_REQUIRED", True)
    monkeypatch.setattr(auth, "PUBLIC_BROWSING_ENABLED", True)

    directory = client.get("/api/companies").json()
    row = next(row for row in directory if row["id"] == company.id)
    assert row["area"] == area and row["study_count"] == 1
    for params in ({}, {"q": condition}, {"company_id": company.id}):
        rows = client.get("/api/studies", params=params).json()
        assert [row["id"] for row in rows] == [study_id]
        assert rows[0]["conditions"] == [condition]
    recent = client.get("/api/home/trials").json()
    assert [row["id"] for row in recent] == [study_id]
    assert recent[0]["conditions"] == [condition]

    detail = client.get(f"/api/studies/{study_id}").json()
    assert json.loads(detail["study"]["conditions"]) == [condition]
    assert detail["reconciliation"]["snapshot_count"] == 2
    assert [row["id"] for row in detail["changes"]] == [event.id]
    assert [row["id"] for row in client.get("/api/changes").json()] == [event.id]
    evidence = client.get(f"/api/changes/{event.id}").json()["evidence"]
    assert len(evidence) == 2
    assert all(condition in item["quote"] for item in evidence)
    first = db.scalar(select(Snapshot).where(Snapshot.study_id == study_id).order_by(Snapshot.id))
    assert client.get(f"/api/snapshots/{first.id}").json() == record


@pytest.mark.parametrize("kind", ["company", "program", "drug", "target", "indication"])
def test_non_oncology_follows_reach_change_feed_digest_and_export(client, db, raw, kind):
    company, record = save_trial(db, raw)
    event = save_change(db, company, record)
    study_id = record["protocolSection"]["identificationModule"]["nctId"]
    response = client.put(f"/api/studies/{study_id}/targets", json={"targets": ["GLP1R"]})
    assert response.status_code == 200
    assert response.json() == {"targets": ["GLP1R"]}
    assert [row["id"] for row in client.get("/api/studies", params={"q": "glp1r"}).json()] == [study_id]
    value = {
        "company": str(company.id), "program": study_id, "drug": "multi-1",
        "target": "glp1r", "indication": "type 2 diabetes mellitus",
    }[kind]
    followed = client.post("/api/follows", json={"kind": kind, "value": value})
    assert followed.status_code == 200
    assert [row["id"] for row in client.get("/api/changes?followed=true").json()] == [event.id]
    day = event.detected_at[:10]
    digest = client.get("/api/digest", params={"day": day}).json()
    assert digest["follow_count"] == 1
    assert [row["id"] for row in digest["changes"]] == [event.id]
    export = client.get("/api/digest/export", params={"day": day})
    assert export.status_code == 200
    assert "MULTI" in export.text and study_id in export.text
    assert "100 participants" in export.text and "160 participants" in export.text


def test_non_oncology_company_evidence_can_be_reviewed_and_published(client, db, raw):
    company, record = save_trial(db, raw)
    study_id = record["protocolSection"]["identificationModule"]["nctId"]
    evidence_ids = []
    for day, value in [("2026-01-01", "H1 2027"), ("2026-02-01", "H2 2027")]:
        response = client.post("/api/evidence", json={
            "company_id": company.id,
            "study_id": study_id,
            "source_type": "press_release",
            "title": "Diabetes trial readout guidance",
            "url": "https://example.org/diabetes-trial",
            "published_at": day,
            "quote": "The company expects diabetes trial results during " + value,
            "category": "timeline",
            "value": value,
        })
        assert response.status_code == 200, response.text
        evidence_id = response.json()["id"]
        evidence_ids.append(evidence_id)
        review = client.post(f"/api/review/evidence/{evidence_id}", json={"action": "approve"})
        assert review.status_code == 200, review.text
    rows = client.get("/api/changes?followed=false").json()
    assert len(rows) == 1
    assert rows[0]["before_value"] == "H1 2027" and rows[0]["after_value"] == "H2 2027"
    detail = client.get(f"/api/changes/{rows[0]['id']}").json()
    assert [row["id"] for row in detail["evidence"]] == evidence_ids
    assert len(client.get(f"/api/studies/{study_id}").json()["guidance"]) == 2
