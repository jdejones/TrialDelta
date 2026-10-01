from sqlalchemy import select
from trialdelta.db import Company
from trialdelta.registry import ingest


def test_source_to_review_to_personal_digest(client, db, raw):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    ingest(db, company, raw)
    db.commit()
    assert (
        client.post(
            "/api/follows", json={"kind": "company", "value": str(company.id)}
        ).status_code
        == 200
    )
    statement = {
        "company_id": company.id,
        "study_id": "NCT01234567",
        "source_type": "presentation",
        "title": "Company presentation, page 12",
        "url": "https://example.org/primary-document",
        "published_at": "2026-01-01",
        "quote": "We expect the primary results in the first half of 2026.",
        "category": "timeline",
        "value": "H1 2026",
        "reason_checked": True,
    }
    baseline = client.post("/api/evidence", json=statement)
    assert baseline.status_code == 200
    bid = baseline.json()["id"]
    assert (
        client.post(f"/api/review/evidence/{bid}", json={"action": "approve"}).json()[
            "change_created"
        ]
        is False
    )
    statement.update(
        published_at="2026-03-01",
        quote="We now expect the primary results in the second half of 2026.",
        value="H2 2026",
    )
    current = client.post("/api/evidence", json=statement).json()
    assert current["precision"] == "half-year"
    assert client.get("/api/changes").json() == []
    assert client.post(
        f'/api/review/evidence/{current["id"]}', json={"action": "approve"}
    ).json()["change_created"]
    changes = client.get("/api/changes?followed=true").json()
    assert len(changes) == 1
    detail = client.get(f'/api/changes/{changes[0]["id"]}').json()
    assert len(detail["evidence"]) == 2
    assert detail["reconciliation"]["primary_completion"]["date"] == "2028-06"
    digest = client.get("/api/digest").json()
    assert len(digest["changes"]) == 1
    export = client.get("/api/digest/export").text
    assert "H1 2026" in export and "H2 2026" in export and "?change=" in export
    assert (
        client.post(
            f'/api/review/evidence/{current["id"]}', json={"action": "approve"}
        ).status_code
        == 409
    )
