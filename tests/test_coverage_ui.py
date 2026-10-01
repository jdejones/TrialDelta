"""Coverage tags preserve public research reads and desktop history visibility."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from trialdelta import auth
from trialdelta.db import Company
from trialdelta.registry import ingest


@pytest.mark.parametrize("status,label", [
    ("active", "Active"), ("delisted", "Delisted"),
    ("out_of_sector", "Outside Healthcare"),
])
def test_coverage_tags_preserve_directory_trials_history_and_follow(client, db, raw, monkeypatch, status, label):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    ingest(db, company, raw)
    changed = deepcopy(raw)
    changed["protocolSection"]["designModule"]["enrollmentInfo"]["count"] = 140
    ingest(db, company, changed)
    company.coverage_status = status
    company.last_error = "private collection diagnostics"
    db.commit()
    study_id = raw["protocolSection"]["identificationModule"]["nctId"]
    followed = client.post("/api/follows", json={"kind": "company", "value": str(company.id)})
    assert followed.status_code == 200
    assert len(client.get("/api/changes?followed=true").json()) == 1

    monkeypatch.setattr(auth, "AUTH_REQUIRED", True)
    monkeypatch.setattr(auth, "PUBLIC_BROWSING_ENABLED", True)
    directory = client.get("/api/companies").json()
    row = next(item for item in directory if item["id"] == company.id)
    assert (row["coverage_status"], row["coverage_label"]) == (status, label)
    assert row["study_count"] == 1
    assert "last_error" not in row
    trials = client.get("/api/studies", params={"company_id": company.id, "q": "TEST-1"}).json()
    assert [row["id"] for row in trials] == [study_id]
    assert (trials[0]["coverage_status"], trials[0]["coverage_label"]) == (status, label)
    detail = client.get(f"/api/studies/{study_id}").json()
    assert detail["company"]["coverage_status"] == status
    assert detail["company"]["coverage_label"] == label
    assert "last_error" not in detail["company"]
    assert len(detail["snapshots"]) == 2 and len(detail["changes"]) == 1
    original_snapshot = detail["snapshots"][-1]["id"]
    assert client.get(f"/api/snapshots/{original_snapshot}").json() == raw


class Value:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value

    def delete(self, *args):
        pass


class Table:
    def __init__(self):
        self.rows = {}

    def selection(self):
        return ()

    def selection_set(self, values):
        pass

    def get_children(self):
        return tuple(self.rows)

    def delete(self, *args):
        self.rows.clear()

    def insert(self, parent, index, **row):
        self.rows[row["iid"]] = row


def test_desktop_keeps_inactive_companies_visible_without_a_live_app():
    from trialdelta.admin_app import AdminApp

    # No Tk window, database connection, or background UI thread is created.
    desktop = SimpleNamespace(
        metrics={}, review_count=Value(), last_job=Value(), review_note=Value(),
        review_text=None, inbox_text=None, _preserve_bulk_status_on_refresh=False,
        _refreshing_bulk_selection=False, _bulk_result_id=None,
        _replace_text=lambda *args: None, _show_history_run=lambda: None,
        _show_bulk_run=lambda: None,
        **{key: Table() for key in ("companies", "reviews", "inbox", "jobs", "bulk_jobs")},
    )
    rows = [
        {"id": i, "ticker": f"TEST{i}", "name": f"Company {i}", "studies": 2,
         "freshness": "stale", "last_checked": None, "error": None,
         "coverage_status": status, "coverage_label": label}
        for i, (status, label) in enumerate([
            ("active", "Active"), ("delisted", "Delisted"), ("out_of_sector", "Outside Healthcare"),
        ], 1)
    ]
    snapshot = {"counts": {"pending_changes": 0, "pending_evidence": 0},
                "companies": rows, "jobs": [], "bulk_jobs": [], "database": "sqlite"}
    AdminApp._show_data(desktop, (snapshot, [], []))
    assert len(desktop.companies.rows) == 3
    assert [item["values"][2] for item in desktop.companies.rows.values()] == [
        "Active", "Delisted", "Outside Healthcare",
    ]
    assert desktop.companies.rows["1"]["values"][-1] == "Ready"
    for key in ("2", "3"):
        assert desktop.companies.rows[key]["tags"] == ("inactive",)
        assert desktop.companies.rows[key]["values"][-1] == "History retained · Collection inactive"


def test_saved_coverage_warnings_render_without_raw_diagnostics():
    from trialdelta.admin_app import AdminApp, _bulk_summary
    from trialdelta.registry import COVERAGE_REFRESH_WARNING

    warnings = [COVERAGE_REFRESH_WARNING, "mysql://user:private-password@example.test"]
    bulk = _bulk_summary({"status": "complete", "id": 3, "warning_messages": warnings})
    desktop = SimpleNamespace(
        jobs=SimpleNamespace(selection=lambda: ("3",)), history_status=Value(),
        job_rows={"3": {"id": 3, "status": "complete", "kind": "company", "warning_messages": warnings}},
    )
    AdminApp._show_history_run(desktop)
    for text in (bulk, desktop.history_status.value):
        assert COVERAGE_REFRESH_WARNING in text
        assert "private-password" not in text
