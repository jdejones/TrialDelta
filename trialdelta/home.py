"""Read-only Home feeds over records already stored by the owner's pipelines."""

import json
from urllib.parse import urlparse

from sqlalchemy import DateTime, bindparam, func, select, text

from .db import Company, Snapshot, Study, engine
from .services import as_dict
from .portfolio_sources import safe_source_url, source_engine, source_table, sqlite_public_urls


FEED_LIMIT = 100
NEWS_SCAN_LIMIT = 500
FILING_PAGE_SIZES = (10, 25, 50, 100)


def recent_trials(db):
    first_seen = (
        select(
            Snapshot.study_id,
            func.min(Snapshot.captured_at).label("added_at"),
        )
        .group_by(Snapshot.study_id)
        .subquery()
    )
    rows = db.execute(
        select(Study, Company, first_seen.c.added_at)
        .join(Company, Company.id == Study.company_id)
        .join(first_seen, first_seen.c.study_id == Study.id)
        .order_by(first_seen.c.added_at.desc(), Study.id)
        .limit(FEED_LIMIT)
    )
    return [
        {
            **as_dict(study),
            "conditions": json.loads(study.conditions),
            "targets": json.loads(study.targets),
            "company": company.name,
            "ticker": company.ticker,
            "added_at": added_at,
        }
        for study, company, added_at in rows
    ]


def _source_engine():
    return source_engine(engine)


def recent_filings(db, *, page=1, page_size=10, q=""):
    """Read one page of saved 8-K metadata without retrieving filing documents."""
    result = {"items": [], "page": 1, "page_size": page_size, "total": 0, "total_pages": 0}
    where = "UPPER(TRIM(form_type)) = :form_type"
    parameters = {"form_type": "8-K"}
    bindings = []
    search = q.strip().lower()
    if search:
        # Escape LIKE metacharacters so searches are literal, including accession
        # numbers and ticker punctuation. Values always remain bound parameters.
        parameters["search"] = "%" + search.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        company_tickers = db.scalars(
            select(func.upper(func.trim(Company.ticker)))
            .where(func.lower(Company.name).contains(search, autoescape=True))
        ).all()
        where += " AND (LOWER(symbol) LIKE :search ESCAPE '!' OR LOWER(accession_no) LIKE :search ESCAPE '!'"
        if company_tickers:
            where += " OR UPPER(TRIM(symbol)) IN :company_tickers"
            parameters["company_tickers"] = company_tickers
            bindings.append(bindparam("company_tickers", expanding=True))
        where += ")"
    source_engine = _source_engine()
    if source_engine is None:
        return result
    table = source_table(source_engine, "filings")
    try:
        with source_engine.connect() as connection:
            total = connection.execute(
                text("SELECT COUNT(*) FROM " + table + " WHERE " + where)
                .bindparams(*bindings), parameters
            ).scalar_one()
            total_pages = (total + page_size - 1) // page_size
            page = min(page, max(1, total_pages))
            result.update(page=page, total=total, total_pages=total_pages)
            if not total:
                return result
            rows = connection.execute(
                text(
                    "SELECT symbol, form_type, accession_no, filed_at, filing_link "
                    "FROM " + table + " WHERE " + where + " "
                    "ORDER BY filed_at IS NULL, filed_at DESC, accession_no DESC, "
                    "symbol, filing_link, form_type LIMIT :page_size OFFSET :offset"
                ).bindparams(*bindings).columns(filed_at=DateTime()),
                {**parameters, "page_size": page_size, "offset": (page - 1) * page_size},
            ).mappings().all()
    finally:
        if source_engine is not engine:
            source_engine.dispose()

    tickers = {(row["symbol"] or "").strip().upper() for row in rows}
    company_names = dict(db.execute(
        select(func.upper(Company.ticker), Company.name)
        .where(func.upper(Company.ticker).in_(tickers))
    ).all())
    for row in rows:
        ticker = (row["symbol"] or "").strip().upper()
        filed_at = row["filed_at"]
        result["items"].append({
            "ticker": ticker,
            "company": company_names.get(ticker, ticker),
            "form_type": (row["form_type"] or "").strip().upper(),
            "accession_no": row["accession_no"],
            "filed_at": filed_at.isoformat() if filed_at is not None else None,
            "filing_link": safe_source_url(row["filing_link"]),
        })
    return result


def recent_news(db):
    source_engine = _source_engine()
    if source_engine is None:
        return []
    if source_table(source_engine, "news") == "td_portfolio_news":
        if source_engine is not engine:
            source_engine.dispose()
        return paged_news(db, page_size=FEED_LIMIT)["items"]
    # This feed uses the biotechnology industry classification from the existing
    # security reference table, independent of any company therapeutic area.
    query = text(
        "SELECT n.id, n.`Ticker`, n.`Title`, n.`Source`, n.`Date`, n.`Url` "
        "FROM news.stock_news AS n "
        "WHERE n.`Ticker` IN ("
        "SELECT symbol FROM stocks.symbol_sector_industry "
        "WHERE LOWER(TRIM(industry)) = :industry) "
        "AND n.`Date` IS NOT NULL "
        "ORDER BY n.`Date` DESC, n.id DESC LIMIT :scan_limit"
    )
    try:
        with source_engine.connect() as connection:
            rows = connection.execute(
                query, {"industry": "biotechnology", "scan_limit": NEWS_SCAN_LIMIT}
            ).mappings().all()
    finally:
        if source_engine is not engine:
            source_engine.dispose()

    company_names = {
        company.ticker.upper(): company.name for company in db.scalars(select(Company))
    }
    result, seen = [], set()
    for row in rows:
        url = safe_source_url(row["Url"])
        ticker = (row["Ticker"] or "").strip().upper()
        if not url or not ticker or (ticker, url) in seen:
            continue
        seen.add((ticker, url))
        published = row["Date"]
        result.append(
            {
                "id": row["id"],
                "title": row["Title"] or "Untitled article",
                "company": company_names.get(ticker, ticker),
                "ticker": ticker,
                "source": row["Source"] or urlparse(url).hostname,
                "url": url,
                "published_at": published.isoformat()
                if hasattr(published, "isoformat")
                else str(published),
            }
        )
        if len(result) == FEED_LIMIT:
            break
    return result


def paged_news(db, *, page=1, page_size=10, q=""):
    """Browse every eligible snapshot headline, deduplicating before paging."""
    result = {"items": [], "page": 1, "page_size": page_size, "total": 0, "total_pages": 0}
    source = _source_engine()
    if source is None:
        return result
    table = source_table(source, "news")
    if table != "td_portfolio_news":
        if source is not engine:
            source.dispose()
        # The established workstation feed remains unchanged.
        rows = recent_news(db)
        search = q.strip().casefold()
        rows = [row for row in rows if not search or any(
            search in str(row.get(key, "")).casefold()
            for key in ("title", "ticker", "company", "source")
        )]
        total = len(rows)
        pages = (total + page_size - 1) // page_size
        page = min(page, max(1, pages))
        return {**result, "items": rows[(page - 1) * page_size:page * page_size],
                "page": page, "total": total, "total_pages": pages}

    cte = (
        "WITH candidates AS (SELECT id, Title, Source, Date, "
        "UPPER(TRIM(Ticker)) AS ticker, td_public_url(Url) AS url FROM " + table +
        " WHERE Date IS NOT NULL), ranked AS (SELECT *, ROW_NUMBER() OVER ("
        "PARTITION BY ticker, url ORDER BY Date DESC, id DESC) AS duplicate_rank "
        "FROM candidates WHERE url IS NOT NULL AND ticker <> ''), "
        "feed AS (SELECT * FROM ranked WHERE duplicate_rank = 1) "
    )
    where, parameters, bindings = "1=1", {}, []
    search = q.strip().lower()
    if search:
        parameters["search"] = "%" + search.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        where = "(LOWER(ticker) LIKE :search ESCAPE '!' OR LOWER(Title) LIKE :search ESCAPE '!' OR LOWER(Source) LIKE :search ESCAPE '!'"
        company_tickers = db.scalars(select(func.upper(Company.ticker)).where(
            func.lower(Company.name).contains(search, autoescape=True)
        )).all()
        if company_tickers:
            where += " OR ticker IN :company_tickers"
            parameters["company_tickers"] = company_tickers
            bindings.append(bindparam("company_tickers", expanding=True))
        where += ")"
    try:
        with source.connect() as connection:
            sqlite_public_urls(connection)
            total = connection.execute(
                text(cte + "SELECT COUNT(*) FROM feed WHERE " + where).bindparams(*bindings), parameters
            ).scalar_one()
            pages = (total + page_size - 1) // page_size
            page = min(page, max(1, pages))
            rows = connection.execute(text(
                cte + "SELECT id, ticker, Title, Source, Date, url FROM feed WHERE " + where +
                " ORDER BY Date DESC, id DESC LIMIT :limit OFFSET :offset"
            ).bindparams(*bindings), {**parameters, "limit": page_size, "offset": (page - 1) * page_size}).mappings().all()
    finally:
        if source is not engine:
            source.dispose()
    names = dict(db.execute(select(func.upper(Company.ticker), Company.name).where(
        func.upper(Company.ticker).in_([row["ticker"] for row in rows])
    )).all())
    result.update(page=page, total=total, total_pages=pages)
    result["items"] = [{
        "id": row["id"], "title": row["Title"] or "Untitled article",
        "ticker": row["ticker"], "company": names.get(row["ticker"], row["ticker"]),
        "source": row["Source"] or urlparse(row["url"]).hostname,
        "url": row["url"], "published_at": str(row["Date"]),
    } for row in rows]
    return result

