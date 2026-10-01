"""Read-only Home feeds over records already stored by the owner's pipelines."""

import json
import os
from urllib.parse import urlparse

from sqlalchemy import DateTime, bindparam, create_engine, func, select, text

from .db import Company, Snapshot, Study, engine
from .services import as_dict


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
    source_url = os.getenv("SOURCE_DATABASE_URL")
    if source_url:
        return create_engine(source_url, pool_pre_ping=True)
    if os.getenv("USE_LOCAL_MYSQL", "false").lower() == "true":
        return engine
    return None


def safe_source_url(value):
    """Only expose ordinary web links, even if the source data is malformed."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or "\\" in value or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlparse(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        parsed.port  # Validate malformed ports without exposing the parse error.
    except ValueError:
        return None
    return value


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
    try:
        with source_engine.connect() as connection:
            total = connection.execute(
                text("SELECT COUNT(*) FROM filings.biotech_filings WHERE " + where)
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
                    "FROM filings.biotech_filings WHERE " + where + " "
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

