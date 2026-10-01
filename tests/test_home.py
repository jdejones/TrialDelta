"""The Home feeds use saved records and preserve the browser/operator boundary."""

from copy import deepcopy

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from trialdelta import home
from trialdelta.app import app
from trialdelta.auth import browsing_user, current_user
from trialdelta.db import Company, Snapshot, SourceCandidate, Study, User
from trialdelta.registry import ingest


def test_trials_use_first_capture_not_latest_refresh(client, db, raw):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    first_dates = {
        "NCT01234561": "2026-01-01T12:00:00+00:00",
        "NCT01234563": "2026-09-23T12:00:00+00:00",
        "NCT01234562": "2026-09-23T12:00:00+00:00",
    }
    for study_id, added_at in first_dates.items():
        record = deepcopy(raw)
        record["protocolSection"]["identificationModule"]["nctId"] = study_id
        ingest(db, company, record)
        snapshot = db.scalar(select(Snapshot).where(Snapshot.study_id == study_id))
        snapshot.captured_at = added_at
        db.get(Study, study_id).last_checked = added_at
    refreshed = deepcopy(raw)
    refreshed["protocolSection"]["identificationModule"]["nctId"] = "NCT01234561"
    refreshed["protocolSection"]["statusModule"]["overallStatus"] = "COMPLETED"
    ingest(db, company, refreshed)
    latest_snapshot = db.scalar(
        select(Snapshot).where(Snapshot.study_id == "NCT01234561").order_by(Snapshot.id.desc())
    )
    latest_snapshot.captured_at = "2026-09-24T12:00:00+00:00"
    db.get(Study, "NCT01234561").last_checked = latest_snapshot.captured_at
    # A record without captured history has no defensible date of addition.
    db.add(Study(id="NCT01234564", company_id=company.id, title="No captured history"))
    db.commit()

    response = client.get("/api/home/trials")
    assert response.status_code == 200
    trials = response.json()
    assert [row["id"] for row in trials] == ["NCT01234562", "NCT01234563", "NCT01234561"]
    assert {row["id"]: row["added_at"] for row in trials} == first_dates
    assert trials[-1]["status"] == "COMPLETED"
    assert trials[-1]["last_checked"] > trials[0]["added_at"]
    assert trials[0]["company"] == "Arvinas"
    assert trials[0]["ticker"] == "ARVN"
    assert trials[0]["conditions"] == ["Solid Tumor"]
    assert isinstance(trials[0]["targets"], list)


@pytest.fixture
def saved_news(monkeypatch):
    source = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    with source.begin() as connection:
        connection.execute(text("ATTACH DATABASE ':memory:' AS news"))
        connection.execute(text("ATTACH DATABASE ':memory:' AS stocks"))
        connection.execute(text(
            "CREATE TABLE news.stock_news "
            "(id INTEGER PRIMARY KEY, Ticker TEXT, Title TEXT, Source TEXT, Date TEXT, Url TEXT)"
        ))
        connection.execute(text(
            "CREATE TABLE stocks.symbol_sector_industry "
            "(symbol TEXT PRIMARY KEY, sector TEXT, industry TEXT)"
        ))
        connection.execute(
            text("INSERT INTO stocks.symbol_sector_industry VALUES (:symbol, :sector, :industry)"),
            [
                {"symbol": "ARVN", "sector": "Healthcare", "industry": "Biotechnology"},
                {"symbol": "OUTSIDE", "sector": "Healthcare", "industry": " biotechnology "},
                {"symbol": "DEVICE", "sector": "Healthcare", "industry": "Medical Devices"},
                {"symbol": "TECH", "sector": "Technology", "industry": "Software"},
            ],
        )
        rows = [
            (1, "ARVN", "Older biotechnology article", "2026-09-21T10:00:00", "https://example.org/old"),
            (2, "ARVN", "New biotechnology article", "2026-09-23T10:00:00", "https://example.org/new"),
            (3, "OUTSIDE", "Biotechnology outside coverage", "2026-09-23T10:00:00", "https://example.org/outside"),
            (4, "DEVICE", "Healthcare device news", "2026-09-24T10:00:00", "https://example.org/device"),
            (5, "TECH", "Technology news", "2026-09-24T10:00:00", "https://example.org/tech"),
            (6, "UNKNOWN", "Unclassified news", "2026-09-24T10:00:00", "https://example.org/unknown"),
            (7, "ARVN", "Unsafe link", "2026-09-24T10:00:00", "javascript:alert(1)"),
            (8, "ARVN", "Credential link", "2026-09-24T10:00:00", "https://user:secret@example.org/"),
            (9, "ARVN", "Older duplicate", "2026-09-20T10:00:00", "https://example.org/new"),
            (10, "ARVN", "No publication date", None, "https://example.org/no-date"),
        ]
        connection.execute(
            text("INSERT INTO news.stock_news VALUES (:id, :ticker, :title, :source, :date, :url)"),
            [
                {"id": ident, "ticker": ticker, "title": title, "source": "Newswire",
                 "date": date, "url": url}
                for ident, ticker, title, date, url in rows
            ],
        )
    queries = []
    event.listen(source, "before_cursor_execute",
                 lambda conn, cursor, statement, parameters, context, many: queries.append(statement))
    monkeypatch.setattr(home, "engine", source)
    monkeypatch.setattr(home, "_source_engine", lambda: source)
    yield queries
    source.dispose()


def test_news_filters_classification_sorts_and_keeps_inbox_private(client, db, saved_news):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    db.add(SourceCandidate(
        company_id=company.id, title="Private operational candidate",
        url="https://example.org/private", identity="private-candidate"
    ))
    reader = User(email="reader@example.org", password_hash="unused", is_admin=False)
    db.add(reader)
    db.commit()
    app.dependency_overrides[current_user] = lambda: reader
    app.dependency_overrides[browsing_user] = lambda: reader

    response = client.get("/api/home/news")
    assert response.status_code == 200
    rows = response.json()
    assert [row["id"] for row in rows] == [3, 2, 1]
    assert rows[0]["company"] == "OUTSIDE"
    assert rows[1]["company"] == "Arvinas"
    assert all(set(row) == {"id", "title", "company", "ticker", "source", "url", "published_at"} for row in rows)
    assert "Private operational candidate" not in response.text
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in saved_news)
    assert client.get("/api/inbox").status_code == 404
    assert db.scalar(select(SourceCandidate)).status == "unreviewed"


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/html,hello", "file:///C:/private.txt",
    "//example.org/article", "https://user:secret@example.org/article",
    "https://@example.org/article", "https://[broken/article", "https://example.org:bad/article",
    "https://example.org/with\nnewline", "https://example.org\\@evil.example/",
    "", None,
])
def test_news_rejects_unsafe_or_malformed_links(url):
    assert home.safe_source_url(url) is None


def test_news_accepts_normal_web_links():
    assert home.safe_source_url("https://example.org/article?q=trial#results") == "https://example.org/article?q=trial#results"
    assert home.safe_source_url("http://example.org/article") == "http://example.org/article"


def test_unconfigured_news_is_empty(client, monkeypatch):
    monkeypatch.delenv("SOURCE_DATABASE_URL", raising=False)
    monkeypatch.setenv("USE_LOCAL_MYSQL", "false")
    assert client.get("/api/home/news").json() == []


def test_news_failure_is_sanitized_and_trials_still_load(client, monkeypatch):
    def unavailable():
        raise OperationalError("mysql://user:secret@private-host", {}, Exception("private details"))

    monkeypatch.setattr(home, "_source_engine", unavailable)
    response = client.get("/api/home/news")
    assert response.status_code == 503
    assert response.json() == {"detail": "Biotechnology news is temporarily unavailable."}
    assert "secret" not in response.text
    assert client.get("/api/home/trials").status_code == 200


@pytest.mark.parametrize("path", ["/api/home/trials", "/api/home/news"])
def test_home_feeds_require_authentication_when_public_browsing_disabled(client, monkeypatch, path):
    import trialdelta.auth as auth

    monkeypatch.setattr(auth, "MODE", "hosted")
    monkeypatch.setattr(auth, "PUBLIC_BROWSING_ENABLED", False)
    response = client.get(path)
    assert response.status_code == 401
    assert response.json() == {"detail": "Please sign in"}

