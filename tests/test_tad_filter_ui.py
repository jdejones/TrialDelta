"""The unresolved view reaches editable evidence without changing saved data."""

import gc
import threading
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def desktop():
    tkinter = pytest.importorskip("tkinter")
    from trialdelta.admin_app import AdminApp

    try:
        root = tkinter.Tk()
    except tkinter.TclError:
        pytest.skip("Tk display is unavailable on this test host")
    root.withdraw()
    app = AdminApp(root, SimpleNamespace(safe_error=lambda exc: type(exc).__name__), initialize=False)
    app.identity = {"id": 7}
    app._set_enabled(True)
    try:
        yield app
    finally:
        deadline = time.monotonic() + 5
        while app.busy and time.monotonic() < deadline:
            root.update()
            app._poll()
            time.sleep(0.01)
        app.metrics.clear()
        for name in tuple(vars(app)):
            if isinstance(getattr(app, name), tkinter.Variable):
                setattr(app, name, None)
        root.destroy()
        del app, root
        gc.collect()


def wait_for_desktop(app):
    deadline = time.monotonic() + 5
    while app.busy and time.monotonic() < deadline:
        app.root.update()
        app._poll()
        time.sleep(0.01)
    assert not app.busy


def test_view_selector_reads_unresolved_in_background_and_keeps_edit_available(desktop):
    started, release = threading.Event(), threading.Event()
    calls = []
    row = {
        "id": 501, "ticker": "TEST", "tad": None, "precision": "unknown",
        "status": "unidentifiable", "form_type": "8-K", "source_count": 1,
        "reason": "Year not stated", "context": "The target action date is November 14.",
        "imported_at": "2026-09-27T10:00:00", "filing_sha256": "a" * 64,
        "selection_identity": "a" * 64,
    }

    def read(user_id, *, status=""):
        calls.append((user_id, status))
        started.set()
        assert release.wait(3)
        items = [row] if status == "unresolved" else []
        return {"items": items, "total": len(items), "identified": 0, "month": 0, "unresolved": len(items)}

    desktop.ops.tad_items = read
    assert desktop.tad_view.get() == "All saved"
    assert str(desktop.tad_view_selector["state"]) == "readonly"
    desktop.tad_view.set("Unresolved")
    desktop.tad_view_selector.event_generate("<<ComboboxSelected>>")
    try:
        assert started.wait(1)
        assert desktop.busy
        assert str(desktop.tad_view_selector["state"]) == "disabled"
    finally:
        release.set()
    wait_for_desktop(desktop)
    assert calls == [(7, "unresolved")]
    assert str(desktop.tad_view_selector["state"]) == "readonly"
    assert "Unresolved: 1 saved results" in desktop.tad_count.get()
    desktop.tads.selection_set("501")
    desktop._show_tad()
    assert str(desktop.tad_edit_button["state"]) == "normal"
    desktop._edit_tad()
    assert desktop.tad_entry_fields["ticker"].get() == "TEST"
    assert desktop.tad_entry_fields["tad"].get() == ""
    assert desktop.tad_entry_fields["notes"].get("1.0", "end-1c") == row["context"]
    desktop._close_tad_entry()
    desktop.tad_view.set("All saved")
    desktop.tad_view_selector.event_generate("<<ComboboxSelected>>")
    wait_for_desktop(desktop)
    assert calls == [(7, "unresolved"), (7, "")]
    assert not desktop.tads.selection()
    assert str(desktop.tad_edit_button["state"]) == "disabled"


def test_empty_unresolved_view_explains_how_to_enter_a_missing_result(desktop):
    desktop.tad_view.set("Unresolved")
    desktop._show_tads({"items": [], "total": 0, "identified": 0, "month": 0, "unresolved": 0})
    message = desktop.tad_text.get("1.0", "end")
    assert "No unresolved" in message
    assert "All saved" in message
    assert "Add date" in message
