"""Database-independent, local health reporting for the continuous collector.

Only an explicitly started worker writes here; the administrator app only reads.
Separate launch records keep an old process from overwriting its replacement.
"""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import uuid


HEARTBEAT_SECONDS = 10
STALE_SECONDS = 45
STATES = {"starting", "collecting", "waiting", "stopped", "failed"}


def health_directory():
    # A stable path lets the GUI and worker start from different directories.
    return Path(__file__).resolve().parents[1] / "data" / "worker-health"


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def _write(path, record):
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(record), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path):
    # Windows can briefly deny an open while os.replace swaps the heartbeat.
    # Retry that transient condition before presenting an unavailable alert.
    for attempt in range(3):
        try:
            content = path.read_text(encoding="utf-8")
            break
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.01)
    record = json.loads(content)
    if not isinstance(record, dict) or record.get("state") not in STATES:
        raise ValueError("Invalid worker health record")
    for key in ("started_at", "heartbeat_at", "next_run_at"):
        value = record.get(key)
        if key == "next_run_at" and value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Invalid worker health time")
    if not isinstance(record.get("pid"), int) or isinstance(record["pid"], bool) or record["pid"] <= 0:
        raise ValueError("Invalid worker process")
    return record


def _paths(directory):
    try:
        return sorted(path for path in directory.iterdir() if path.suffix == ".json")
    except FileNotFoundError:
        return []


def read_health(directory=None, now=None):
    """Read the latest continuous launch without creating files or touching DB."""
    directory = Path(directory) if directory is not None else health_directory()
    empty = {"heartbeat_at": None, "next_run_at": None}
    try:
        paths = _paths(directory)
        if not paths:
            return {**empty, "state": "not_started", "detail": "Daily worker has not reported from this installation. Start it explicitly to enable monitoring."}
        # Names begin with launch time, not heartbeat time. A stopped older
        # process must not hide the health of the most recently launched one.
        record = _read(paths[-1])
        now = time.time() if now is None else now
        state = record["state"]
        if state not in {"stopped", "failed"} and abs(now - record["heartbeat_at"]) > STALE_SECONDS:
            state = "unresponsive"
        messages = {
            "starting": "Daily worker is starting.",
            "collecting": "Daily worker is collecting registry data.",
            "waiting": "Daily worker is running and waiting for its next collection.",
            "stopped": "Daily worker was stopped intentionally. Automatic collection is off.",
            "failed": "DAILY WORKER FAILED — Automatic collection has stopped. Check the worker error log or launch console, then restart the worker.",
            "unresponsive": "DAILY WORKER NOT RESPONDING — Its heartbeat is missing. Automatic collection may have stopped. Check the worker process and error log.",
        }
        return {
            "state": state, "detail": messages[state],
            "heartbeat_at": _iso(record["heartbeat_at"]),
            "next_run_at": _iso(record["next_run_at"]) if state == "waiting" and record.get("next_run_at") is not None else None,
        }
    except (OSError, ValueError, TypeError, OverflowError):
        return {**empty, "state": "unknown", "detail": "WORKER STATUS UNAVAILABLE — The local health record cannot be read. Check data/worker-health and the worker error log."}


class WorkerHealth:
    def __init__(self, directory=None, heartbeat_seconds=HEARTBEAT_SECONDS):
        self.directory = Path(directory) if directory is not None else health_directory()
        self.path = self.directory / f"{time.time_ns():020d}-{uuid.uuid4().hex}.json"
        self.interval = heartbeat_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._warned = False
        self.record = {"pid": os.getpid(), "started_at": time.time(), "heartbeat_at": time.time(), "state": "starting", "next_run_at": None}

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        # Fail before collecting if monitoring cannot be established at all.
        _write(self.path, self.record)
        self._thread = threading.Thread(target=self._heartbeat, name="worker-heartbeat", daemon=True)
        self._thread.start()
        return self

    def update(self, state, next_run_at=None):
        if state not in STATES:
            raise ValueError("Invalid worker health state")
        with self._lock:
            self.record.update(state=state, next_run_at=next_run_at, heartbeat_at=time.time())
            self._persist()

    def _persist(self):
        try:
            _write(self.path, self.record)
            self._warned = False
        except OSError:
            # Keep collecting on transient disk trouble; a stale pulse alerts
            # the GUI and a later successful pulse automatically recovers.
            if not self._warned:
                print("Worker health could not be saved; the administrator may show a missing heartbeat.", file=sys.stderr, flush=True)
            self._warned = True

    def _heartbeat(self):
        while not self._stop.wait(self.interval):
            with self._lock:
                self.record["heartbeat_at"] = time.time()
                self._persist()

    def __exit__(self, exc_type, exc_value, traceback):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        intentional = exc_type is None or issubclass(exc_type, KeyboardInterrupt)
        if exc_type is not None and issubclass(exc_type, SystemExit):
            intentional = exc_value.code in (None, 0)
        self.update("stopped" if intentional else "failed")
        return False


class UnmonitoredRun:
    """A scheduler's --once run must not reset the continuous worker's status."""

    def __enter__(self):
        return self

    def update(self, state, next_run_at=None):
        pass

    def __exit__(self, *exc):
        return False


def mark_stopped(pids, directory=None):
    """Called by stop.ps1 only AFTER it stops verified worker processes."""
    directory = Path(directory) if directory is not None else health_directory()
    for path in _paths(directory):
        try:
            record = _read(path)
        except (OSError, ValueError, TypeError, OverflowError):
            # A damaged old record must not prevent the current launch from
            # being marked stopped. read_health still flags a damaged latest.
            continue
        if record["pid"] in pids and record["state"] not in {"failed", "stopped"}:
            record.update(state="stopped", next_run_at=None)
            _write(path, record)


def main():
    parser = argparse.ArgumentParser(description="Record an intentional stop of verified local worker processes")
    parser.add_argument("--mark-stopped", type=int, nargs="+", required=True)
    args = parser.parse_args()
    mark_stopped(set(args.mark_stopped))


if __name__ == "__main__":
    main()
