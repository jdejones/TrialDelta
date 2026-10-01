import gc
from types import SimpleNamespace

import pytest


@pytest.fixture
def desktop(monkeypatch):
    tkinter = pytest.importorskip("tkinter")
    from trialdelta import admin_app

    health = {
        "state": "not_started", "detail": "No worker has reported health.",
        "heartbeat_at": None, "next_run_at": None,
    }
    monkeypatch.setattr(admin_app, "read_health", lambda: health.copy())
    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:
        pytest.skip(f"Tk display is unavailable on this test host: {exc}")
    root.withdraw()
    app = admin_app.AdminApp(root, SimpleNamespace(), initialize=False)
    try:
        yield app, health
    finally:
        _dispose_desktop(app)
        # A monkeypatch can retain a root-bound method until its own teardown.
        monkeypatch.undo()
        del app, root
        gc.collect()


def _dispose_desktop(app):
    root = app.root
    app.busy = False
    if not app._closed:
        # Cancel the ordinary event poll as well as the health poll; these
        # isolated tests never leave background work running after teardown.
        for callback in root.tk.call("after", "info"):
            root.after_cancel(callback)
        app._worker_health_after = None
        app._close()
    # Pytest may retain the fixture result after this finalizer. Release every
    # widget and variable it owns now, while still on the Tcl creation thread.
    vars(app).clear()


def poll(app):
    app.root.after_cancel(app._worker_health_after)
    app._poll_worker_health()


@pytest.mark.parametrize("state", ["failed", "unresponsive", "unknown"])
def test_failure_banner_is_global_and_updates_while_busy_and_signed_out(desktop, state):
    from tkinter import ttk

    app, health = desktop
    app.busy = True
    assert app.identity is None
    health.update(state=state, detail="The worker needs attention.")
    poll(app)
    assert app.worker_health_banner.master is app.root
    assert app.root.winfo_children()[0] is app.worker_health_banner
    assert app.worker_health_banner.pack_info()["fill"] == "x"
    assert app.worker_health_banner.cget("bg") == "#a61b1b"
    assert app.worker_health_detail.get() == "The worker needs attention."
    assert app._worker_health_detail_label.winfo_manager() == "pack"
    assert app._worker_health_after is not None
    assert app.busy
    outer = app.root.winfo_children()[1]
    notebook = next(child for child in outer.winfo_children() if isinstance(child, ttk.Notebook))
    for tab in notebook.tabs():
        notebook.select(tab)
        app.root.update_idletasks()
        assert app.worker_health_banner.winfo_manager() == "pack"


@pytest.mark.parametrize("state", ["not_started", "stopped", "starting", "collecting", "waiting"])
def test_expected_worker_states_are_compact_and_not_red(desktop, state):
    app, health = desktop
    health["state"] = "failed"
    poll(app)
    alert_padding = int(app.worker_health_banner.cget("pady"))
    health["state"] = state
    poll(app)
    assert app.worker_health_banner.cget("bg") != "#a61b1b"
    assert app._worker_health_detail_label.winfo_manager() == ""
    assert int(app.worker_health_banner.cget("pady")) < alert_padding
    assert app._worker_health_state == state


def test_last_heartbeat_and_next_collection_are_local_and_log_only_changes(desktop):
    from trialdelta.admin_app import _date

    app, health = desktop
    health.update(
        state="waiting", heartbeat_at="2026-09-27T12:00:00+00:00",
        next_run_at="2026-09-28T12:00:00+00:00",
    )
    poll(app)
    assert _date(health["heartbeat_at"]) in app.worker_health_times.get()
    assert _date(health["next_run_at"]) in app.worker_health_times.get()
    assert "(local)" in app.worker_health_times.get()
    initial_log = app.log.get("1.0", "end")
    health["heartbeat_at"] = "2026-09-27T12:01:00+00:00"
    poll(app)
    assert app.log.get("1.0", "end") == initial_log
    assert _date(health["heartbeat_at"]) in app.worker_health_times.get()
    health.update(state="unresponsive", detail="No recent heartbeat.")
    poll(app)
    changed_log = app.log.get("1.0", "end")
    assert changed_log.count("DAILY WORKER IS NOT RESPONDING") == 1
    poll(app)
    assert app.log.get("1.0", "end") == changed_log


def test_monitor_read_failure_is_safe_and_monitoring_recovers(desktop, monkeypatch):
    from trialdelta import admin_app

    app, health = desktop

    def fail():
        raise OSError("private connection information")

    monkeypatch.setattr(admin_app, "read_health", fail)
    poll(app)
    assert app._worker_health_state == "unknown"
    assert app.worker_health_banner.cget("bg") == "#a61b1b"
    assert "private" not in app.worker_health_detail.get()
    assert "private" not in app.log.get("1.0", "end")
    assert app._worker_health_after is not None
    health["state"] = "waiting"
    monkeypatch.setattr(admin_app, "read_health", lambda: health.copy())
    poll(app)
    assert app._worker_health_state == "waiting"


def test_health_poll_callback_runs_without_database_work(desktop):
    app, health = desktop
    health["state"] = "failed"
    app.root.after_cancel(app._worker_health_after)
    app._worker_health_after = app.root.after(0, app._poll_worker_health)
    app.root.update()
    assert app._worker_health_state == "failed"
    assert app._worker_health_after is not None
    assert vars(app.ops) == {}


def test_close_cancels_pending_health_callback(desktop, monkeypatch):
    app, _ = desktop
    callback = app._worker_health_after
    cancelled = []
    original_cancel = app.root.after_cancel

    def cancel(token):
        cancelled.append(token)
        original_cancel(token)

    monkeypatch.setattr(app.root, "after_cancel", cancel)
    app._close()
    assert callback in cancelled
    assert app._worker_health_after is None
    assert app._closed


def test_prior_failure_is_shown_immediately_on_console_open(monkeypatch):
    tkinter = pytest.importorskip("tkinter")
    from trialdelta import admin_app

    monkeypatch.setattr(admin_app, "read_health", lambda: {
        "state": "failed", "detail": "The previous worker stopped unexpectedly.",
        "heartbeat_at": "2026-09-26T12:00:00+00:00", "next_run_at": None,
    })
    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:
        pytest.skip(f"Tk display is unavailable on this test host: {exc}")
    root.withdraw()
    app = admin_app.AdminApp(root, SimpleNamespace(), initialize=False)
    try:
        assert app._worker_health_state == "failed"
        assert app.worker_health_banner.cget("bg") == "#a61b1b"
        assert "previous worker" in app.worker_health_detail.get()
    finally:
        _dispose_desktop(app)
        monkeypatch.undo()
        del app, root
        gc.collect()
