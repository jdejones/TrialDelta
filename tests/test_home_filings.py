"""The homepage 8-K feed is read-only, bounded at source, and paginated."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import DateTime, bindparam, create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from trialdelta import home


@pytest.fixture
def saved_filings(monkeypatch):
    source = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    with source.begin() as connection:
        connection.execute(text("ATTACH DATABASE ':memory:' AS filings"))
        connection.execute(text(
            "CREATE TABLE filings.biotech_filings (symbol TEXT, form_type TEXT, filing TEXT, "
            "accession_no TEXT, filed_at DATETIME, filing_link TEXT)"
        ))
    queries = []
    event.listen(source, "before_cursor_execute", lambda conn, cursor, statement, params, context, many:
                 queries.append((statement, params)))
    monkeypatch.setattr(home, "engine", source)
    monkeypatch.setattr(home, "_source_engine", lambda: source)

    def populate(rows):
        with source.begin() as connection:
            connection.execute(text(
                "INSERT INTO filings.biotech_filings "
                "VALUES (:symbol, :form_type, :filing, :accession_no, :filed_at, :filing_link)"
            ).bindparams(bindparam("filed_at", type_=DateTime())), rows)
        queries.clear()

    yield source, populate, queries
    source.dispose()


def filing(accession, *, symbol="ARVN", form_type="8-K", filed_at=None, link=None):
    return {
        "symbol": symbol, "form_type": form_type,
        "filing": "Private full filing document; never sent to the browser.",
        "accession_no": accession, "filed_at": filed_at,
        "filing_link": link or f"https://www.sec.gov/Archives/{accession}",
    }


def test_filings_pagination_bounds_source_query_and_returns_only_metadata(client, saved_filings):
    _, populate, queries = saved_filings
    populate([
        filing(f"accession-{index:03}", filed_at=datetime(2026, 1, 1) + timedelta(days=index))
        for index in range(23)
    ])
    pages = [client.get("/api/home/filings", params={"page": page}) for page in (1, 2, 3)]
    assert all(response.status_code == 200 for response in pages)
    results = [response.json() for response in pages]
    assert [len(result["items"]) for result in results] == [10, 10, 3]
    assert [result["page"] for result in results] == [1, 2, 3]
    assert all(result["page_size"] == 10 and result["total"] == 23 and result["total_pages"] == 3
               for result in results)
    assert [row["accession_no"] for result in results for row in result["items"]] == [
        f"accession-{index:03}" for index in reversed(range(23))
    ]
    row = results[0]["items"][0]
    assert set(row) == {"ticker", "company", "form_type", "accession_no", "filed_at", "filing_link"}
    assert row["company"] == "Arvinas"
    assert row["filed_at"] == "2026-01-23T00:00:00"
    assert "Private full filing" not in pages[0].text
    assert len(queries) == 6
    assert all(statement.startswith("SELECT ") for statement, _ in queries)
    selects = [(statement, parameters) for statement, parameters in queries if "COUNT(*)" not in statement]
    assert len(selects) == 3
    assert all("LIMIT ? OFFSET ?" in statement for statement, _ in selects)
    assert [parameters[-2:] for _, parameters in selects] == [(10, 0), (10, 10), (10, 20)]
    assert all("filing," not in statement and "SELECT *" not in statement for statement, _ in queries)


@pytest.mark.parametrize("page_size", [10, 25, 50, 100])
def test_filings_supported_page_sizes_and_stale_page_clamping(client, saved_filings, page_size):
    _, populate, queries = saved_filings
    populate([filing(f"accession-{index:03}") for index in range(107)])
    response = client.get("/api/home/filings", params={"page": 999, "page_size": page_size})
    assert response.status_code == 200
    result = response.json()
    expected_pages = (107 + page_size - 1) // page_size
    assert result["page"] == result["total_pages"] == expected_pages
    assert result["page_size"] == page_size
    assert len(result["items"]) == 107 - (expected_pages - 1) * page_size
    assert queries[1][1][-2:] == (page_size, (expected_pages - 1) * page_size)


def test_filings_filter_exact_form_and_sort_dates_then_stable_metadata(client, saved_filings):
    _, populate, _ = saved_filings
    same_date = datetime(2026, 9, 27, 12, 0)
    populate([
        filing("c", symbol="ZZZ", filed_at=same_date),
        filing("b", filed_at=same_date),
        filing("c", symbol=" outside ", form_type=" 8-k ", filed_at=same_date),
        filing("undated", filed_at=None),
        filing("older", filed_at=datetime(2026, 9, 26)),
        filing("newest", filed_at=datetime(2026, 9, 28)),
        filing("amended", form_type="8-K/A", filed_at=datetime(2026, 9, 29)),
        filing("annual", form_type="10-K", filed_at=datetime(2026, 9, 29)),
        filing("quarterly", form_type="10-Q"),
        filing("missing", form_type=None),
    ])
    result = client.get("/api/home/filings").json()
    assert result["total"] == 6
    assert [row["accession_no"] for row in result["items"]] == ["newest", "c", "c", "b", "older", "undated"]
    assert [row["ticker"] for row in result["items"]][1:3] == ["OUTSIDE", "ZZZ"]
    assert result["items"][1]["company"] == "OUTSIDE"
    assert all(row["form_type"] == "8-K" for row in result["items"])
    assert result["items"][-1]["filed_at"] is None


@pytest.mark.parametrize(("query", "expected"), [
    ("  arvn  ", ["accession-one"]),
    ("ARVINAS", ["accession-one"]),
    ("ACCESSION-TWO", ["accession-two"]),
    ("%_!", ["literal-%_!-match"]),
    ("' OR 1=1 --", []),
])
def test_filings_search_is_applied_before_pagination_and_escaped(client, saved_filings, query, expected):
    _, populate, queries = saved_filings
    populate([
        filing("accession-one", symbol="ARVN"),
        filing("accession-two", symbol="OUTSIDE"),
        filing("literal-%_!-match", symbol="OUTSIDE"),
        filing("annual", symbol="ARVN", form_type="10-K"),
    ])
    result = client.get("/api/home/filings", params={"q": query}).json()
    assert [row["accession_no"] for row in result["items"]] == expected
    assert result["total"] == len(expected)
    assert all(query not in statement for statement, _ in queries)
    assert all("LIKE ? ESCAPE '!'" in statement for statement, _ in queries)


def test_filings_company_search_counts_and_pages_the_matching_filings(client, saved_filings):
    _, populate, queries = saved_filings
    populate([
        filing(f"matched-{index:02}", filed_at=datetime(2026, 1, 1) + timedelta(days=index))
        for index in range(12)
    ] + [
        filing(f"other-{index:02}", symbol="OUTSIDE", filed_at=datetime(2026, 9, 27))
        for index in range(15)
    ])
    response = client.get("/api/home/filings", params={"q": "Arvinas", "page": 2})
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == 12
    assert result["page"] == result["total_pages"] == 2
    assert [row["accession_no"] for row in result["items"]] == ["matched-01", "matched-00"]
    assert all("UPPER(TRIM(symbol)) IN (?)" in statement for statement, _ in queries)
    assert queries[-1][1][-2:] == (10, 10)


@pytest.mark.parametrize("link", ["javascript:alert(1)", "https://user:secret@example.com/", "https://[broken"])
def test_filings_unsafe_links_remain_visible_as_unlinked_metadata(client, saved_filings, link):
    _, populate, _ = saved_filings
    populate([filing("unsafe-link", link=link)])
    result = client.get("/api/home/filings").json()
    assert result["total"] == 1
    assert result["items"][0]["filing_link"] is None
    assert link not in str(result)


@pytest.mark.parametrize("params", [
    {"page": 0}, {"page": -1}, {"page": "bad"}, {"page": 1.5}, {"page": 2147483648},
    {"page_size": 0}, {"page_size": -1}, {"page_size": 11}, {"page_size": 101},
    {"page_size": "bad"}, {"page_size": 10.5}, {"q": "x" * 201},
])
def test_filings_validate_parameters_before_loading_source(client, monkeypatch, params):
    def unexpected_source():
        pytest.fail("Invalid pagination must not connect to the source database")

    monkeypatch.setattr(home, "_source_engine", unexpected_source)
    assert client.get("/api/home/filings", params=params).status_code == 422


def test_filings_unconfigured_source_is_empty(client, monkeypatch):
    monkeypatch.delenv("SOURCE_DATABASE_URL", raising=False)
    monkeypatch.setenv("USE_LOCAL_MYSQL", "false")
    response = client.get("/api/home/filings", params={"page": 4, "page_size": 25})
    assert response.status_code == 200
    assert response.json() == {"items": [], "page": 1, "page_size": 25, "total": 0, "total_pages": 0}


def test_filings_source_errors_are_sanitized(client, monkeypatch):
    def unavailable():
        raise OperationalError("mysql://user:secret@private-host", {}, Exception("private details"))

    monkeypatch.setattr(home, "_source_engine", unavailable)
    response = client.get("/api/home/filings")
    assert response.status_code == 503
    assert response.json() == {"detail": "8-K filings are temporarily unavailable."}
    assert "secret" not in response.text


@pytest.mark.parametrize("has_rows", [False, True])
def test_filings_dispose_separate_source_engine(client, saved_filings, monkeypatch, has_rows):
    source, populate, _ = saved_filings
    if has_rows:
        populate([filing("accession-one")])
    disposed = []
    monkeypatch.setattr(home, "engine", object())
    monkeypatch.setattr(source, "dispose", lambda: disposed.append(True))
    assert client.get("/api/home/filings").status_code == 200
    assert disposed == [True]


def test_filings_require_authentication_when_public_browsing_disabled(client, monkeypatch):
    import trialdelta.auth as auth

    monkeypatch.setattr(auth, "MODE", "hosted")
    monkeypatch.setattr(auth, "PUBLIC_BROWSING_ENABLED", False)
    response = client.get("/api/home/filings")
    assert response.status_code == 401
    assert response.json() == {"detail": "Please sign in"}
