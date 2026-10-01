"""Recognize one committed biotechnology filing, save its results, then exit.

News Tracker starts a new process for each filing and supplies its HTML/metadata
over stdin. This module never watches a stream, polls for work, or scans history.
The existing administrator import handles historical or failed-filing recovery.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Column, Float, Index, Integer, MetaData, String, Table, Text, create_engine,
    inspect, select, text, update,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


JOBS = Table(
    "td_tad_filing_jobs", MetaData(),
    Column("identity", String(64), primary_key=True),
    Column("state", String(16), nullable=False),
    Column("payload", Text().with_variant(LONGTEXT(), "mysql")),
    Column("attempts", Integer, nullable=False, default=0),
    # Kept for compatibility with existing receipt tables; no retry scheduler.
    Column("retry_at", Float, nullable=False, default=0),
    Column("matched", Integer, nullable=False, default=0),
    Column("identified", Integer, nullable=False, default=0),
    Column("unresolved", Integer, nullable=False, default=0),
)
Index("ix_td_tad_filing_jobs_ready", JOBS.c.state, JOBS.c.retry_at)
SOURCE_QUERY = text(
    "SELECT symbol, form_type, filing, accession_no, filed_at, filing_link "
    "FROM filings.biotech_filings"
)


def normalize_filing(row):
    """Match the source representation used by the shared filing importer."""
    filed_at = row.get("filed_at")
    if isinstance(filed_at, datetime):
        filed_at = filed_at.isoformat()
    elif filed_at:
        try:
            filed_at = datetime.fromisoformat(str(filed_at).replace("Z", "+00:00")).isoformat()
        except ValueError:
            # Preserve legacy zero dates as failure evidence. The single-filing
            # validation reports failure without inventing a source timestamp.
            filed_at = str(filed_at)
    return {
        "symbol": (row.get("symbol") or "").strip().upper(),
        "form_type": (row.get("form_type") or "").strip().upper(),
        "filing": row.get("filing") or "",
        "accession_no": (row.get("accession_no") or "").strip() or None,
        "filed_at": filed_at or None,
        "filing_link": row.get("filing_link") or None,
    }


def filing_identity(row):
    values = dict(row)
    values["filing"] = hashlib.sha256(values["filing"].encode("utf-8")).hexdigest()
    return hashlib.sha256(json.dumps(
        values, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


class FilingRecognitionWorker:
    """Own destination transactions; never alter a source filing."""

    def __init__(self, destination, save_filing, *, log=print):
        if destination.dialect.name == "mysql" and destination.url.database != "trialdelta":
            raise ValueError("Recognition writes require the trialdelta database.")
        self.destination = destination
        self.save_filing = save_filing
        self.log = log

    def initialize(self):
        # The app owns its date schema and migrations. This worker only creates
        # its own additive receipt table, never initializes the rest of the app.
        if not inspect(self.destination).has_table("td_target_action_dates"):
            raise RuntimeError("Start the TrialDelta backend once to initialize saved dates.")
        JOBS.create(self.destination, checkfirst=True)

    def enqueue(self, row):
        payload = normalize_filing(row)
        identity = filing_identity(payload)
        try:
            with self.destination.begin() as connection:
                if connection.scalar(select(JOBS.c.identity).where(JOBS.c.identity == identity)):
                    return False
                connection.execute(JOBS.insert().values(
                    identity=identity, state="pending", payload=json.dumps(payload, ensure_ascii=False),
                    attempts=0, retry_at=0, matched=0, identified=0, unresolved=0,
                ))
        except IntegrityError:
            # Multiple GUI instances can deliver the same stored filing.
            with self.destination.connect() as connection:
                if not connection.scalar(select(JOBS.c.identity).where(JOBS.c.identity == identity)):
                    raise
            return False
        return True

    def process_filing(self, row):
        """Attempt only this filing; other saved jobs are never polled/drained."""
        payload = normalize_filing(row)
        identity = filing_identity(payload)
        self.enqueue(payload)
        try:
            with Session(self.destination) as db, db.begin():
                job = db.execute(select(JOBS).where(
                    JOBS.c.identity == identity,
                ).with_for_update()).mappings().one()
                if job["state"] == "complete":
                    self.log("[Target action dates] Filing already processed.")
                    return True
                result = self.save_filing(db, payload)
                db.execute(update(JOBS).where(JOBS.c.identity == identity).values(
                    state="complete", payload=None, attempts=job["attempts"] + 1,
                    retry_at=0, matched=result["matched"], identified=result["identified"],
                    unresolved=result["unresolved"],
                ))
        except Exception:
            # The date inserts and successful receipt roll back together. Keep
            # failure evidence without starting a retry timer or another worker.
            with self.destination.begin() as connection:
                job = connection.execute(select(JOBS).where(
                    JOBS.c.identity == identity,
                ).with_for_update()).mappings().one()
                if job["state"] == "complete":
                    return True
                connection.execute(update(JOBS).where(JOBS.c.identity == identity).values(
                    state="failed", attempts=job["attempts"] + 1, retry_at=0,
                ))
            self.log("[Target action dates] Recognition failed; filing retained. Retry with Import stored filings in the backend.")
            return False
        self.log(
            f"[Target action dates] Filing complete: {result['identified']} identified, "
            f"{result['unresolved']} unresolved; {result['imported']} new results, "
            f"{result['existing']} already saved."
        )
        return True


def configured_destination(environ=None):
    """Fail closed: this service must never fall back to a local SQLite file."""
    values = os.environ if environ is None else environ
    if values.get("USE_LOCAL_MYSQL", "false").lower() == "true":
        password = values.get("gptdb")
        if not password:
            raise ValueError("TrialDelta gptdb password environment variable is missing.")
        destination = URL.create(
            "mysql+pymysql", username=values.get("MYSQL_USER", "gptdb"), password=password,
            host=values.get("MYSQL_HOST", "127.0.0.1"), port=int(values.get("MYSQL_PORT", "3306")),
            database="trialdelta", query={"charset": "utf8mb4"},
        )
    else:
        value = values.get("DATABASE_URL")
        if not value:
            raise ValueError("An explicit TrialDelta MySQL destination is required.")
        destination = make_url(value)
    if destination.drivername != "mysql+pymysql" or destination.database != "trialdelta":
        raise ValueError("The recognition destination must be mysql+pymysql and database trialdelta.")
    return destination


def configured_urls(environ=None):
    """Source access is needed only for the explicit read-only --check command."""
    values = os.environ if environ is None else environ
    destination = configured_destination(values)
    source = make_url(values.get("SOURCE_DATABASE_URL") or destination)
    if source.drivername != "mysql+pymysql":
        raise ValueError("A MySQL source connection is required.")
    return destination, source


def read_filing(stream):
    """Accept exactly one filing object; reject old streaming/control messages."""
    message = json.load(stream)
    if not isinstance(message, dict) or message.get("command") != "filing":
        raise ValueError("Expected a single filing message.")
    row = message.get("row")
    fields = {"symbol", "form_type", "filing", "accession_no", "filed_at", "filing_link"}
    if not isinstance(row, dict) or not fields.issubset(row):
        raise ValueError("Filing metadata is incomplete.")
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--once", action="store_true", help="Read one filing from stdin, save its results, and exit.")
    modes.add_argument("--check", action="store_true", help="Validate connections and existing tables without writing or importing.")
    args = parser.parse_args(argv)
    destination = source = None
    log = lambda message: print(message, flush=True)
    try:
        row = read_filing(sys.stdin) if args.once else None
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
        destination_url = configured_destination()
        options = {"pool_pre_ping": True, "connect_args": {"connect_timeout": 5, "read_timeout": 30, "write_timeout": 30}}
        destination = create_engine(destination_url, **options)
        if args.check:
            _, source_url = configured_urls()
            source = create_engine(source_url, **options)
            with destination.connect() as connection:
                if connection.scalar(text("SELECT DATABASE()")) != "trialdelta":
                    raise ValueError("Wrong destination database")
                connection.execute(text("SELECT id FROM td_target_action_dates LIMIT 0"))
            with source.connect() as connection:
                connection.execute(text(str(SOURCE_QUERY) + " LIMIT 0"))
            log("[Target action dates] Ready: destination trialdelta, source filings.biotech_filings. No changes made.")
            return 0

        # Only import application persistence after the explicit database guard.
        from .tad import save_filing_target_action_dates
        worker = FilingRecognitionWorker(destination, save_filing_target_action_dates, log=log)
        worker.initialize()
        return 0 if worker.process_filing(row) else 1
    except Exception:
        log("[Target action dates] Filing recognition/check failed. Check the input, TrialDelta MySQL configuration, and saved-date tables. No SQLite fallback is allowed. Saved filings can be retried with Import stored filings.")
        return 1
    finally:
        if source is not None:
            source.dispose()
        if destination is not None:
            destination.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
