"""Hosted snapshot reads are complete, bounded and independent of local sources."""

import pytest
from sqlalchemy import event, select, text

from trialdelta import auth, portfolio_sources
from trialdelta.db import Change, Company, Evidence, Snapshot, Study, TargetActionDate, TargetActionDateEdit, engine


@pytest.fixture
def visitor(client, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_REQUIRED", True)
    monkeypatch.setattr(auth, "PUBLIC_BROWSING_ENABLED", True)
    return client


@pytest.fixture
def snapshot(visitor, monkeypatch):
    monkeypatch.setenv("APP_MODE", "hosted")
    monkeypatch.setenv("SOURCE_DATABASE_URL", "mysql+pymysql://unused:unused@private-workstation/news")
    monkeypatch.setattr(portfolio_sources, "create_engine", lambda *a, **k: pytest.fail("Hosted browsing opened an external source"))
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE td_portfolio_news (id INTEGER PRIMARY KEY, Ticker TEXT, Title TEXT, Source TEXT, Date TEXT, Url TEXT, Category TEXT, article_hash BLOB)"))
        connection.execute(text("CREATE TABLE td_portfolio_filings (id INTEGER PRIMARY KEY, symbol TEXT, form_type TEXT, filing TEXT, accession_no TEXT, filed_at TEXT, filing_link TEXT)"))
        connection.execute(text("CREATE TABLE td_portfolio_symbols (symbol TEXT PRIMARY KEY, sector TEXT, industry TEXT)"))
        connection.execute(text("INSERT INTO td_portfolio_news VALUES (:id,'DEVICE',:title,'Newswire',:date,:url,'private category',NULL)"), [
            {"id": index, "title": f"Healthcare device headline {index}",
             "date": f"2026-10-09T12:{index % 60:02}:00", "url": f"https://example.org/{index}"}
            for index in range(1, 131)
        ])
        connection.execute(text("INSERT INTO td_portfolio_news VALUES (:id,'DEVICE',:title,'Newswire','2026-10-02T00:00:00',:url,NULL,NULL)"), [
            {"id": 201, "title": "Older duplicate", "url": "https://example.org/1"},
            {"id": 202, "title": "Credential leak", "url": "https://user:secret@example.org/"},
            {"id": 203, "title": "Private host", "url": "http://127.0.0.1/private"},
            {"id": 204, "title": "Script", "url": "javascript:alert(1)"},
        ])
        connection.execute(text("INSERT INTO td_portfolio_filings VALUES (1,'ARVN','8-K','Private full HTML never sent','accession-one','2026-10-09T05:06:07','https://www.sec.gov/Archives/one')"))
    yield visitor
    with engine.begin() as connection:
        for table in ("td_portfolio_news", "td_portfolio_filings", "td_portfolio_symbols"):
            connection.execute(text("DROP TABLE " + table))


def test_news_all_symbols_and_all_pages_without_private_columns(snapshot):
    first = snapshot.get("/api/home/news/page").json()
    assert first["total"] == 130 and first["total_pages"] == 13
    assert len(first["items"]) == 10
    assert all(row["ticker"] == "DEVICE" for row in first["items"])
    rows = [row for page in range(1, 14) for row in snapshot.get(
        "/api/home/news/page", params={"page": page}
    ).json()["items"]]
    assert {row["id"] for row in rows} == set(range(1, 131))
    assert all(set(row) == {"id", "title", "ticker", "company", "source", "url", "published_at"} for row in rows)
    assert len(snapshot.get("/api/home/news").json()) == 100
    assert snapshot.get("/api/home/news/page", params={"q": "headline 130"}).json()["total"] == 1
    assert snapshot.get("/api/home/news/page", params={"q": "Older duplicate"}).json()["total"] == 0


def test_flat_filing_metadata_and_iso_dates(snapshot):
    response = snapshot.get("/api/home/filings")
    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["company"] == "Arvinas" and row["filed_at"] == "2026-10-09T05:06:07"
    assert "Private full HTML" not in response.text
    assert set(row) == {"ticker", "company", "form_type", "accession_no", "filed_at", "filing_link"}


def test_study_sql_search_counts_and_pagination(visitor, db):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    db.add_all([Study(id=f"NCT{index:08}", company_id=company.id,
                      title=f"Device program {index}", drug="DEVICE-1", conditions='["Heart disease"]',
                      targets='["MET"]', last_checked="2026-10-09T00:00:00+00:00")
                for index in range(125)])
    db.commit()
    first = visitor.get("/api/studies")
    assert first.headers["X-Total-Count"] == "125" and len(first.json()) == 100
    tail = visitor.get("/api/studies", params={"offset": 100})
    assert len(tail.json()) == 25
    assert set(row["id"] for row in first.json()).isdisjoint(row["id"] for row in tail.json())
    for search in ("Arvinas", "ARVN", "heart disease", "MET"):
        result = visitor.get("/api/studies", params={"q": search, "limit": 10})
        assert result.headers["X-Total-Count"] == "125" and len(result.json()) == 10
    assert visitor.get("/api/studies", params={"limit": 501}).status_code == 422


def test_changes_count_filter_page_and_private_fields(visitor, db):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    db.add_all([Change(company_id=company.id, category="timeline", title=f"Published {index}",
                       source="company", review_status="approved", before_value="H1", after_value="H2",
                       review_note="private operator note", reviewed_by=1,
                       detected_at="2026-10-09T00:00:00+00:00") for index in range(123)])
    db.commit()
    first = visitor.get("/api/changes")
    assert first.headers["X-Total-Count"] == "123" and len(first.json()) == 100
    assert len(visitor.get("/api/changes", params={"offset": 100}).json()) == 23
    assert "private operator note" not in first.text and "reviewed_by" not in first.text
    result = visitor.get("/api/changes", params={"q": "Published 122"})
    assert result.headers["X-Total-Count"] == "1"
    assert visitor.get("/api/changes", params={"followed": "true"}).headers["X-Total-Count"] == "0"
    assert "review_note" not in visitor.get(f'/api/changes/{result.json()[0]["id"]}').text


def test_theme_lookup_uses_full_dataset_and_exact_term_follow(client, db):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    db.add_all([
        Study(id="NCT00000001", company_id=company.id, title="First", drug=" A ; RET ",
              conditions='["Solid tumor"]', targets='["MET"]'),
        Study(id="NCT00000002", company_id=company.id, title="Later", drug="Z-OTHER",
              conditions='["Heart disease"]', targets='["Met", "RET"]'),
    ])
    db.commit()
    assert client.get("/api/themes", params={"kind": "target"}).json() == ["MET", "RET"]
    assert client.get("/api/themes", params={"kind": "drug", "q": "other"}).json() == ["Z-OTHER"]
    assert client.get("/api/themes", params={"kind": "indication", "q": "heart"}).json() == ["Heart disease"]
    assert client.get("/api/themes", params={"kind": "unknown"}).status_code == 422


def test_published_guidance_hides_attribution_and_private_links(visitor, db):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    db.add(Study(id="NCT00000001", company_id=company.id, title="Public study"))
    db.flush()
    db.add(Evidence(company_id=company.id, study_id="NCT00000001", source_type="filing",
                    title="Public evidence", url="http://localhost/private-document",
                    published_at="2026-10-09", quote="Public source quotation", category="timeline",
                    value="H2 2026", review_status="approved", created_by=100, reviewed_by=101))
    db.commit()
    guidance = visitor.get("/api/studies/NCT00000001").json()["guidance"][0]
    assert "created_by" not in guidance and "reviewed_by" not in guidance
    assert guidance["url"] is None and guidance["quote"] == "Public source quotation"


@pytest.mark.parametrize("url", [
    "https://example.org/source?token=private", "https://example.org/source?%61pi_key=private",
    "https://example.org/source?X-Amz-Signature=private", "http://workstation/private",
    "https://my%2Elocalhost/private", "https://workstation.lan/private", "https://workstation.home/private",
    "https://workstation.test/private", "https://workstation.invalid/private",
    "https://docs.google.com/document/private", "https://drive.google.com/file/private",
    "https://www.dropbox.com/private", "https://onedrive.live.com/private", "https://1drv.ms/private",
    "https://app.box.com/private",
])
def test_runtime_source_urls_cannot_publish_credentials_or_private_documents(url):
    assert portfolio_sources.safe_source_url(url) is None
    assert portfolio_sources.safe_source_url("https://www.sec.gov/Archives/edgar/data/123?output=1")


def test_study_detail_reads_snapshot_metadata_without_raw_blobs(visitor, db):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    db.add(Study(id="NCT00000001", company_id=company.id, title="Public study"))
    db.flush()
    db.add(Snapshot(study_id="NCT00000001", digest="a" * 64, raw_json='{"public":true}',
                    normalized='{"primary_completion":"2027-01"}', captured_at="2026-10-09T00:00:00+00:00",
                    source_updated="2026-10-08"))
    db.commit()
    statements = []

    def record_query(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_query)
    try:
        result = visitor.get("/api/studies/NCT00000001")
    finally:
        event.remove(engine, "before_cursor_execute", record_query)
    assert result.status_code == 200
    item = result.json()["snapshots"][0]
    assert set(item) == {"id", "study_id", "captured_at", "source_updated", "digest"}
    assert result.json()["reconciliation"]["primary_completion"] == "2027-01"
    assert all("raw_json" not in statement.lower() for statement in statements)


def test_calendar_corrected_month_grouping_counts_not_latest500(visitor, db):
    base = dict(symbol="ARVN", form_type="8-K", filing_sha256="a" * 64,
                precision="day", status="identified", matched_text="Target action date",
                reason="", start=0, end=1, context_start=0, context_end=1,
                context="Public filing excerpt")
    rows = [TargetActionDate(identity=f"{index:064x}", tad="2026-10-02", accession_no=f"a-{index}", **base)
            for index in range(1, 503)]
    corrected = TargetActionDate(identity="f" * 64, tad="2099-01-01", accession_no="corrected", **base)
    db.add_all(rows + [corrected])
    db.flush()
    db.add(TargetActionDateEdit(target_identity=corrected.identity, revision_token="e" * 64,
                               symbol="ARVN", tad="2026-10", precision="month", source_url="",
                               notes="Corrected public context", entered_by=1))
    db.commit()
    result = visitor.get("/api/home/tads/page", params={"month": "2026-10", "status": "identified", "limit": 1}).json()
    assert result["total"] == 2 and result["identified"] == 2 and result["month"] == 1
    second = visitor.get("/api/home/tads/page", params={"month": "2026-10", "status": "identified", "offset": 1, "limit": 1}).json()
    assert result["items"][0]["source_count"] == 502
    assert second["items"][0]["tad"] == "2026-10" and second["items"][0]["is_edited"]
    assert visitor.get("/api/home/tads/page", params={"month": "2099-01"}).json()["total"] == 0
    assert visitor.get("/api/home/tads/page", params={"q": "Arvinas"}).json()["total"] == 2
    assert visitor.get("/api/home/tads/page", params={"month": "2026-13"}).status_code == 422
