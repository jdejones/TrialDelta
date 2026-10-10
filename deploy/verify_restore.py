"""Verify a restored SQLite backup in an isolated container with no network."""
import json
import os

from fastapi.testclient import TestClient
from sqlalchemy import text

from trialdelta.app import app
from trialdelta.db import Session


def main():
    with TestClient(app, base_url=os.environ["APP_ORIGIN"]) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        companies = client.get("/api/companies").json()
        assert len(companies) == 982
        assert sum(row["study_count"] for row in companies) == 27524
        studies = client.get("/api/studies?limit=25&offset=100")
        assert studies.status_code == 200 and studies.headers["X-Total-Count"] == "27524"
        assert client.get("/api/home/news/page?page_size=10").status_code == 200
        assert client.get("/api/home/filings?page_size=10").status_code == 200
        assert client.get("/api/home/tads/page?limit=10").status_code == 200
        assert client.get("/api/review").status_code == 401
    with Session() as db:
        counts = {table: db.scalar(text(f'SELECT COUNT(*) FROM "{table}"')) for table in (
            "td_companies", "td_studies", "td_snapshots", "td_portfolio_filings", "td_portfolio_news", "td_users"
        )}
        assert counts["td_snapshots"] == 49600 and counts["td_portfolio_filings"] == 29274
        assert counts["td_users"] == 1
        assert db.scalar(text("SELECT COUNT(*) FROM td_users WHERE is_admin=1")) == 1
        assert db.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert not db.execute(text("PRAGMA foreign_key_check")).first()
    print(json.dumps({"result": "passed", "network": "none", "restored_counts": counts}))


if __name__ == "__main__":
    main()
