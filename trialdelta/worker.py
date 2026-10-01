"""Run once for a scheduler, or retain a 24-hour polling process."""

import argparse
import json
import os
import time
from .worker_health import UnmonitoredRun, WorkerHealth


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Collect TrialDelta registry snapshots"
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--send-email",
        action="store_true",
        help="Explicitly enable email to opted-in users using configured SMTP",
    )
    parser.add_argument("--company-id", type=int)
    parser.add_argument(
        "--import-news",
        action="store_true",
        help="Read source candidates from the optional existing news database",
    )
    args = parser.parse_args(argv)
    # Include startup/configuration failures in the continuous worker's health.
    monitor = UnmonitoredRun() if args.once else WorkerHealth()
    with monitor as health:
        _run(args, health)


def _run(args, health):
    from .db import initialize
    from .seed import seed
    from .auth import bootstrap
    from .registry import sync_all
    from .digest import deliver_daily
    from .inbox import import_news

    initialize()
    seed()
    bootstrap()
    interval = max(1, float(os.getenv("SYNC_INTERVAL_HOURS", "24"))) * 3600
    while True:
        health.update("collecting")
        print(json.dumps(sync_all(args.company_id, refresh_coverage=True)), flush=True)
        if args.import_news:
            try:
                print(json.dumps(import_news()), flush=True)
            except Exception as exc:
                print(f"News bridge unavailable ({type(exc).__name__}).", flush=True)
        if args.send_email:
            try:
                print(f"Digests delivered: {deliver_daily()}", flush=True)
            except Exception as exc:
                print(
                    f"Email delivery failed ({type(exc).__name__}); registry collection will continue.",
                    flush=True,
                )
        if args.once:
            break
        health.update("waiting", next_run_at=time.time() + interval)
        time.sleep(interval)


if __name__ == "__main__":
    main()
