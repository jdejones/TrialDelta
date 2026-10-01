"""Optional read-only bridge to the user's existing news pipeline.

Headlines are discovery candidates, never evidence of a clinical change.
Only the application's own candidate metadata is written to trialdelta.
"""

import hashlib
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from sqlalchemy import (
    select,
    text,
    bindparam,
    create_engine,
)
from .db import Company, SourceCandidate, Session, engine


def import_news():
    source_url = os.getenv("SOURCE_DATABASE_URL")
    if source_url:
        source_engine = create_engine(source_url, pool_pre_ping=True)
    elif os.getenv("USE_LOCAL_MYSQL", "false").lower() == "true":
        source_engine = engine
    else:
        return {
            "status": "unconfigured",
            "imported": 0,
            "detail": "Set SOURCE_DATABASE_URL for the optional read-only news bridge.",
        }
    with Session() as db:
        companies = {c.ticker: c for c in db.scalars(select(Company))}
        query = text(
            "SELECT `Ticker`, `Title`, `Source`, `Date`, `Url` FROM news.stock_news WHERE `Ticker` IN :tickers AND `Date` >= :since ORDER BY `Date` DESC LIMIT 500"
        ).bindparams(bindparam("tickers", expanding=True))
        try:
            with source_engine.connect() as connection:
                rows = (
                    connection.execute(
                        query,
                        {
                            "tickers": list(companies),
                            "since": (
                                datetime.now(timezone.utc) - timedelta(days=30)
                            ).replace(tzinfo=None),
                        },
                    )
                    .mappings()
                    .all()
                )
        finally:
            if source_engine is not engine:
                source_engine.dispose()
        count = 0
        for row in rows:
            url = row["Url"] or ""
            parsed = urlparse(url)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                continue
            key = hashlib.sha256((row["Ticker"] + "|" + url).encode()).hexdigest()
            if db.scalar(
                select(SourceCandidate).where(SourceCandidate.identity == key)
            ):
                continue
            published = (
                row["Date"].isoformat()
                if hasattr(row["Date"], "isoformat")
                else str(row["Date"])
            )
            db.add(
                SourceCandidate(
                    company_id=companies[row["Ticker"]].id,
                    title=row["Title"] or "Untitled source",
                    source=(row["Source"] or "")[:200],
                    url=url,
                    published_at=published,
                    identity=key,
                )
            )
            count += 1
        db.commit()
        return {"status": "complete", "imported": count, "window_days": 30}
