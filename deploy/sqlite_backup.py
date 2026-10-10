"""Create a transactionally consistent backup without copying a live WAL file."""

import argparse
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.destination.resolve() or args.destination.exists():
        raise ValueError("Choose a new backup path distinct from the database")
    source = sqlite3.connect(args.source.resolve().as_uri() + "?mode=ro", uri=True)
    destination = sqlite3.connect(args.destination)
    try:
        try:
            # BEGIN alone is deferred; this read pins one snapshot for every
            # backup batch. WAL writers can continue without restarting it.
            source.execute("BEGIN")
            source.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            source.backup(destination, pages=256)
        finally:
            source.rollback()
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Backup integrity check failed")
        if destination.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Backup foreign key check failed")
    finally:
        destination.close()
        source.close()
    print("Consistent SQLite backup verified")


if __name__ == "__main__":
    main()
