"""Build an offline, sanitized portfolio SQLite artifact from read-only MySQL.

Run from the repository root. Credentials are read from the existing environment
and .env; neither credentials nor source endpoints are written to the manifest.
This command never initializes the source application or modifies source data.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pymysql
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
NEWS_START = "2026-10-02 00:00:00"
NEWS_END_EXCLUSIVE = "2026-10-10 00:00:00"
RESEARCH_TABLES = (
    "td_companies", "td_studies", "td_snapshots", "td_evidence", "td_changes",
    "td_source_inbox", "td_target_action_dates", "td_target_action_date_edits",
)
EXCLUDED_TABLES = (
    "td_users", "td_account_profiles", "td_account_tokens", "td_sessions",
    "td_auth_rate_limits", "td_follows", "td_digest_deliveries", "td_jobs",
    "td_bulk_checkpoints", "td_tad_filing_jobs",
)
PUBLIC_EVIDENCE_DOMAINS = (
    "sec.gov", "fda.gov", "clinicaltrials.gov", "prnewswire.com",
    "globenewswire.com", "businesswire.com", "reuters.com", "finance.yahoo.com",
    "biospace.com", "fiercebiotech.com", "stocktitan.net", "benzinga.com",
)
SYMBOL_UNION = """(
    SELECT ticker AS symbol FROM trialdelta.td_companies
    UNION SELECT symbol FROM trialdelta.td_target_action_dates
    UNION SELECT symbol FROM trialdelta.td_target_action_date_edits
    UNION SELECT symbol FROM filings.biotech_filings
) AS symbols"""


class ExportError(RuntimeError):
    """A safe error code, never source data or database exception text."""


def public_url(value):
    """Retain ordinary public web links, rejecting local paths and credentials."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or "\\" in value or any(c.isspace() or ord(c) < 32 for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").lower()
        parsed.port
        if (
            parsed.scheme not in ("http", "https") or not host
            or parsed.username is not None or parsed.password is not None
            or "%" in host
        ):
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            if "." not in host or host.endswith((".local", ".localhost", ".internal", ".lan", ".home", ".test", ".invalid")):
                return ""
        if any(host == domain or host.endswith("." + domain) for domain in ("drive.google.com", "docs.google.com", "dropbox.com", "onedrive.live.com", "1drv.ms", "box.com")):
            return ""
        # These commonly carry access credentials rather than public provenance.
        query_keys = {key.lower() for key, _ in parse_qsl(parsed.query)}
        if query_keys.intersection({"token", "access_token", "api_key", "apikey", "password", "secret", "signature", "x-amz-signature"}):
            return ""
        return value
    except (TypeError, ValueError):
        return ""


def normalized_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return value


def verified_evidence_url(value, company_website=""):
    safe = public_url(value)
    if not safe:
        return False
    host = urlsplit(safe).hostname.lower()
    if any(host == domain or host.endswith("." + domain) for domain in PUBLIC_EVIDENCE_DOMAINS):
        return True
    issuer = public_url(company_website)
    if not issuer:
        return False
    issuer_host = urlsplit(issuer).hostname.lower().removeprefix("www.")
    return host == issuer_host or host.endswith("." + issuer_host)


def sanitized_row(table, row):
    result = {key: normalized_value(value) for key, value in row.items()}
    for key in ("website", "url", "source_url", "Url"):
        if key in result:
            result[key] = public_url(result[key])
    if "filing_link" in result:
        result["filing_link"] = public_url(result["filing_link"]) or None
    if table == "td_companies":
        result["last_error"] = None
    if "reviewed_by" in result:
        result["reviewed_by"] = None
    if "created_by" in result:
        result["created_by"] = 0  # Neutral imported provenance, no source identity.
    if "entered_by" in result:
        result["entered_by"] = 0 if table == "td_target_action_date_edits" else None
    if table == "td_changes":
        result["review_note"] = None
    if table == "td_evidence":
        result["reason"] = None  # Free-form author notes are not public provenance.
    if table == "td_target_action_date_edits":
        result["notes"] = ""
    if table == "td_target_action_dates" and result.get("entry_method") == "manual":
        result["context"] = ""  # Manual context holds administrator-written notes.
        result["reason"] = ""
        result["matched_text"] = ""
    return result


def hash_row(digest, values):
    def encode(value):
        return {"binary_hex": value.hex()} if isinstance(value, bytes) else value
    digest.update(json.dumps([encode(v) for v in values], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\n")


def storage_budget(database_bytes, target_disk_gb=60):
    """Conservative decimal GB budget: database, one copy, OS, and spare space."""
    target = target_disk_gb * 1_000_000_000
    required = database_bytes * 2 + 8_000_000_000 + 15_000_000_000
    return {
        "target_disk_bytes": target, "database_bytes": database_bytes,
        "backup_copy_bytes": database_bytes, "os_and_docker_allowance_bytes": 8_000_000_000,
        "free_headroom_bytes": 15_000_000_000, "required_bytes": required,
        "remaining_budget_bytes": target - required, "passed": required <= target,
    }


def create_schema(path):
    # Set these before importing db.py: it must never construct a MySQL engine.
    os.environ["USE_LOCAL_MYSQL"] = "false"
    os.environ["DATABASE_URL"] = "sqlite:///" + path.as_posix()
    sys.path.insert(0, str(ROOT))
    from sqlalchemy import create_engine
    from trialdelta.db import Base

    destination_engine = create_engine(os.environ["DATABASE_URL"])
    Base.metadata.create_all(destination_engine)
    destination_engine.dispose()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS td_portfolio_news (
            id INTEGER PRIMARY KEY, Ticker TEXT, Title TEXT, Source TEXT,
            Date TEXT, Url TEXT, Category TEXT, article_hash BLOB
        );
        CREATE INDEX IF NOT EXISTS ix_td_portfolio_news_date ON td_portfolio_news(Date DESC,id DESC);
        CREATE INDEX IF NOT EXISTS ix_td_portfolio_news_ticker ON td_portfolio_news(Ticker,Date);
        CREATE TABLE IF NOT EXISTS td_portfolio_filings (
            id INTEGER PRIMARY KEY, symbol TEXT, form_type TEXT, filing TEXT,
            accession_no TEXT, filed_at TEXT, filing_link TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_td_portfolio_filings_date ON td_portfolio_filings(filed_at DESC);
        CREATE INDEX IF NOT EXISTS ix_td_portfolio_filings_symbol ON td_portfolio_filings(symbol);
        CREATE INDEX IF NOT EXISTS ix_td_portfolio_filings_feed ON td_portfolio_filings(
            UPPER(TRIM(form_type)), (filed_at IS NULL), filed_at DESC,
            accession_no DESC, symbol, filing_link, form_type
        );
        CREATE TABLE IF NOT EXISTS td_portfolio_symbols (
            symbol TEXT PRIMARY KEY, sector TEXT, industry TEXT
        );
    """)
    return connection, Base


def protected_patterns(source):
    """Read private email strings into memory solely to detect accidental copies."""
    with source.cursor() as cursor:
        cursor.execute("SELECT email FROM trialdelta.td_users")
        private_emails = [row[0].casefold() for row in cursor.fetchall() if row[0]]
    workstation_home = str(Path.home()).casefold()
    paths = [workstation_home, workstation_home.replace("\\", "/"), str(ROOT.parent).casefold(), "e:\\market research", "e:/market research"]
    paths += [json.dumps(path)[1:-1] for path in paths]
    secret = os.getenv("gptdb", "")
    return private_emails, paths, secret


def privacy_match_counts(values, patterns):
    emails, paths, secret = patterns
    counts = {"private_email": 0, "workstation_path": 0, "source_credential": 0, "local_database_ip": 0}
    for value in values:
        if not isinstance(value, str):
            continue
        lowered = value.lower()
        counts["private_email"] += int(any(email in lowered for email in emails))
        counts["workstation_path"] += int(any(path in lowered for path in paths))
        counts["source_credential"] += int(bool(secret and secret in value))
        # Literal prefilter keeps the regex off multi-gigabyte public filings.
        if "127.0.0.1" in value or "[::1]" in value:
            counts["local_database_ip"] += int(bool(re.search(r"(?<![0-9])127\.0\.0\.1(?![0-9])|https?://\[::1\]", value, re.I)))
    return counts


def finalize_artifact(database_path):
    """Apply stricter redaction without rereading or re-exporting the source."""
    database_path = Path(database_path).resolve()
    manifest_path = database_path.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    load_dotenv(ROOT / ".env")
    source = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"), port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "gptdb"), password=os.getenv("gptdb"), database="trialdelta",
        charset="utf8mb4", connect_timeout=15, read_timeout=60, autocommit=False,
    )
    with source.cursor() as cursor:
        cursor.execute("SET SESSION TRANSACTION READ ONLY")
    try:
        patterns = protected_patterns(source)
    finally:
        source.rollback()
        source.close()
    destination, base = create_schema(database_path)
    destination.execute("PRAGMA secure_delete=ON")
    removed_url_count = 0
    url_fields = {
        "td_companies": ("website",), "td_evidence": ("url",),
        "td_source_inbox": ("url",), "td_target_action_dates": ("filing_link",),
        "td_target_action_date_edits": ("source_url",),
        "td_portfolio_news": ("Url",), "td_portfolio_filings": ("filing_link",),
    }
    for table, fields in url_fields.items():
        for field in fields:
            for row_id, original in destination.execute(f'SELECT id,"{field}" FROM "{table}"').fetchall():
                replacement = public_url(original)
                if field == "filing_link":
                    replacement = replacement or None
                if replacement != original:
                    destination.execute(f'UPDATE "{table}" SET "{field}"=? WHERE id=?', (replacement, row_id))
                    removed_url_count += 1
    withheld_evidence = []
    manual_count = destination.execute("SELECT COUNT(*) FROM td_target_action_dates WHERE entry_method='manual'").fetchone()[0]
    destination.execute("UPDATE td_target_action_dates SET context='',reason='',matched_text='',entered_by=NULL WHERE entry_method='manual'")
    for evidence_id, url, website in destination.execute("SELECT e.id,e.url,c.website FROM td_evidence e JOIN td_companies c ON c.id=e.company_id").fetchall():
        if not verified_evidence_url(url, website):
            withheld_evidence.append(evidence_id)
            destination.execute("UPDATE td_evidence SET title='Source withheld from portfolio snapshot',quote='',value='',url='',reason=NULL,review_status='rejected',reviewed_by=NULL,created_by=0 WHERE id=?", (evidence_id,))
    withheld_changes = 0
    if withheld_evidence:
        placeholders = ",".join("?" for _ in withheld_evidence)
        parameters = tuple(withheld_evidence) * 2
        condition = f"before_evidence IN ({placeholders}) OR after_evidence IN ({placeholders})"
        withheld_changes = destination.execute("SELECT COUNT(*) FROM td_changes WHERE " + condition, parameters).fetchone()[0]
        destination.execute("UPDATE td_changes SET title='Source withheld from portfolio snapshot',before_value=NULL,after_value=NULL,review_status='rejected',reviewed=0,review_note=NULL,reviewed_by=NULL WHERE " + condition, parameters)
    destination.commit()
    # Current Base indexes may have been added while the large export streamed.
    from sqlalchemy import create_engine
    index_engine = create_engine("sqlite:///" + database_path.as_posix())
    for table in base.metadata.tables.values():
        for index in table.indexes:
            index.create(index_engine, checkfirst=True)
    index_engine.dispose()
    pattern_counts = {"private_email": 0, "workstation_path": 0, "source_credential": 0, "local_database_ip": 0}
    for table, info in manifest["tables"].items():
        columns = info["columns"]
        column_sql = ",".join('"' + column + '"' for column in columns)
        order = "symbol" if table == "td_portfolio_symbols" else "id"
        digest = hashlib.sha256()
        count = 0
        for values in destination.execute(f'SELECT {column_sql} FROM "{table}" ORDER BY "{order}"'):
            hash_row(digest, values)
            matches = privacy_match_counts(values, patterns)
            for kind in pattern_counts:
                pattern_counts[kind] += matches[kind]
            count += 1
        if count != info["rows"]:
            raise ExportError("post_sanitization_count_mismatch")
        if table in ("td_studies", "td_snapshots", "td_portfolio_symbols") and digest.hexdigest() != info["sanitized_rows_sha256"]:
            raise ExportError("unchanged_research_checksum_mismatch")
        info.setdefault("export_sanitized_rows_sha256", info["sanitized_rows_sha256"])
        info["sanitized_rows_sha256"] = digest.hexdigest()
        print(json.dumps({"status": "privacy_and_checksum_verified", "table": table, "rows": count}), flush=True)
    if any(pattern_counts.values()):
        print(json.dumps({"status": "privacy_check_failed", "protected_source_pattern_match_counts": pattern_counts}), flush=True)
        raise ExportError("protected_source_text_detected")
    verify_privacy(destination)
    if destination.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ExportError("foreign_key_check_failed")
    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ExportError("sqlite_integrity_failed")
    news_stats = destination.execute("SELECT COUNT(*),COUNT(DISTINCT Ticker),MIN(Date),MAX(Date) FROM td_portfolio_news").fetchone()
    manifest["news_coverage"] = dict(zip(("article_count", "symbols_with_news", "first_stored_datetime", "last_stored_datetime"), news_stats))
    manifest["news_coverage"]["migrated_dataset_symbols"] = destination.execute("SELECT COUNT(*) FROM td_portfolio_symbols").fetchone()[0]
    destination.close()
    budget = storage_budget(database_path.stat().st_size, manifest["storage_budget"]["target_disk_bytes"] // 1_000_000_000)
    if not budget["passed"]:
        raise ExportError("target_disk_headroom_insufficient")
    with database_path.open("rb") as file:
        manifest["artifact_sha256"] = hashlib.file_digest(file, "sha256").hexdigest()
    manifest["storage_budget"] = budget
    manifest["additional_sanitization"] = {
        "manual_date_freeform_text_cleared": manual_count,
        "unverified_evidence_withheld": len(withheld_evidence),
        "referencing_changes_withheld": withheld_changes,
        "nonpublic_or_credential_urls_cleared": removed_url_count,
        "protected_source_pattern_match_counts": pattern_counts,
        "public_registry_contacts": "Retained; scanner matches source private identities rather than arbitrary email addresses.",
        "original_export_hashes": "Retained separately from final sanitized row hashes.",
    }
    manifest["checks"]["protected_source_text"] = "passed"
    manifest["checks"]["evidence_provenance"] = "passed; unverified evidence withheld"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "finalized", "database_path": str(database_path), "database_bytes": database_path.stat().st_size, "artifact_sha256": manifest["artifact_sha256"], "additional_sanitization": manifest["additional_sanitization"], "storage_budget": budget}), flush=True)


def copy_table(source, destination, *, table, columns, query, parameters=(), expected_count, batch_size=25, synthetic_id=False):
    digest = hashlib.sha256()
    copied = 0
    raw_snapshot_hash_failures = 0
    placeholders = ",".join("?" for _ in columns)
    column_sql = ",".join('"' + column + '"' for column in columns)
    insert_sql = f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})'
    with source.cursor(pymysql.cursors.SSDictCursor) as cursor:
        cursor.execute(query, parameters)
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            values_batch = []
            for row in batch:
                result = sanitized_row(table, row)
                if synthetic_id:
                    result["id"] = copied + 1
                if table == "td_snapshots":
                    if hashlib.sha256(result["raw_json"].encode("utf-8")).hexdigest() != result["digest"]:
                        raw_snapshot_hash_failures += 1
                values = tuple(result[column] for column in columns)
                hash_row(digest, values)
                values_batch.append(values)
                copied += 1
            destination.executemany(insert_sql, values_batch)
            destination.commit()
            if copied % 1000 < len(batch):
                print(json.dumps({"status": "copying", "table": table, "rows": copied}), flush=True)
    if copied != expected_count:
        raise ExportError("source_count_mismatch")
    if raw_snapshot_hash_failures:
        raise ExportError("snapshot_digest_mismatch")
    destination_digest = hashlib.sha256()
    order_column = "symbol" if table == "td_portfolio_symbols" else "id"
    actual_count = 0
    for values in destination.execute(f'SELECT {column_sql} FROM "{table}" ORDER BY "{order_column}"'):
        hash_row(destination_digest, values)
        actual_count += 1
    if actual_count != copied or destination_digest.hexdigest() != digest.hexdigest():
        raise ExportError("destination_checksum_mismatch")
    result = {"rows": copied, "columns": columns, "sanitized_rows_sha256": digest.hexdigest()}
    print(json.dumps({"status": "table_verified", "table": table, "rows": copied}), flush=True)
    return result


def scalar(source, query, parameters=()):
    with source.cursor() as cursor:
        cursor.execute(query, parameters)
        return cursor.fetchone()[0]


def verify_privacy(destination):
    for table in EXCLUDED_TABLES:
        exists = destination.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if exists and destination.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]:
            raise ExportError("excluded_data_present")
    assertions = (
        "SELECT COUNT(*) FROM td_companies WHERE last_error IS NOT NULL",
        "SELECT COUNT(*) FROM td_changes WHERE reviewed_by IS NOT NULL OR review_note IS NOT NULL",
        "SELECT COUNT(*) FROM td_evidence WHERE created_by != 0 OR reviewed_by IS NOT NULL OR reason IS NOT NULL",
        "SELECT COUNT(*) FROM td_target_action_dates WHERE entered_by IS NOT NULL OR (entry_method='manual' AND context != '')",
        "SELECT COUNT(*) FROM td_target_action_date_edits WHERE entered_by != 0 OR notes != ''",
    )
    if any(destination.execute(query).fetchone()[0] for query in assertions):
        raise ExportError("private_attribution_present")


def export(output_directory, target_disk_gb=60):
    load_dotenv(ROOT / ".env")
    password = os.getenv("gptdb")
    if not password:
        raise ExportError("source_credential_missing")
    output_directory = Path(output_directory).resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    partial = output_directory / f"trialdelta-portfolio-{stamp}.partial.db"
    final = output_directory / f"trialdelta-portfolio-{stamp}.db"
    if partial.exists() or final.exists():
        raise ExportError("destination_exists")
    source = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"), port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "gptdb"), password=password, database="trialdelta",
        charset="utf8mb4", connect_timeout=15, read_timeout=600, write_timeout=30,
        autocommit=False,
    )
    destination = None
    manifest = {
        "format_version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_read_only": True, "source_isolation": "REPEATABLE READ consistent snapshot",
        "news_start_inclusive": NEWS_START, "news_end_exclusive": NEWS_END_EXCLUSIVE,
        "news_timezone_policy": "Preserve timezone-less source DATETIME calendar values without conversion.",
        "excluded_tables": list(EXCLUDED_TABLES), "neutral_imported_user_id": 0,
        "cleared_private_fields": ["last_error", "review_note", "reviewed_by", "evidence.reason", "date_edit.notes", "manual_date.context"],
        "tables": {},
    }
    try:
        with source.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            cursor.execute("SELECT @@transaction_read_only")
            if cursor.fetchone()[0] != 1:
                raise ExportError("source_not_read_only")
        allocated_bytes = scalar(source, """SELECT SUM(DATA_LENGTH+INDEX_LENGTH)
            FROM information_schema.TABLES WHERE TABLE_SCHEMA='trialdelta'
            OR (TABLE_SCHEMA='filings' AND TABLE_NAME='biotech_filings')""") or 0
        if shutil.disk_usage(output_directory).free < allocated_bytes + 1_000_000_000:
            raise ExportError("local_disk_headroom_insufficient")
        destination, base = create_schema(partial)
        for table in RESEARCH_TABLES:
            columns = [column.name for column in base.metadata.tables[table].columns]
            column_sql = ",".join("`" + column + "`" for column in columns)
            count = scalar(source, f"SELECT COUNT(*) FROM trialdelta.`{table}`")
            manifest["tables"][table] = copy_table(
                source, destination, table=table, columns=columns,
                query=f"SELECT {column_sql} FROM trialdelta.`{table}` ORDER BY id",
                expected_count=count,
            )
        count = scalar(source, "SELECT COUNT(*) FROM filings.biotech_filings")
        columns = ["id", "symbol", "form_type", "filing", "accession_no", "filed_at", "filing_link"]
        manifest["tables"]["td_portfolio_filings"] = copy_table(
            source, destination, table="td_portfolio_filings", columns=columns,
            query="SELECT symbol,form_type,filing,accession_no,filed_at,filing_link FROM filings.biotech_filings",
            expected_count=count, synthetic_id=True, batch_size=5,
        )
        manifest["tables"]["td_portfolio_filings"]["id_policy"] = "Synthetic migration order; source has no primary key; duplicates preserved."
        news_where = f"FROM news.stock_news n INNER JOIN {SYMBOL_UNION} ON n.Ticker=symbols.symbol WHERE n.Date >= %s AND n.Date < %s"
        parameters = (NEWS_START, NEWS_END_EXCLUSIVE)
        count = scalar(source, "SELECT COUNT(*) " + news_where, parameters)
        columns = ["id", "Ticker", "Title", "Source", "Date", "Url", "Category", "article_hash"]
        manifest["tables"]["td_portfolio_news"] = copy_table(
            source, destination, table="td_portfolio_news", columns=columns,
            query="SELECT n.id,n.Ticker,n.Title,n.Source,n.Date,n.Url,n.Category,n.article_hash " + news_where + " ORDER BY n.id",
            parameters=parameters, expected_count=count,
        )
        count = scalar(source, "SELECT COUNT(*) FROM " + SYMBOL_UNION)
        manifest["tables"]["td_portfolio_symbols"] = copy_table(
            source, destination, table="td_portfolio_symbols", columns=["symbol", "sector", "industry"],
            query="SELECT symbols.symbol,s.sector,s.industry FROM " + SYMBOL_UNION + " LEFT JOIN stocks.symbol_sector_industry s ON s.symbol=symbols.symbol ORDER BY BINARY symbols.symbol",
            expected_count=count,
        )
        verify_privacy(destination)
        if destination.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ExportError("foreign_key_check_failed")
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ExportError("sqlite_integrity_failed")
        out_of_window = destination.execute("SELECT COUNT(*) FROM td_portfolio_news WHERE Date < ? OR Date >= ? OR Date IS NULL", (NEWS_START.replace(" ", "T"), NEWS_END_EXCLUSIVE.replace(" ", "T"))).fetchone()[0]
        if out_of_window:
            raise ExportError("news_window_failed")
        destination.execute("PRAGMA optimize")
        destination.commit()
        destination.close()
        destination = None
        budget = storage_budget(partial.stat().st_size, target_disk_gb)
        manifest["storage_budget"] = budget
        if not budget["passed"]:
            raise ExportError("target_disk_headroom_insufficient")
        with partial.open("rb") as file:
            manifest["artifact_sha256"] = hashlib.file_digest(file, "sha256").hexdigest()
        manifest["checks"] = {"source_counts": "passed", "sanitized_row_checksums": "passed", "snapshot_content_hashes": "passed", "foreign_keys": "passed", "sqlite_integrity": "passed", "privacy_exclusions": "passed", "news_window": "passed", "disk_headroom": "passed"}
        partial.rename(final)
        manifest["artifact_filename"] = final.name
        manifest_path = final.with_suffix(".manifest.json")
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "complete", "database_path": str(final), "manifest_path": str(manifest_path), "database_bytes": final.stat().st_size, "artifact_sha256": manifest["artifact_sha256"], "storage_budget": budget}), flush=True)
        return final, manifest_path
    finally:
        if destination is not None:
            destination.close()
        source.rollback()
        source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, default=ROOT / "artifacts" / "portfolio")
    parser.add_argument("--target-disk-gb", type=int, default=60)
    parser.add_argument("--finalize-existing", type=Path, help="Redact and revalidate an existing exported artifact without re-exporting source rows.")
    args = parser.parse_args()
    try:
        if args.finalize_existing:
            finalize_artifact(args.finalize_existing)
        else:
            final, _ = export(args.output_directory, args.target_disk_gb)
            finalize_artifact(final)
    except Exception as error:
        code = str(error) if isinstance(error, ExportError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
