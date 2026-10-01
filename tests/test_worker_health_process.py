"""Kill and restart isolated worker processes without importing collection/database code."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from trialdelta.worker_health import read_health


HARNESS = r'''
from pathlib import Path
import sys
import time
from trialdelta import worker_health
worker_health.health_directory = lambda: Path(sys.argv[1])
from trialdelta import worker
assert "trialdelta.db" not in sys.modules, "Worker imported database code before monitoring startup"
original_health = worker.WorkerHealth
worker.WorkerHealth = lambda: original_health(heartbeat_seconds=0.1)
mode = sys.argv[2]

def fake_run(args, health):
    if mode == "startup-error":
        raise RuntimeError("synthetic configuration or import failure")
    health.update("collecting")
    if mode == "interrupt":
        raise KeyboardInterrupt
    health.update("waiting", next_run_at=time.time() + 86400)
    if mode in ("once", "clean"):
        return
    while True:
        time.sleep(30)

worker._run = fake_run
worker.main(["--once"] if mode == "once" else [])
'''


def command(directory, mode):
    # The Windows virtualenv executable may be a launcher with a different PID.
    # This harness imports only stdlib-backed modules, so use Python directly.
    return [sys._base_executable, "-B", "-u", "-c", HARNESS, str(directory), mode]


def options():
    return {
        "cwd": str(Path(__file__).resolve().parents[1]),
        "env": dict(os.environ, DATABASE_URL="invalid://must-not-connect",
                    USE_LOCAL_MYSQL="false", PYTHONIOENCODING="utf-8",
                    PYTHONDONTWRITEBYTECODE="1"),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def run_child(directory, mode):
    return subprocess.run(command(directory, mode), **options(), timeout=15)


def wait_for_waiting(process, directory):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            pytest.fail(f"Isolated worker exited early: {stdout}\n{stderr}")
        result = read_health(directory)
        if result["state"] == "waiting":
            try:
                latest = sorted(directory.glob("*.json"))[-1]
                row = json.loads(latest.read_text(encoding="utf-8"))
                if row["pid"] == process.pid and row["state"] == "waiting":
                    return
            except PermissionError:
                pass  # A concurrent atomic replacement can briefly deny reads.
        time.sleep(0.1)
    pytest.fail("Isolated worker did not reach its 24-hour idle state")


@contextmanager
def idle_child(directory):
    process = subprocess.Popen(command(directory, "idle"), **options())
    try:
        wait_for_waiting(process, directory)
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_real_hard_kill_leaves_stale_evidence_and_a_new_launch_recovers(tmp_path):
    with idle_child(tmp_path) as first:
        first_pid = first.pid
        first.kill()
        first.wait(timeout=10)
        previous_paths = set(tmp_path.glob("*.json"))
        killed_record = next(json.loads(path.read_text(encoding="utf-8"))
                             for path in previous_paths)
        assert killed_record["pid"] == first_pid
        assert killed_record["state"] == "waiting"
        assert read_health(tmp_path, now=killed_record["heartbeat_at"] + 46)["state"] == "unresponsive"

    with idle_child(tmp_path):
        assert read_health(tmp_path)["state"] == "waiting"
        assert len(set(tmp_path.glob("*.json")) - previous_paths) == 1


def test_real_uncaught_startup_error_is_persisted_before_database_imports(tmp_path):
    result = run_child(tmp_path, "startup-error")
    assert result.returncode != 0
    assert "synthetic configuration or import failure" in result.stderr
    assert read_health(tmp_path)["state"] == "failed"


@pytest.mark.parametrize("mode", ["clean", "interrupt"])
def test_real_clean_exit_and_ctrl_c_are_recorded_as_intentional_stops(tmp_path, mode):
    result = run_child(tmp_path, mode)
    assert read_health(tmp_path)["state"] == "stopped", result.stdout + result.stderr
    if mode == "clean":
        assert result.returncode == 0, result.stdout + result.stderr


def test_one_time_run_does_not_create_or_replace_continuous_monitoring(tmp_path):
    empty_directory = tmp_path / "one-time-only"
    result = run_child(empty_directory, "once")
    assert result.returncode == 0, result.stdout + result.stderr
    assert read_health(empty_directory)["state"] == "not_started"
    assert not empty_directory.exists()

    continuous_directory = tmp_path / "continuous"
    with idle_child(continuous_directory) as continuous:
        before = set(continuous_directory.glob("*.json"))
        result = run_child(continuous_directory, "once")
        assert result.returncode == 0, result.stdout + result.stderr
        assert set(continuous_directory.glob("*.json")) == before
        assert read_health(continuous_directory)["state"] == "waiting"
        assert all(json.loads(path.read_text(encoding="utf-8"))["pid"] == continuous.pid
                   for path in before)
