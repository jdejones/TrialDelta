"""Worker monitoring remains useful while idle, after a crash, and after reopening."""

from datetime import datetime, timezone
import json
import time

import pytest

from trialdelta.worker_health import WorkerHealth, mark_stopped, read_health


def record(directory, *, state="waiting", heartbeat=1_800_000_000.0,
           started=None, pid=1234, next_run=None, name="worker.json"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps({
        "pid": pid,
        "state": state,
        "started_at": started if started is not None else heartbeat - 60,
        "heartbeat_at": heartbeat,
        "next_run_at": next_run,
    }), encoding="utf-8")
    return path


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    return parsed.astimezone(timezone.utc).timestamp()


def read_record(path):
    # Windows may briefly deny a read while os.replace publishes a heartbeat.
    deadline = time.monotonic() + 2
    while True:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def test_never_started_worker_is_not_a_crash_and_read_does_not_create_storage(tmp_path):
    directory = tmp_path / "not-created"
    assert read_health(directory)["state"] == "not_started"
    assert not directory.exists()


@pytest.mark.parametrize("state", ["starting", "collecting", "waiting"])
def test_recent_heartbeat_is_healthy_in_every_running_phase(tmp_path, state):
    now = 1_800_000_000.0
    record(tmp_path, state=state, heartbeat=now - 20, next_run=now + 86_400)
    result = read_health(tmp_path, now=now)
    assert result["state"] == state
    assert timestamp(result["heartbeat_at"]) == now - 20
    if state == "waiting":
        assert timestamp(result["next_run_at"]) == now + 86_400
    else:
        assert result["next_run_at"] is None


def test_idle_worker_is_unresponsive_after_heartbeat_deadline_even_when_next_run_is_tomorrow(tmp_path):
    heartbeat = 1_800_000_000.0
    record(tmp_path, heartbeat=heartbeat, next_run=heartbeat + 86_400)
    assert read_health(tmp_path, now=heartbeat + 44)["state"] == "waiting"
    stale = read_health(tmp_path, now=heartbeat + 46)
    assert stale["state"] == "unresponsive"
    assert timestamp(stale["heartbeat_at"]) == heartbeat
    assert stale["detail"]


@pytest.mark.parametrize("state", ["stopped", "failed"])
def test_terminal_state_survives_reopening_long_after_last_heartbeat(tmp_path, state):
    heartbeat = 1_800_000_000.0
    record(tmp_path, state=state, heartbeat=heartbeat)
    first_read = read_health(tmp_path, now=heartbeat + 86_400)
    reopened = read_health(tmp_path, now=heartbeat + 172_800)
    assert first_read["state"] == reopened["state"] == state


def test_an_older_worker_heartbeat_cannot_hide_a_newer_launch_failure(tmp_path):
    now = 1_800_000_000.0
    record(tmp_path, state="failed", heartbeat=now - 20, started=now - 30,
           name="00002-newer.json")
    record(tmp_path, state="waiting", heartbeat=now, started=now - 60,
           name="00001-older.json")
    assert read_health(tmp_path, now=now)["state"] == "failed"


def test_corrupt_latest_launch_is_not_hidden_by_an_older_healthy_record(tmp_path):
    now = 1_800_000_000.0
    record(tmp_path, heartbeat=now, name="00001-older.json")
    (tmp_path / "00002-latest.json").write_text("{incomplete", encoding="utf-8")
    assert read_health(tmp_path, now=now)["state"] == "unknown"


@pytest.mark.parametrize("contents", [
    "{unfinished",
    "[]",
    "null",
    '{"state": "waiting"}',
    '{"state":"waiting","pid":1234,"started_at":1800000000,"heartbeat_at":"broken"}',
    '{"state":"imaginary","pid":1234,"started_at":1800000000,"heartbeat_at":1800000000}',
])
def test_unreadable_or_invalid_health_is_unknown_instead_of_healthy(tmp_path, contents):
    (tmp_path / "worker.json").write_text(contents, encoding="utf-8")
    result = read_health(tmp_path, now=1_800_000_010.0)
    assert result["state"] == "unknown"
    assert result["detail"]


def test_background_heartbeat_continues_during_long_collection_or_sleep(tmp_path):
    with WorkerHealth(tmp_path, heartbeat_seconds=0.1) as health:
        next_run = time.time() + 86_400
        health.update("waiting", next_run_at=next_run)
        initial = read_record(health.path)["heartbeat_at"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = read_record(health.path)
            if current["heartbeat_at"] > initial:
                break
            time.sleep(0.01)
        else:
            pytest.fail("The heartbeat stopped while the main worker was idle")
        assert current["state"] == "waiting"
        assert current["next_run_at"] == next_run
    assert read_health(tmp_path)["state"] == "stopped"


def test_exception_persists_failed_state_but_keyboard_interrupt_is_intentional(tmp_path):
    failure_directory = tmp_path / "failed"
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with WorkerHealth(failure_directory) as health:
            health.update("collecting")
            raise RuntimeError("synthetic failure")
    assert read_health(failure_directory)["state"] == "failed"

    stopped_directory = tmp_path / "stopped"
    with pytest.raises(KeyboardInterrupt):
        with WorkerHealth(stopped_directory):
            raise KeyboardInterrupt
    assert read_health(stopped_directory)["state"] == "stopped"


def test_a_new_launch_recovers_from_previous_failure_without_reusing_its_record(tmp_path):
    with pytest.raises(RuntimeError):
        with WorkerHealth(tmp_path) as failed:
            raise RuntimeError("synthetic failure")
    failed_contents = failed.path.read_text(encoding="utf-8")
    with WorkerHealth(tmp_path) as current:
        current.update("waiting", next_run_at=time.time() + 86_400)
        assert current.path != failed.path
        assert read_health(tmp_path)["state"] == "waiting"
        assert failed.path.read_text(encoding="utf-8") == failed_contents


def test_mark_stopped_only_marks_requested_pid_and_preserves_failed_evidence(tmp_path):
    first = record(tmp_path, pid=111, name="first.json")
    other = record(tmp_path, pid=222, name="other.json")
    failed = record(tmp_path, pid=333, state="failed", name="failed.json")
    other_before = other.read_text(encoding="utf-8")
    failed_before = failed.read_text(encoding="utf-8")
    mark_stopped([111, 333], directory=tmp_path)
    assert json.loads(first.read_text(encoding="utf-8"))["state"] == "stopped"
    assert other.read_text(encoding="utf-8") == other_before
    assert failed.read_text(encoding="utf-8") == failed_before


def test_corrupt_history_does_not_prevent_marking_a_later_worker_stopped(tmp_path):
    (tmp_path / "00000-old.json").write_text("{incomplete", encoding="utf-8")
    current = record(tmp_path, pid=444, name="99999-current.json")
    mark_stopped([444], directory=tmp_path)
    assert json.loads(current.read_text(encoding="utf-8"))["state"] == "stopped"
    assert read_health(tmp_path)["state"] == "stopped"
