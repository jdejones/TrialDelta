"""Package only reviewed application source; never credentials or workstation data."""

import argparse
import hashlib
import json
from pathlib import Path
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    allowed = [root / name for name in (".dockerignore", "Dockerfile", "compose.yaml", "requirements.lock", "deploy/Caddyfile", "deploy/sqlite_backup.py", "deploy/verify_restore.py")]
    allowed += sorted((root / "trialdelta").glob("*.py"))
    allowed += sorted(p for p in (root / "trialdelta/static").rglob("*") if p.is_file() and p.suffix in {".js", ".css", ".html", ".svg"})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, "w:gz") as archive:
        for source in allowed:
            if source.is_symlink():
                raise ValueError("Application package cannot contain symlinks")
            archive.add(source, arcname=source.relative_to(root).as_posix(), recursive=False)
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    print(json.dumps({"path": str(args.output), "sha256": digest, "files": len(allowed), "bytes": args.output.stat().st_size}))


if __name__ == "__main__":
    main()
