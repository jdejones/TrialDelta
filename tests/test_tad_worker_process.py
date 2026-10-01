"""Real process-per-filing smoke tests with all writes in temporary SQLite."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


HARNESS = r'''
import os, sys
from sqlalchemy import create_engine
from trialdelta.db import Base
from trialdelta import tad_worker
destination = create_engine("sqlite:///" + sys.argv[1])
Base.metadata.create_all(destination)
calls = []
def isolated_destination(url, **options):
    assert url.drivername == "mysql+pymysql" and url.database == "trialdelta"
    calls.append(url)
    assert len(calls) == 1, "One-shot recognition must not create a source engine"
    return destination
tad_worker.create_engine = isolated_destination
os.environ["DATABASE_URL"] = "mysql+pymysql://test@127.0.0.1/trialdelta"
try:
    raise SystemExit(tad_worker.main(sys.argv[2:]))
finally:
    destination.dispose()
'''


def run_child(destination, message):
    # The production MySQL guard is exercised. Only engine construction is
    # replaced inside this test child, so no production connection is possible.
    env = dict(os.environ, USE_LOCAL_MYSQL="false", DATABASE_URL="sqlite:///" + str(destination),
               SOURCE_DATABASE_URL="deliberately-invalid-and-unused", PYTHONIOENCODING="utf-8",
               PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-B", "-u", "-c", HARNESS, str(destination), "--once"],
        cwd=str(Path(__file__).resolve().parents[1]), env=env,
        input=message, capture_output=True, text=True, encoding="utf-8", timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def filing(markup, accession):
    return {"command": "filing", "row": {
        "symbol": "ARVN", "form_type": "8-K", "filing": markup,
        "accession_no": accession, "filed_at": "2026-09-27T10:30:00",
        "filing_link": "https://example.org/filing",
    }}


def test_each_real_child_saves_one_filing_and_exits_without_stop_message(tmp_path):
    destination = tmp_path / "destination.db"
    first = filing("The target action date is not known.", "one")
    second = filing("The target action date is December 2027.", "two")
    for expected, message in enumerate((first, second), 1):
        result = run_child(destination, json.dumps(message))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Filing complete" in result.stdout
        with sqlite3.connect(destination) as db:
            assert db.execute("SELECT COUNT(*) FROM td_target_action_dates").fetchone()[0] == expected
            assert db.execute("SELECT COUNT(*) FROM td_tad_filing_jobs WHERE state='complete'").fetchone()[0] == expected
    with sqlite3.connect(destination) as db:
        saved = db.execute("SELECT tad, status FROM td_target_action_dates ORDER BY accession_no").fetchall()
    assert saved == [(None, "unidentifiable"), ("2027-12", "identified")]
    # An explicitly delivered duplicate gets a new short-lived process, but
    # cannot duplicate results or undo backend corrections.
    result = run_child(destination, json.dumps(first))
    assert result.returncode == 0
    assert "already processed" in result.stdout


def test_one_shot_child_rejects_multiple_filings_without_saving_either(tmp_path):
    destination = tmp_path / "destination.db"
    message = json.dumps(filing("The target action date is December 2027.", "one"))
    result = run_child(destination, message + "\n" + message)
    assert result.returncode == 1
    with sqlite3.connect(destination) as db:
        assert db.execute("SELECT COUNT(*) FROM td_target_action_dates").fetchone()[0] == 0
