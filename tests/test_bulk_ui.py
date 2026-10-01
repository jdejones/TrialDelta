import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


@pytest.fixture
def desktop(monkeypatch):
    tkinter = pytest.importorskip("tkinter")
    from trialdelta.admin_app import AdminApp

    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:
        pytest.skip(f"Tk display is unavailable on this test host: {exc}")
    root.withdraw()
    app = AdminApp(root, SimpleNamespace(safe_error=lambda exc: type(exc).__name__), initialize=False)
    app.identity = {"id": 7}
    app._set_enabled(True)
    monkeypatch.setattr(app, "refresh", lambda: None)
    try:
        yield app
    finally:
        if app.bulk_cancel is not None:
            app.bulk_cancel.set()
        _wait_for_idle(app)
        # Tk variables must be released on this thread, before a later API test
        # can collect the dashboard's callback cycles from a worker thread.
        app.metrics.clear()
        for name in tuple(vars(app)):
            if isinstance(getattr(app, name), tkinter.Variable):
                setattr(app, name, None)
        root.destroy()


def _wait_for_idle(app):
    deadline = time.monotonic() + 4
    while app.busy and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    assert not app.busy


@pytest.mark.parametrize("scope,expected_ids", [("healthcare", None), ("all", None), ("selected", [12])])
def test_bulk_worker_keeps_dashboard_responsive_and_cancel_available(desktop, scope, expected_ids):
    app = desktop
    started = threading.Event()
    calls = []
    main_thread = threading.get_ident()

    def download(user_id, **options):
        assert threading.get_ident() != main_thread
        calls.append((user_id, options))
        options["emit"]("Page 2 saved: 1,500 scanned, 80 trials.")
        started.set()
        options["cancel"].wait(3)
        return {"status": "cancelled", "job_id": 3, "pages": 2, "scanned": 1500, "studies": 80, "skipped": 1420, "changes": 6}

    app.ops.run_bulk_download = download
    app.companies.insert("", "end", iid="12", values=("TEST", "Test company"))
    app.companies.selection_set("12")
    app.bulk_scope.set(scope)
    app.bulk_since.insert(0, "2026-09-01")
    app.bulk_through.insert(0, "2026-09-24")
    app._start_bulk()
    assert started.wait(1)
    assert app.busy
    assert all(str(button["state"]) == "disabled" for button in app.buttons)
    assert str(app.bulk_cancel_button["state"]) == "normal"
    assert str(app.bulk_since["state"]) == "disabled"
    assert str(app.bulk_through["state"]) == "disabled"
    app._poll()
    assert "1,500 scanned" in app.bulk_status.get()
    responsive = []
    app.root.after(0, lambda: responsive.append(True))
    app.root.update()
    assert responsive == [True]

    app._cancel_bulk()
    assert str(app.bulk_cancel_button["state"]) == "disabled"
    _wait_for_idle(app)
    assert calls[0][0] == 7
    assert calls[0][1]["company_ids"] == expected_ids
    assert calls[0][1]["updated_since"] == "2026-09-01"
    assert calls[0][1]["updated_through"] == "2026-09-24"
    assert calls[0][1]["discover_companies"] is (scope == "healthcare")
    assert calls[0][1]["oncology_only"] is False
    assert calls[0][1]["cancel"].is_set()
    assert app.bulk_cancel is None
    assert "cancelled" in app.bulk_status.get()
    assert "1,500 scanned" in app.bulk_status.get()
    assert "saved checkpoint" in app.bulk_status.get()
    assert str(app.bulk_since["state"]) == "normal"
    assert str(app.bulk_through["state"]) == "normal"


def test_bulk_defaults_to_healthcare_without_a_specialty_limit(desktop, monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "_run_bulk", lambda **options: calls.append(options))
    assert desktop.bulk_scope.get() == "healthcare"
    desktop._start_bulk()
    assert calls == [{"company_ids": None, "updated_since": None, "updated_through": None, "discover_companies": True, "oncology_only": False}]


def test_healthcare_directory_refresh_does_not_download_trials(desktop):
    calls = []
    main_thread = threading.get_ident()

    def discover(user_id, emit):
        assert threading.get_ident() != main_thread
        calls.append(user_id)
        emit("Read Healthcare_Data reference files.")
        return {"discovered": 130, "created": 105, "updated": 25, "skipped": 4}

    desktop.ops.discover_bulk_companies = discover
    desktop._discover_bulk_companies()
    _wait_for_idle(desktop)
    assert calls == [7]
    assert "130 discovered" in desktop.bulk_status.get()
    assert "105 added" in desktop.bulk_status.get()
    assert str(desktop.bulk_cancel_button["state"]) == "disabled"


@pytest.mark.parametrize("date", ["2026-02-30", "09/01/2026", "2026-9-01"])
@pytest.mark.parametrize("field,label", [("bulk_since", "updated-since"), ("bulk_through", "updated-through")])
def test_bulk_rejects_invalid_date_before_start(desktop, monkeypatch, date, field, label):
    calls, dialogs = [], []
    monkeypatch.setattr(desktop, "_run_bulk", lambda **options: calls.append(options))
    monkeypatch.setattr("trialdelta.admin_app.messagebox.showinfo", lambda *args, **kwargs: dialogs.append(args))
    getattr(desktop, field).insert(0, date)
    desktop._start_bulk()
    assert not calls
    assert dialogs[0][0] == f"Check {label} date"


@pytest.mark.parametrize("field,label", [("bulk_since", "updated-since"), ("bulk_through", "updated-through")])
def test_bulk_rejects_future_dates_before_start(desktop, monkeypatch, field, label):
    calls, dialogs = [], []
    monkeypatch.setattr(desktop, "_run_bulk", lambda **options: calls.append(options))
    monkeypatch.setattr("trialdelta.admin_app.messagebox.showinfo", lambda *args, **kwargs: dialogs.append(args))
    getattr(desktop, field).insert(0, (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat())
    desktop._start_bulk()
    assert not calls
    assert dialogs[0][0] == f"Check {label} date"
    assert "today or an earlier date" in dialogs[0][1]


def test_bulk_rejects_reversed_range_before_start(desktop, monkeypatch):
    calls, dialogs = [], []
    monkeypatch.setattr(desktop, "_run_bulk", lambda **options: calls.append(options))
    monkeypatch.setattr("trialdelta.admin_app.messagebox.showinfo", lambda *args, **kwargs: dialogs.append(args))
    desktop.bulk_since.insert(0, "2026-09-25")
    desktop.bulk_through.insert(0, "2026-09-01")
    desktop._start_bulk()
    assert not calls
    assert dialogs[0][0] == "Check date range"


@pytest.mark.parametrize("since,through", [
    ("2026-09-01", "2026-09-24"),
    ("2026-09-01", "2026-09-01"),
    ("2026-09-01", ""),
    ("", "2026-09-24"),
    ("", ""),
])
def test_bulk_accepts_closed_single_day_and_open_ranges(desktop, monkeypatch, since, through):
    calls = []
    monkeypatch.setattr(desktop, "_run_bulk", lambda **options: calls.append(options))
    desktop.bulk_since.insert(0, f"  {since}  ")
    desktop.bulk_through.insert(0, f"  {through}  ")
    desktop._start_bulk()
    assert len(calls) == 1
    assert calls[0]["updated_since"] == (since or None)
    assert calls[0]["updated_through"] == (through or None)


def test_bulk_selected_scope_requires_company_selection(desktop, monkeypatch):
    calls, dialogs = [], []
    monkeypatch.setattr(desktop, "_run_bulk", lambda **options: calls.append(options))
    monkeypatch.setattr("trialdelta.admin_app.messagebox.showinfo", lambda *args, **kwargs: dialogs.append(args))
    desktop.bulk_scope.set("selected")
    desktop._start_bulk()
    assert not calls
    assert dialogs[0][0] == "Select companies"


@pytest.mark.parametrize("status", ["partial", "cancelled", "interrupted", "running"])
def test_bulk_resume_uses_saved_run_without_current_filters(desktop, monkeypatch, status):
    calls = []
    monkeypatch.setattr(desktop, "_run_bulk", lambda **options: calls.append(options))
    desktop.bulk_scope.set("selected")
    desktop.bulk_since.insert(0, "invalid current input")
    desktop.bulk_through.insert(0, "also invalid")
    desktop.bulk_rows = {"21": {"id": 21, "status": status}}
    desktop.bulk_jobs.insert("", "end", iid="21", values=(21, status))
    desktop.bulk_jobs.selection_set("21")
    desktop._resume_bulk()
    assert calls == [{"resume_job_id": 21}]


def test_bulk_resume_explains_legacy_specialty_scope_without_changing_checkpoint(desktop, monkeypatch):
    calls, messages = [], []
    monkeypatch.setattr(desktop, "_run_bulk", lambda **options: calls.append(options))
    monkeypatch.setattr(desktop, "_append_log", messages.append)
    desktop.bulk_rows = {"21": {"id": 21, "status": "partial", "oncology_only": True}}
    desktop.bulk_jobs.insert("", "end", iid="21", values=(21, "partial"))
    desktop.bulk_jobs.selection_set("21")
    desktop._resume_bulk()
    assert calls == [{"resume_job_id": 21}]
    assert len(messages) == 1
    assert "legacy specialty filter" in messages[0]
    assert "Start a new download for all conditions and study types" in messages[0]


def test_bulk_completed_run_cannot_resume(desktop, monkeypatch):
    calls, dialogs = [], []
    monkeypatch.setattr(desktop, "_run_bulk", lambda **options: calls.append(options))
    monkeypatch.setattr("trialdelta.admin_app.messagebox.showinfo", lambda *args, **kwargs: dialogs.append(args))
    desktop.bulk_rows = {"21": {"id": 21, "status": "complete"}}
    desktop.bulk_jobs.insert("", "end", iid="21", values=(21, "complete"))
    desktop.bulk_jobs.selection_set("21")
    desktop._resume_bulk()
    assert not calls
    assert dialogs[0][0] == "Run is complete"


@pytest.mark.parametrize("filters,expected_range", [
    ({"updated_since": "2026-09-01"}, "2026-09-01 – Present"),
    ({"updated_since": "2026-09-01", "updated_through": "2026-09-24"}, "2026-09-01 – 2026-09-24"),
    ({"updated_through": "2026-09-24"}, "Any date – 2026-09-24"),
    ({}, "All dates"),
])
def test_bulk_saved_runs_render_and_keep_selection(desktop, filters, expected_range):
    snapshot = {
        "counts": {key: 0 for key in (*desktop.metrics, "pending_changes", "pending_evidence")},
        "companies": [], "jobs": [], "database": "sqlite",
        "bulk_jobs": [{"id": 3, "status": "partial", "started_at": "2026-09-26T12:00:00+00:00", "finished_at": None, **filters, "pages": 2, "scanned": 1500, "studies": 80, "skipped": 1420, "changes": 6}],
    }
    desktop._show_data((snapshot, [], []))
    desktop.bulk_jobs.selection_set("3")
    desktop._show_data((snapshot, [], []))
    values = desktop.bulk_jobs.item("3", "values")
    assert values[1] == "partial"
    assert values[3:] == (expected_range, "2", "1500", "80", "1420", "6")
    assert desktop.bulk_jobs.selection() == ("3",)


def test_bulk_failure_clears_cancel_and_restores_controls(desktop, monkeypatch):
    dialogs = []
    monkeypatch.setattr("trialdelta.admin_app.messagebox.showerror", lambda *args, **kwargs: dialogs.append(args))

    def failed(*args, **kwargs):
        raise RuntimeError("source unavailable")

    desktop.ops.run_bulk_download = failed
    desktop._start_bulk()
    _wait_for_idle(desktop)
    assert dialogs
    assert desktop.bulk_cancel is None
    assert str(desktop.bulk_cancel_button["state"]) == "disabled"
    assert str(desktop.bulk_since["state"]) == "normal"
    assert str(desktop.bulk_through["state"]) == "normal"
    assert "needs attention" in desktop.bulk_status.get()


def _saved_bulk_snapshot(app, error_messages):
    failed = {
        "id": 8, "kind": "bulk", "status": "partial",
        "started_at": "2026-09-26T12:00:00+00:00", "finished_at": None,
        "companies": 2, "pages": 0, "scanned": 0, "studies": 0, "skipped": 0, "changes": 0,
        "errors": len(error_messages), "error_messages": error_messages,
    }
    complete = {**failed, "id": 7, "status": "complete", "pages": 2, "errors": 0, "error_messages": []}
    return {
        "counts": {key: 0 for key in (*app.metrics, "pending_changes", "pending_evidence")},
        "companies": [], "jobs": [failed, complete], "bulk_jobs": [failed, complete], "database": "sqlite",
    }


def test_partial_completion_keeps_failure_reason_and_explains_zero_saved_pages(desktop):
    from trialdelta.bulk_download import DATABASE_STORAGE_ERROR

    def download(user_id, **options):
        options["emit"](DATABASE_STORAGE_ERROR)
        return {
            "status": "partial", "job_id": 8, "pages": 0,
            "errors": [DATABASE_STORAGE_ERROR, "mysql://root:never-display-this@localhost"],
        }

    desktop.ops.run_bulk_download = download
    desktop._start_bulk()
    _wait_for_idle(desktop)
    assert "Bulk run #8 partial" in desktop.bulk_status.get()
    assert "Stopped before saving any pages" in desktop.bulk_status.get()
    assert DATABASE_STORAGE_ERROR in desktop.bulk_status.get()
    assert DATABASE_STORAGE_ERROR in desktop.log.get("1.0", "end")
    assert "never-display-this" not in desktop.bulk_status.get()
    assert "never-display-this" not in desktop.log.get("1.0", "end")


def test_saved_bulk_failure_reason_is_available_after_loading_and_refresh(desktop):
    from trialdelta.bulk_download import DATABASE_DATA_ERROR

    snapshot = _saved_bulk_snapshot(desktop, [DATABASE_DATA_ERROR])
    desktop._show_data((snapshot, [], []))
    desktop.bulk_jobs.selection_set("8")
    desktop.jobs.selection_set("8")
    desktop.root.update()
    for variable in (desktop.bulk_status, desktop.history_status):
        assert DATABASE_DATA_ERROR in variable.get()
        assert "Stopped before saving any pages" in variable.get()

    desktop._show_data((snapshot, [], []))
    assert desktop.bulk_jobs.selection() == ("8",)
    assert desktop.jobs.selection() == ("8",)
    assert DATABASE_DATA_ERROR in desktop.bulk_status.get()
    assert DATABASE_DATA_ERROR in desktop.history_status.get()


def test_bulk_result_stays_selected_after_completion_refresh(desktop, monkeypatch):
    from trialdelta.bulk_download import DATABASE_STORAGE_ERROR

    snapshot = _saved_bulk_snapshot(desktop, [DATABASE_STORAGE_ERROR])
    desktop._show_data((snapshot, [], []))
    desktop.bulk_jobs.selection_set("7")
    desktop.root.update()
    monkeypatch.setattr(desktop, "refresh", lambda: desktop._show_data((snapshot, [], [])))
    desktop.ops.run_bulk_download = lambda *args, **kwargs: {
        "status": "partial", "job_id": 8, "pages": 0, "errors": [DATABASE_STORAGE_ERROR],
    }

    desktop._start_bulk()
    _wait_for_idle(desktop)
    assert desktop.bulk_jobs.selection() == ("8",)
    assert DATABASE_STORAGE_ERROR in desktop.bulk_status.get()


def test_saved_run_selection_does_not_replace_active_bulk_progress(desktop):
    from trialdelta.bulk_download import DATABASE_DATA_ERROR

    desktop._show_data((_saved_bulk_snapshot(desktop, [DATABASE_DATA_ERROR]), [], []))
    desktop.bulk_cancel = threading.Event()
    desktop.bulk_status.set("Downloading the next page…")
    desktop.bulk_jobs.selection_set("8")
    desktop.root.update()
    assert desktop.bulk_status.get() == "Downloading the next page…"


def test_company_history_uses_company_failure_text_without_bulk_diagnostics(desktop):
    snapshot = _saved_bulk_snapshot(desktop, [])
    snapshot["jobs"][0].update({
        "kind": "company", "errors": 1,
        "error_messages": ["mysql://root:never-display-this@localhost"],
    })
    snapshot["bulk_jobs"] = []
    desktop._show_data((snapshot, [], []))
    desktop.jobs.selection_set("8")
    desktop.root.update()
    assert desktop.history_status.get() == (
        "Registry run #8 partial. 1 company refresh failed; last good snapshots are retained."
    )


@pytest.mark.parametrize("operation,expected_message", [
    ("busy", "Another registry collection is running. Try again when it finishes."),
    ("discovery", "Healthcare company list refreshed: 130 discovered, 105 added, 25 updated, 4 skipped."),
])
def test_bulk_notice_survives_refresh_with_an_old_run_selected(desktop, monkeypatch, operation, expected_message):
    from trialdelta.bulk_download import DATABASE_DATA_ERROR

    snapshot = _saved_bulk_snapshot(desktop, [DATABASE_DATA_ERROR])
    desktop._show_data((snapshot, [], []))
    desktop.bulk_jobs.selection_set("8")
    desktop.root.update()
    desktop.ops.dashboard_snapshot = lambda user_id: snapshot
    desktop.ops.review_items = lambda user_id: []
    desktop.ops.inbox_items = lambda user_id: []
    monkeypatch.setattr(desktop, "refresh", type(desktop).refresh.__get__(desktop))
    if operation == "busy":
        desktop.ops.run_bulk_download = lambda *args, **kwargs: {"status": "busy"}
        desktop._start_bulk()
    else:
        desktop.ops.discover_bulk_companies = lambda *args, **kwargs: {
            "discovered": 130, "created": 105, "updated": 25, "skipped": 4,
        }
        desktop._discover_bulk_companies()
    _wait_for_idle(desktop)
    desktop.root.update()
    assert desktop.bulk_jobs.selection() == ("8",)
    assert desktop.bulk_status.get() == expected_message

    # A later explicit selection still shows the saved run's details.
    desktop.bulk_jobs.selection_set("7")
    desktop.root.update()
    assert "Bulk run #7 complete" in desktop.bulk_status.get()
    desktop.bulk_jobs.selection_set("8")
    desktop.root.update()
    assert DATABASE_DATA_ERROR in desktop.bulk_status.get()
