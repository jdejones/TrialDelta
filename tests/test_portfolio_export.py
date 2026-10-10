"""Sanitized migration checks use tiny fixtures, never the workstation MySQL."""

import hashlib
import importlib.util
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location("portfolio_export", Path(__file__).resolve().parents[1] / "scripts" / "export_portfolio.py")
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


@pytest.mark.parametrize("url", [
    "file:///C:/Users/owner/data.csv", "http://localhost/private", "http://127.0.0.1/",
    "http://192.168.1.8/", "http://169.254.169.254/", "http://[::1]/",
    "https://admin:password@example.com/", "https://example.com/?access_token=secret",
    "https://example.com/?%74oken=secret", "http://server.internal/", "https://example.com\\private",
    "https://drive.google.com/file/d/private-document/view",
])
def test_private_urls_are_removed(url):
    assert exporter.public_url(url) == ""


def test_public_provenance_link_is_preserved():
    url = "https://www.sec.gov/Archives/edgar/data/1234/report.htm"
    assert exporter.public_url(url) == url


def test_evidence_provenance_requires_known_public_source_or_issuer_domain():
    assert exporter.verified_evidence_url("https://www.sec.gov/Archives/report.htm")
    assert exporter.verified_evidence_url("https://ir.example-issuer.com/report", "https://www.example-issuer.com")
    assert not exporter.verified_evidence_url("https://example.com/my-private-notes")
    assert not exporter.verified_evidence_url("https://docs.google.com/document/d/private", "https://docs.google.com")


def test_targeted_privacy_scan_does_not_remove_public_registry_contacts():
    patterns = (["owner@example.net"], ["c:\\users\\owner"], "private-db-credential")
    assert not any(exporter.privacy_match_counts(["Public contact: investigator@university.edu"], patterns).values())
    counts = exporter.privacy_match_counts(["owner@example.net", "C:\\Users\\owner\\data", "private-db-credential", "http://127.0.0.1"], patterns)
    assert counts == {"private_email": 1, "workstation_path": 1, "source_credential": 1, "local_database_ip": 1}


def test_identity_and_private_notes_are_removed_without_changing_provenance_hash():
    row = exporter.sanitized_row("td_target_action_date_edits", {
        "id": 23, "target_identity": "original-content-hash", "entered_by": 9,
        "notes": "owner's private comment", "source_url": "http://127.0.0.1/local",
    })
    assert row == {"id": 23, "target_identity": "original-content-hash", "entered_by": 0, "notes": "", "source_url": ""}
    manual = exporter.sanitized_row("td_target_action_dates", {"entry_method": "manual", "context": "private notes", "entered_by": 9})
    assert manual["context"] == "" and manual["entered_by"] is None
    filing = exporter.sanitized_row("td_target_action_dates", {"entry_method": "filing", "context": "public filing excerpt", "entered_by": None})
    assert filing["context"] == "public filing excerpt"


def test_datetime_and_binary_fields_remain_portable():
    row = exporter.sanitized_row("td_portfolio_news", {"Date": datetime(2026, 10, 9, 12, 30), "article_hash": memoryview(b"\0\xff")})
    assert row["Date"] == "2026-10-09T12:30:00"
    assert row["article_hash"] == b"\0\xff"


class SourceCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, parameters):
        assert query.startswith("SELECT ")

    def fetchmany(self, batch_size):
        batch, self.rows = self.rows[:batch_size], self.rows[batch_size:]
        return batch


class Source:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self, *args):
        return SourceCursor(self.rows)


def test_streaming_copy_preserves_ids_and_validates_sanitized_checksum():
    destination = sqlite3.connect(":memory:")
    destination.execute("CREATE TABLE td_portfolio_news(id INTEGER PRIMARY KEY,Date TEXT,Url TEXT,article_hash BLOB)")
    columns = ["id", "Date", "Url", "article_hash"]
    rows = [
        {"id": 2, "Date": datetime(2026, 10, 2), "Url": "https://example.com/public", "article_hash": b"a"},
        {"id": 9, "Date": datetime(2026, 10, 9, 23, 59), "Url": "file:///private", "article_hash": b"b"},
    ]
    result = exporter.copy_table(Source(rows), destination, table="td_portfolio_news", columns=columns, query="SELECT fixture", expected_count=2, batch_size=1)
    assert destination.execute("SELECT id,Url FROM td_portfolio_news ORDER BY id").fetchall() == [(2, "https://example.com/public"), (9, "")]
    assert result["rows"] == 2 and len(result["sanitized_rows_sha256"]) == 64


def test_streaming_copy_fails_closed_on_source_count_mismatch():
    destination = sqlite3.connect(":memory:")
    destination.execute("CREATE TABLE td_portfolio_symbols(symbol TEXT PRIMARY KEY)")
    with pytest.raises(exporter.ExportError, match="source_count_mismatch"):
        exporter.copy_table(Source([{"symbol": "TEST"}]), destination, table="td_portfolio_symbols", columns=["symbol"], query="SELECT fixture", expected_count=2)


def test_filing_source_duplicates_are_preserved_with_separate_synthetic_ids():
    destination = sqlite3.connect(":memory:")
    destination.execute("CREATE TABLE td_portfolio_filings(id INTEGER PRIMARY KEY,symbol TEXT,filing TEXT)")
    rows = [{"symbol": "TEST", "filing": "same public document"}] * 2
    exporter.copy_table(Source(rows), destination, table="td_portfolio_filings", columns=["id", "symbol", "filing"], query="SELECT fixture", expected_count=2, synthetic_id=True)
    assert destination.execute("SELECT id,filing FROM td_portfolio_filings ORDER BY id").fetchall() == [(1, "same public document"), (2, "same public document")]


def test_snapshot_content_hash_mismatch_prevents_publication():
    destination = sqlite3.connect(":memory:")
    destination.execute("CREATE TABLE td_snapshots(id INTEGER PRIMARY KEY,raw_json TEXT,digest TEXT)")
    rows = [{"id": 1, "raw_json": '{"public":"fixture"}', "digest": hashlib.sha256(b"other content").hexdigest()}]
    with pytest.raises(exporter.ExportError, match="snapshot_digest_mismatch"):
        exporter.copy_table(Source(rows), destination, table="td_snapshots", columns=["id", "raw_json", "digest"], query="SELECT fixture", expected_count=1)


def test_disk_gate_reserves_backup_os_and_spare_space():
    budget = exporter.storage_budget(14_000_000_000)
    assert budget["passed"] and budget["required_bytes"] == 51_000_000_000
    assert not exporter.storage_budget(19_000_000_000)["passed"]


def test_portfolio_schema_contains_empty_authentication_tables(tmp_path):
    destination, _ = exporter.create_schema(tmp_path / "portfolio.db")
    try:
        exporter.verify_privacy(destination)
        assert destination.execute("SELECT COUNT(*) FROM td_users").fetchone()[0] == 0
        tables = {row[0] for row in destination.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"td_portfolio_news", "td_portfolio_filings", "td_portfolio_symbols"} <= tables
        assert destination.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        destination.close()


def test_finalizer_withholds_unverified_evidence_and_referencing_change(tmp_path, monkeypatch):
    path = tmp_path / "portfolio.db"
    destination, _ = exporter.create_schema(path)
    destination.execute("INSERT INTO td_companies(id,ticker,name,sponsor,coverage_status) VALUES (1,'PORT','Portfolio','Portfolio','active')")
    destination.execute("INSERT INTO td_evidence(id,company_id,source_type,title,url,published_at,captured_at,quote,category,value,review_status,created_by) VALUES (1,1,'press_release','private title','https://example.com/notes','2026-10-09','2026-10-09','private quote','completion','private value','approved',0)")
    destination.execute("INSERT INTO td_changes(id,company_id,category,title,detected_at,source,after_evidence,after_value,review_status,reviewed) VALUES (1,1,'completion','private title','2026-10-09','company',1,'private value','approved',1)")
    destination.commit()
    tables = {}
    for table in ("td_evidence", "td_changes"):
        columns = [row[1] for row in destination.execute(f'PRAGMA table_info("{table}")')]
        tables[table] = {"columns": columns, "rows": 1, "sanitized_rows_sha256": "initial-hash"}
    destination.close()
    path.with_suffix(".manifest.json").write_text(json.dumps({"tables": tables, "checks": {}, "storage_budget": {"target_disk_bytes": 60_000_000_000}}))

    class PrivacyCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query):
            assert query in ("SET SESSION TRANSACTION READ ONLY", "SELECT email FROM trialdelta.td_users")

        def fetchall(self):
            return [("private-owner@example.net",)]

    class PrivacySource:
        def cursor(self):
            return PrivacyCursor()

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(exporter.pymysql, "connect", lambda **kwargs: PrivacySource())
    monkeypatch.setenv("gptdb", "private-db-fixture-secret")
    exporter.finalize_artifact(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT title,quote,value,url,review_status FROM td_evidence").fetchone() == ("Source withheld from portfolio snapshot", "", "", "", "rejected")
        assert connection.execute("SELECT title,after_value,review_status,reviewed FROM td_changes").fetchone() == ("Source withheld from portfolio snapshot", None, "rejected", 0)
    manifest = json.loads(path.with_suffix(".manifest.json").read_text())
    assert manifest["additional_sanitization"]["unverified_evidence_withheld"] == 1
    assert manifest["additional_sanitization"]["referencing_changes_withheld"] == 1
    assert not any(manifest["additional_sanitization"]["protected_source_pattern_match_counts"].values())
    assert manifest["tables"]["td_evidence"]["export_sanitized_rows_sha256"] == "initial-hash"
