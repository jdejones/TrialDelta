import copy
import json
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from trialdelta import admin_ops as ops
from trialdelta.db import BulkCheckpoint, Company, Change, Evidence, Job, SourceCandidate, TargetActionDate, User
from trialdelta.registry import ingest
from trialdelta.seed import seed


@pytest.fixture
def administrator(db):
    seed()
    return db.scalar(select(User).where(User.email == "local@trialdelta.local"))


def test_desktop_startup_never_collects(client, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Desktop startup must not retrieve source data")

    monkeypatch.setattr(ops.registry, "sync_all", unexpected)
    monkeypatch.setattr(ops, "import_news", unexpected)
    result = ops.initialize_desktop()
    identity = ops.authenticate()
    metrics = ops.dashboard_snapshot(identity["id"])
    assert result == {"mode": "local", "database": "sqlite"}
    assert metrics["counts"]["companies"] == 25
    assert metrics["counts"]["snapshots"] == 0
    assert metrics["jobs"] == []


def test_metrics_include_pending_items_and_hide_error_details(db, administrator, raw):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    ingest(db, company, raw)
    changed = copy.deepcopy(raw)
    changed["protocolSection"]["statusModule"]["overallStatus"] = "COMPLETED"
    ingest(db, company, changed)
    db.add(Evidence(
        company_id=company.id, source_type="presentation", title="Company source",
        url="https://example.org/source", published_at="2026-09-01",
        quote="The company expects results next year.", category="timeline",
        value="2027", created_by=administrator.id,
    ))
    company.last_error = "mysql://root:never-display-this@localhost"
    db.add(Job(status="partial", summary=json.dumps({"companies": 1, "studies": 1, "changes": 1, "errors": [{"company": "ARVN", "error": "never-display-this"}]})))
    db.commit()
    result = ops.dashboard_snapshot(administrator.id)
    assert result["counts"]["snapshots"] == 2
    assert result["counts"]["pending"] == 1
    assert result["counts"]["pending_changes"] == 0
    assert result["counts"]["pending_evidence"] == 1
    assert result["counts"]["approved"] == 0
    assert result["jobs"][0]["errors"] == 1
    assert result["jobs"][0]["error_messages"] == []
    assert "never-display-this" not in json.dumps(result)


def test_saved_bulk_errors_keep_safe_diagnostics_without_exposing_stored_details(db, administrator):
    job = Job(status="partial", summary=json.dumps({
        "kind": "bulk", "pages": 0, "studies": 0,
        "errors": [
            "Bulk download stopped (DataError). Saved pages are retained; resume to retry the unfinished page.",
            ops.bulk_download.DATABASE_STORAGE_ERROR,
            "INSERT INTO td_snapshots VALUES ('mysql://root:never-display-this@localhost')",
        ],
    }))
    db.add(job)
    db.flush()
    db.add(BulkCheckpoint(job_id=job.id, payload="{}"))
    db.commit()

    # dashboard_snapshot owns a fresh session, as it does after desktop restart.
    result = ops.dashboard_snapshot(administrator.id)
    saved = result["bulk_jobs"][0]
    assert saved["errors"] == 3
    assert saved["error_messages"] == [
        ops.bulk_download.DATABASE_DATA_ERROR,
        ops.bulk_download.DATABASE_STORAGE_ERROR,
        ops.bulk_download.UNKNOWN_BULK_ERROR,
    ]
    assert saved == result["jobs"][0]
    assert "never-display-this" not in json.dumps(result)
    assert "INSERT INTO" not in json.dumps(result)


def test_selected_collection_deduplicates_validates_and_stops_on_busy(db, administrator, monkeypatch):
    companies = db.scalars(select(Company).order_by(Company.id).limit(3)).all()
    calls, logs = [], []

    def collect(company_id):
        calls.append(company_id)
        return {"status": "busy" if len(calls) == 2 else "complete", "studies": 2, "changes": 1, "errors": []}

    monkeypatch.setattr(ops.registry, "sync_all", collect)
    ids = [companies[0].id, companies[0].id, companies[1].id, companies[2].id]
    result = ops.run_registry_sync(administrator.id, ids, logs.append)
    assert calls == [companies[0].id, companies[1].id]
    assert result["status"] == "partial"
    assert any("Remaining selections were not started" in line for line in logs)
    with pytest.raises(ops.AdminOperationError):
        ops.run_registry_sync(administrator.id, [999999])
    with pytest.raises(ops.AdminOperationError):
        ops.run_registry_sync(administrator.id, [])
    assert len(calls) == 2


def test_news_import_is_independent_of_registry(db, administrator, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("News import must not trigger ClinicalTrials.gov collection")

    monkeypatch.setattr(ops.registry, "sync_all", unexpected)
    monkeypatch.setattr(ops, "import_news", lambda: {"status": "complete", "imported": 3})
    assert ops.run_news_import(administrator.id)["imported"] == 3


def test_unresolved_view_reaches_records_beyond_the_all_saved_limit_and_can_correct_them(db, administrator):
    for index in range(501):
        db.add(TargetActionDate(
            identity=f"{index:064x}", symbol=f"KNOWN{index}", form_type="8-K",
            filing_sha256=f"{index:064x}", tad="2027-11-14", precision="day", status="identified",
            matched_text="November 14, 2027", reason="Explicit date", start=0, end=10,
            context_start=0, context_end=10, context="Known date",
        ))
    unresolved = TargetActionDate(
        identity="f" * 64, symbol="UNKNOWN", form_type="8-K", filing_sha256="f" * 64,
        tad=None, precision="unknown", status="unidentifiable", matched_text="", reason="Missing year",
        start=4, end=22, context_start=0, context_end=39,
        context="The target action date is November 14.",
    )
    db.add(unresolved)
    db.commit()
    all_saved = ops.tad_items(administrator.id)
    assert all_saved["total"] == 502
    assert len(all_saved["items"]) == 500
    assert all(row["status"] == "identified" for row in all_saved["items"])
    filtered = ops.tad_items(administrator.id, status="unresolved")
    assert filtered["total"] == filtered["unresolved"] == 1
    row = filtered["items"][0]
    assert row["id"] == unresolved.id
    assert row["selection_identity"] == unresolved.identity
    assert row["context"] == unresolved.context
    assert ops.edit_tad(
        administrator.id, row["id"], expected_identity=row["selection_identity"], ticker="UNKNOWN",
        tad="2028-11-14", notes="Confirmed the year from the source filing",
    )["status"] == "updated"
    assert ops.tad_items(administrator.id, status="unresolved")["total"] == 0
    corrected = ops.tad_items(administrator.id)["items"][0]
    assert corrected["ticker"] == "UNKNOWN" and corrected["tad"] == "2028-11-14"
    assert corrected["is_edited"] and corrected["original_context"] == unresolved.context
    db.refresh(unresolved)
    assert unresolved.tad is None  # Filtering and correction preserve original evidence.


def test_unresolved_view_requires_admin_before_reading(db, monkeypatch):
    reader = User(email="unresolved-reader@example.org", password_hash="unused", is_admin=False)
    db.add(reader)
    db.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("An unauthorized user cannot read target date records")

    monkeypatch.setattr(ops, "list_target_action_dates", forbidden)
    with pytest.raises(ops.AdminOperationError, match="Administrator"):
        ops.tad_items(reader.id, status="unresolved")


@pytest.mark.parametrize("status", ["automatic", "pending"])
def test_registry_changes_bypass_review_and_preserve_snapshot_evidence(db, administrator, raw, status):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    ingest(db, company, raw)
    changed = copy.deepcopy(raw)
    changed["protocolSection"]["designModule"]["enrollmentInfo"]["count"] = 160
    ingest(db, company, changed)
    change = db.scalar(select(Change))
    change.review_status = status
    db.commit()
    assert ops.review_items(administrator.id) == []
    assert ops.dashboard_snapshot(administrator.id)["counts"]["pending_changes"] == 0
    detail = ops.review_detail(administrator.id, "change", change.id)
    assert len(detail["snapshots"]) == 2
    assert detail["snapshots"][0]["normalized"]["enrollment"]["count"] == 100
    assert detail["snapshots"][1]["normalized"]["enrollment"]["count"] == 160
    assert change.review_status == status
    assert change.reviewed is False
    assert change.reviewed_by is None


def test_inbox_dismissal_keeps_audit_record(db, administrator):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    row = SourceCandidate(company_id=company.id, title="Source discovery", url="https://example.com/source", identity="test-source")
    db.add(row)
    db.commit()
    assert len(ops.inbox_items(administrator.id)) == 1
    ops.dismiss_inbox(administrator.id, row.id)
    assert ops.inbox_items(administrator.id) == []
    db.refresh(row)
    assert row.status == "dismissed"


def test_reader_cannot_authenticate_or_run_admin_operations(db, administrator, monkeypatch):
    reader = User(email="reader@example.com", password_hash="unused", is_admin=False)
    hosted_admin = User(email="owner@example.com", password_hash="matching-hash", is_admin=True)
    db.add_all([reader, hosted_admin])
    db.commit()
    monkeypatch.setattr(ops.auth, "MODE", "hosted")
    monkeypatch.setattr(ops.auth, "verify_password", lambda password, stored: password == "correct" and stored == "matching-hash")
    with pytest.raises(ops.AdminOperationError):
        ops.authenticate(reader.email, "correct")
    with pytest.raises(ops.AdminOperationError):
        ops.authenticate(hosted_admin.email, "incorrect")
    assert ops.authenticate(hosted_admin.email, "correct")["id"] == hosted_admin.id
    with pytest.raises(ops.AdminOperationError):
        ops.authenticate("local@trialdelta.local", "correct")
    for operation in (ops.dashboard_snapshot, ops.review_items, ops.inbox_items, ops.run_news_import):
        with pytest.raises(ops.AdminOperationError):
            operation(reader.id)
    with pytest.raises(ops.AdminOperationError):
        ops.run_registry_sync(reader.id)


def test_error_and_source_url_sanitization():
    error = ops.safe_error(RuntimeError("mysql://user:secret@host/db?password=secret"))
    assert "secret" not in error
    assert "mysql://" not in error
    assert "RuntimeError" in error
    assert ops.public_source_url("https://example.com/document") == "https://example.com/document"
    for url in ("file:///C:/private.txt", "https://user:secret@example.com", "javascript:alert(1)"):
        with pytest.raises(ops.AdminOperationError):
            ops.public_source_url(url)


def test_tk_shell_renders_and_long_tasks_leave_event_loop_responsive(monkeypatch):
    tkinter = pytest.importorskip("tkinter")
    from trialdelta.admin_app import AdminApp

    try:
        root = tkinter.Tk()
    except tkinter.TclError:
        pytest.skip("Tk display is unavailable on this test host")
    root.withdraw()
    task_started, release = threading.Event(), threading.Event()
    try:
        app = AdminApp(root, SimpleNamespace(safe_error=ops.safe_error), initialize=False)
        root.update_idletasks()
        assert len(app.metrics) == 6
        assert not app.busy
        app._show_data(({
            "counts": {"companies": 1, "studies": 2, "snapshots": 3, "approved": 1, "pending": 0, "pending_changes": 0, "pending_evidence": 0, "inbox": 0},
            "companies": [{"id": 1, "ticker": "TEST", "name": "Test company", "studies": 2, "freshness": "Current", "last_checked": "2026-09-24T12:00:00+00:00", "error": ""}],
            "jobs": [{"id": 1, "status": "complete", "started_at": "2026-09-24T12:00:00+00:00", "finished_at": "2026-09-24T12:02:00+00:00", "companies": 1, "studies": 2, "changes": 1, "errors": 0}],
            "database": "sqlite",
        }, [], []))
        root.update_idletasks()
        assert app.metrics["studies"].get() == "2"
        assert app.companies.item("1", "values")[0] == "TEST"
        assert app.jobs.item("1", "values")[1] == "complete"
        main_thread = threading.get_ident()
        completed = []

        def task():
            assert threading.get_ident() != main_thread
            task_started.set()
            release.wait(3)
            return "done"

        def callback(result):
            assert threading.get_ident() == main_thread
            completed.append(result)

        app._submit(task, callback, "Test long task")
        assert task_started.wait(1)
        assert app.busy
        assert all(str(button["state"]) == "disabled" for button in app.buttons)
        responsive = []
        root.after(0, lambda: responsive.append(True))
        root.update()
        assert responsive == [True]
        release.set()
        deadline = time.monotonic() + 3
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert completed == ["done"]
        assert not app.busy
        dialogs = []
        monkeypatch.setattr("trialdelta.admin_app.messagebox.showerror", lambda *args, **kwargs: dialogs.append(args))

        def failure():
            raise RuntimeError("mysql://root:secret-value@host")

        app._submit(failure, None, "Test failure")
        deadline = time.monotonic() + 3
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert dialogs
        assert "secret-value" not in str(dialogs)
        assert "secret-value" not in app.log.get("1.0", "end")
        assert app.status.get() == "Operation needs attention"
    finally:
        release.set()
        root.destroy()
