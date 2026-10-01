"""Verify real worker control flow with collection and database writes replaced."""

from datetime import datetime
from types import SimpleNamespace
import time

import pytest

from trialdelta import auth, db, digest, inbox, registry, seed, worker, worker_health


@pytest.fixture
def isolated_worker(tmp_path, monkeypatch):
    directory = tmp_path / "health"
    monkeypatch.setattr(worker_health, "health_directory", lambda: directory)
    initialized = []
    monkeypatch.setattr(db, "initialize", lambda: initialized.append("database"))
    monkeypatch.setattr(seed, "seed", lambda: initialized.append("seed"))
    monkeypatch.setattr(auth, "bootstrap", lambda: initialized.append("accounts"))
    return directory, initialized


def test_actual_loop_reports_collecting_then_waiting_and_preserves_partial_result(isolated_worker, monkeypatch, capsys):
    directory, initialized = isolated_worker
    clock = time.time()
    observed = []

    def collect(company_id, refresh_coverage=False):
        assert company_id == 7
        assert refresh_coverage is True
        assert worker_health.read_health(directory)["state"] == "collecting"
        return {"status": "partial", "errors": [{"company": "TEST"}]}

    def sleep(interval):
        observed.append(interval)
        status = worker_health.read_health(directory)
        assert status["state"] == "waiting"
        assert datetime.fromisoformat(status["next_run_at"]).timestamp() == int(clock + 86400)
        raise KeyboardInterrupt

    monkeypatch.setattr(registry, "sync_all", collect)
    monkeypatch.setenv("SYNC_INTERVAL_HOURS", "24")
    monkeypatch.setattr(worker, "time", SimpleNamespace(time=lambda: clock, sleep=sleep))
    with pytest.raises(KeyboardInterrupt):
        worker.main(["--company-id", "7"])
    assert initialized == ["database", "seed", "accounts"]
    assert observed == [86400]
    assert '"status": "partial"' in capsys.readouterr().out
    assert worker_health.read_health(directory)["state"] == "stopped"


def test_once_optional_failures_do_not_create_continuous_health(isolated_worker, monkeypatch, capsys):
    directory, _ = isolated_worker
    collections = []
    def collect(company_id, refresh_coverage=False):
        collections.append((company_id, refresh_coverage))
        return {"status": "complete"}
    monkeypatch.setattr(registry, "sync_all", collect)

    def unavailable():
        raise RuntimeError("Do not display private connection details")

    monkeypatch.setattr(inbox, "import_news", unavailable)
    monkeypatch.setattr(digest, "deliver_daily", unavailable)
    worker.main(["--once", "--import-news", "--send-email"])
    assert collections == [(None, True)]
    assert not directory.exists()
    output = capsys.readouterr().out
    assert "News bridge unavailable" in output
    assert "Email delivery failed" in output
    assert "private connection" not in output


def test_continuous_worker_refreshes_coverage_on_every_iteration(isolated_worker, monkeypatch):
    collections, sleeps = [], []

    def collect(company_id, refresh_coverage=False):
        collections.append((company_id, refresh_coverage))
        return {"status": "complete"}

    def sleep(interval):
        sleeps.append(interval)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(registry, "sync_all", collect)
    monkeypatch.setattr(worker, "time", SimpleNamespace(time=time.time, sleep=sleep))
    with pytest.raises(KeyboardInterrupt):
        worker.main([])
    assert collections == [(None, True), (None, True)]
