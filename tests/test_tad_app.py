"""TAD imports stay in the administrator; browsing reads saved results only."""

import gc
import importlib
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from trialdelta import admin_ops as ops
from trialdelta.auth import browsing_user
from trialdelta.db import TargetActionDate, User

web = importlib.import_module("trialdelta.app")


def test_home_tads_reads_saved_month_and_unresolved_results(client, monkeypatch):
    rows = [
        {"id": 1, "ticker": "TEST", "tad": "2027-10", "precision": "month", "status": "identified"},
        {"id": 2, "ticker": "OTHER", "tad": None, "precision": "unknown", "status": "unidentifiable"},
    ]
    reads = []

    def saved(db, **options):
        reads.append(options)
        return {"items": rows}

    def unexpected():
        pytest.fail("Browsing must not import source filings")

    monkeypatch.setattr(web, "list_target_action_dates", saved)
    monkeypatch.setattr(ops, "import_target_action_dates", unexpected)
    response = client.get("/api/home/tads")
    assert response.status_code == 200
    assert response.json() == rows
    assert reads == [{"limit": 500}]
    assert response.headers["cache-control"] == "no-store"
    assert client.post("/api/home/tads").status_code == 405
    assert client.post("/api/tads/import").status_code == 404


def test_home_tads_uses_existing_browsing_access_and_hides_database_errors(client, monkeypatch):
    from fastapi import HTTPException

    def denied():
        raise HTTPException(401, "Sign in required")

    web.app.dependency_overrides[browsing_user] = denied
    assert client.get("/api/home/tads").status_code == 401
    web.app.dependency_overrides[browsing_user] = lambda: None

    def failed(*args, **kwargs):
        raise OperationalError("mysql://user:secret@private-host", {}, Exception("private details"))

    monkeypatch.setattr(web, "list_target_action_dates", failed)
    response = client.get("/api/home/tads")
    assert response.status_code == 503
    assert response.json() == {"detail": "Target action dates are temporarily unavailable."}


def test_tad_admin_operations_check_access_before_source_read(db, monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "import_target_action_dates", lambda: calls.append("import") or {"status": "complete"})
    monkeypatch.setattr(ops, "list_target_action_dates", lambda db, **kwargs: calls.append("read") or {"items": []})
    reader = User(email="tad-reader@example.org", password_hash="unused", is_admin=False)
    db.add(reader)
    db.commit()
    for operation in (ops.run_tad_import, ops.tad_items):
        with pytest.raises(ops.AdminOperationError, match="Administrator"):
            operation(reader.id)
    assert not calls
    admin = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    assert ops.run_tad_import(admin.id)["status"] == "complete"
    assert ops.tad_items(admin.id) == {"items": []}
    assert calls == ["import", "read"]


def test_tad_hosted_admin_cannot_use_local_identity(db, monkeypatch):
    admin = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    monkeypatch.setattr(ops.auth, "MODE", "hosted")
    with pytest.raises(ops.AdminOperationError, match="Administrator"):
        ops.run_tad_import(admin.id)
    with pytest.raises(ops.AdminOperationError, match="Administrator"):
        ops.tad_items(admin.id)


def test_manual_date_requires_administrator_before_validation_or_write(db, monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "add_manual_target_action_date", lambda *args, **kwargs: calls.append(kwargs))
    reader = User(email="manual-reader@example.org", password_hash="unused", is_admin=False)
    db.add(reader)
    db.commit()
    for user_id in (reader.id, -1):
        with pytest.raises(ops.AdminOperationError, match="Administrator"):
            ops.add_tad(user_id, ticker="TEST", tad="2027-10")
    admin = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    monkeypatch.setattr(ops.auth, "MODE", "hosted")
    with pytest.raises(ops.AdminOperationError, match="Administrator"):
        ops.add_tad(admin.id, ticker="TEST", tad="2027-10")
    assert not calls


def test_manual_date_admin_save_is_committed_and_visible_to_readers(client, db):
    admin = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    result = ops.add_tad(admin.id, ticker="test", tad="2027-10", notes="Month and year confirmed.")
    assert result["status"] == "saved"
    stored = db.get(TargetActionDate, result["id"])
    assert stored.tad == "2027-10"
    assert stored.precision == "month"
    assert stored.entered_by == admin.id
    assert stored.entry_method == "manual"
    public = client.get("/api/home/tads").json()[0]
    assert public["tad"] == "2027-10"
    assert public["entry_method"] == "manual"
    assert public["context"] == "Month and year confirmed."
    assert "entered_by" not in public
    assert "selection_identity" not in public
    assert client.post("/api/home/tads", json={"ticker": "TEST", "tad": "2028-01"}).status_code == 405
    assert client.post("/api/tads/manual", json={"ticker": "TEST", "tad": "2028-01"}).status_code == 404


def test_manual_date_validation_is_safe_and_does_not_write(db):
    admin = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    with pytest.raises(ops.TADValidationError) as invalid:
        ops.add_tad(admin.id, ticker="TEST", tad="2027-02-30")
    assert ops.safe_error(invalid.value) == str(invalid.value)
    assert db.scalar(select(TargetActionDate)) is None


def test_delete_date_requires_administrator_before_access(db, monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "delete_target_action_date", lambda *args, **kwargs: calls.append(args))
    reader = User(email="delete-reader@example.org", password_hash="unused", is_admin=False)
    db.add(reader)
    db.commit()
    for user_id in (reader.id, -1):
        with pytest.raises(ops.AdminOperationError, match="Administrator"):
            ops.delete_tad(user_id, 1, expected_identity="a" * 64)
    admin = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    monkeypatch.setattr(ops.auth, "MODE", "hosted")
    with pytest.raises(ops.AdminOperationError, match="Administrator"):
        ops.delete_tad(admin.id, 1, expected_identity="a" * 64)
    assert not calls


def test_delete_date_commits_group_removal_and_updates_public_feed(client, db):
    admin = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    first = ops.add_tad(admin.id, ticker="TEST", tad="2027-10", notes="First source.")
    second = ops.add_tad(admin.id, ticker="TEST", tad="2027-10", notes="Second source.")
    other = ops.add_tad(admin.id, ticker="TEST", tad="2027-11")
    selected = next(row for row in ops.tad_items(admin.id)["items"] if row["id"] == first["id"])
    result = ops.delete_tad(admin.id, first["id"], expected_identity=selected["selection_identity"])
    assert result["deleted"] == 2
    assert result["ticker"] == "TEST"
    assert result["tad"] == "2027-10"
    assert db.get(TargetActionDate, first["id"]) is None
    assert db.get(TargetActionDate, second["id"]) is None
    assert db.get(TargetActionDate, other["id"]) is not None
    assert [row["tad"] for row in client.get("/api/home/tads").json()] == ["2027-11"]
    with pytest.raises(ops.TADValidationError):
        ops.delete_tad(admin.id, first["id"], expected_identity=selected["selection_identity"])
    assert client.delete("/api/home/tads").status_code == 405
    assert client.delete(f"/api/tads/{other['id']}").status_code == 404


def test_edit_date_requires_administrator_before_access(db, monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "edit_target_action_date", lambda *args, **kwargs: calls.append(kwargs))
    reader = User(email="edit-reader@example.org", password_hash="unused", is_admin=False)
    db.add(reader)
    db.commit()
    values = {"expected_identity": "a" * 64, "ticker": "TEST", "tad": "2027-10"}
    for user_id in (reader.id, -1):
        with pytest.raises(ops.AdminOperationError, match="Administrator"):
            ops.edit_tad(user_id, 1, **values)
    admin = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    monkeypatch.setattr(ops.auth, "MODE", "hosted")
    with pytest.raises(ops.AdminOperationError, match="Administrator"):
        ops.edit_tad(admin.id, 1, **values)
    assert not calls


def test_edit_date_keeps_original_evidence_and_updates_public_feed(client, db):
    admin = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    saved = ops.add_tad(admin.id, ticker="TEST", tad="2027-10", notes="Original notes.")
    original = ops.tad_items(admin.id)["items"][0]
    result = ops.edit_tad(admin.id, saved["id"], expected_identity=original["selection_identity"],
                          ticker="TEST", tad="2027-12-14", notes="Corrected notes.")
    assert result["status"] == "updated"
    unchanged_source = db.get(TargetActionDate, saved["id"])
    assert unchanged_source.tad == "2027-10"
    assert unchanged_source.context == "Original notes."
    public = client.get("/api/home/tads").json()[0]
    assert public["tad"] == "2027-12-14"
    assert public["precision"] == "day"
    assert public["context"] == "Corrected notes."
    assert public["original_tad"] == "2027-10"
    assert public["original_context"] == "Original notes."
    assert public["is_edited"] is True
    assert "entered_by" not in public and "selection_identity" not in public
    corrected = ops.tad_items(admin.id)["items"][0]
    assert corrected["selection_identity"] != original["selection_identity"]
    with pytest.raises(ops.TADValidationError):
        ops.edit_tad(admin.id, saved["id"], expected_identity=original["selection_identity"],
                     ticker="TEST", tad="2028-01")
    assert client.put("/api/home/tads", json={"tad": "2028-01"}).status_code == 405
    assert client.patch(f"/api/tads/{saved['id']}", json={"tad": "2028-01"}).status_code == 404


@pytest.fixture
def desktop(monkeypatch):
    tkinter = pytest.importorskip("tkinter")
    from trialdelta.admin_app import AdminApp

    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
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
        # Collect Tk callback cycles on the UI thread before a later TestClient
        # worker can trigger GC and release a Tcl interpreter on that thread.
        del app, root
        gc.collect()


def test_tad_desktop_import_is_explicit_and_does_not_block_ui(desktop):
    started, finish = threading.Event(), threading.Event()
    calls = []

    def importing(user_id):
        calls.append(user_id)
        started.set()
        assert finish.wait(3)
        return {"status": "complete", "scanned": 0, "imported": 0, "existing": 0, "unresolved": 0}

    desktop.ops.run_tad_import = importing
    desktop.ops.tad_items = lambda user_id: {"items": [], "total": 0, "identified": 0, "month": 0, "unresolved": 0}
    assert not calls
    desktop._import_tads()
    try:
        assert started.wait(1)
        assert desktop.busy
        responsive = []
        desktop.root.after(0, lambda: responsive.append(True))
        desktop.root.update()
        assert responsive == [True]
    finally:
        finish.set()
    deadline = time.monotonic() + 5
    while desktop.busy and time.monotonic() < deadline:
        desktop.root.update()
        desktop._poll()
        time.sleep(0.01)
    assert not desktop.busy
    assert calls == [7]
    assert "source table is empty" in desktop.tad_notice.get()
    assert "0 saved results" in desktop.tad_count.get()


def test_tad_desktop_preserves_month_and_unidentified_details(desktop):
    row = {"id": 1, "ticker": "TEST", "tad": "2027-10", "precision": "month",
           "status": "identified", "form_type": "8-K", "source_count": 1,
           "reason": "Month and year reported.", "context": "target action date of October 2027",
           "imported_at": "2026-09-26T10:00:00+00:00", "filing_sha256": "a" * 64}
    desktop._show_tads({"items": [row], "total": 1, "identified": 1, "month": 1, "unresolved": 0})
    assert desktop.tads.item("1", "values")[1] == "2027-10"
    desktop.tads.selection_set("1")
    desktop._show_tad()
    assert "target action date of October 2027" in desktop.tad_text.get("1.0", "end")
    assert "2027-10-01" not in desktop.tad_text.get("1.0", "end")


def _wait_for_desktop(desktop):
    deadline = time.monotonic() + 5
    while desktop.busy and time.monotonic() < deadline:
        desktop.root.update()
        desktop._poll()
        time.sleep(0.01)
    assert not desktop.busy


def _fill_manual_date(desktop):
    desktop._add_tad()
    desktop.tad_entry_fields["ticker"].insert(0, "TEST")
    desktop.tad_entry_fields["tad"].insert(0, "2027-10")
    desktop.tad_entry_fields["source_url"].insert(0, "https://example.org/source")
    desktop.tad_entry_fields["notes"].insert("1.0", "Month and year reported.")


def test_manual_date_dialog_saves_once_in_background_and_refreshes(desktop):
    started, finish = threading.Event(), threading.Event()
    calls, reads = [], []

    def save(user_id, **values):
        calls.append((user_id, values))
        started.set()
        assert finish.wait(3)
        return {"status": "saved", "ticker": "TEST", "tad": "2027-10"}

    def saved(user_id):
        reads.append(user_id)
        return {"items": [], "total": 0, "identified": 0, "month": 0, "unresolved": 0}

    desktop.ops.add_tad = save
    desktop.ops.tad_items = saved
    _fill_manual_date(desktop)
    window = desktop._tad_entry_window
    desktop._add_tad()
    assert desktop._tad_entry_window is window
    desktop._save_tad_entry()
    try:
        assert started.wait(1)
        assert desktop.busy
        assert all(str(widget.cget("state")) == "disabled" for widget in desktop.tad_entry_controls)
        desktop._save_tad_entry()
        desktop._close_tad_entry()
        assert desktop._tad_entry_window is window
        responsive = []
        desktop.root.after(0, lambda: responsive.append(True))
        desktop.root.update()
        assert responsive
    finally:
        finish.set()
    _wait_for_desktop(desktop)
    assert desktop._tad_entry_window is None
    assert calls == [(7, {"ticker": "TEST", "tad": "2027-10", "source_url": "https://example.org/source", "notes": "Month and year reported."})]
    assert reads == [7]
    assert "Manual date saved: TEST" in desktop.tad_notice.get()


def test_manual_date_failure_retains_form_for_correction(desktop, monkeypatch):
    errors = []
    monkeypatch.setattr("trialdelta.admin_app.messagebox.showerror", lambda *args, **kwargs: errors.append((args, kwargs)))
    desktop.ops.safe_error = ops.safe_error

    def failed(*args, **kwargs):
        raise ops.TADValidationError("Enter a valid calendar date.")

    desktop.ops.add_tad = failed
    _fill_manual_date(desktop)
    window = desktop._tad_entry_window
    desktop._save_tad_entry()
    _wait_for_desktop(desktop)
    assert desktop._tad_entry_window is window
    assert desktop.tad_entry_fields["tad"].get() == "2027-10"
    assert desktop.tad_entry_fields["notes"].get("1.0", "end-1c") == "Month and year reported."
    assert errors[0][1]["parent"] is window
    assert all(str(widget.cget("state")) == "normal" for widget in desktop.tad_entry_controls)
    desktop._close_tad_entry()
    assert desktop._tad_entry_window is None


def test_manual_date_cancel_and_signed_out_do_not_save(desktop):
    desktop.identity = None
    desktop._add_tad()
    assert desktop._tad_entry_window is None
    desktop.identity = {"id": 7}
    _fill_manual_date(desktop)
    desktop._close_tad_entry()
    assert desktop._tad_entry_window is None


def test_manual_date_details_do_not_claim_a_filing(desktop):
    row = {"id": 1, "ticker": "TEST", "tad": "2027-10", "precision": "month",
           "status": "identified", "form_type": "", "source_count": 0, "manual_count": 1,
           "entry_method": "manual", "evidence_method": "manual",
           "reason": "Manually entered.", "context": "Confirmed month only.",
           "imported_at": "2026-09-26T10:00:00+00:00", "filing_sha256": None}
    desktop._show_tads({"items": [row], "total": 1, "identified": 1, "month": 1, "unresolved": 0})
    desktop.tads.selection_set("1")
    desktop._show_tad()
    details = desktop.tad_text.get("1.0", "end")
    assert "Manual entry" in details
    assert "Confirmed month only." in details
    assert "Filing SHA" not in details
    assert "Accession" not in details
    assert "Imported:" not in details
    assert "2027-10-01" not in details


def _select_delete_fixture(desktop, *, unresolved=False):
    row = {"id": 15, "ticker": "TEST", "tad": None if unresolved else "2027-10",
           "precision": "unknown" if unresolved else "month",
           "status": "unidentifiable" if unresolved else "identified", "form_type": "8-K",
           "source_count": 1, "manual_count": 0 if unresolved else 1,
           "occurrence_count": 1 if unresolved else 3,
           "entry_method": "filing" if unresolved else "mixed", "evidence_method": "filing",
           "reason": "Fixture.", "context": "Reported evidence.", "imported_at": "2026-09-26",
           "filing_sha256": "a" * 64, "selection_identity": "b" * 64}
    desktop._show_tads({"items": [row], "total": 1, "identified": int(not unresolved),
                       "month": int(not unresolved), "unresolved": int(unresolved)})
    desktop.tads.selection_set("15")
    desktop._show_tad()


def test_delete_button_requires_selection_and_cancel_does_not_write(desktop, monkeypatch):
    calls, prompts, notices = [], [], []
    desktop.ops.delete_tad = lambda *args, **kwargs: calls.append(args)
    monkeypatch.setattr("trialdelta.admin_app.messagebox.showinfo", lambda *args, **kwargs: notices.append(args))
    monkeypatch.setattr("trialdelta.admin_app.messagebox.askyesno", lambda *args, **kwargs: prompts.append((args, kwargs)) or False)
    assert str(desktop.tad_delete_button.cget("state")) == "disabled"
    desktop._delete_tad()
    assert notices
    assert not prompts
    _select_delete_fixture(desktop)
    assert str(desktop.tad_delete_button.cget("state")) == "normal"
    desktop._delete_tad()
    assert not calls
    message = prompts[0][0][1]
    assert "TEST · 2027-10" in message
    assert "3 saved records" in message
    assert "All manual and filing records" in message
    assert "Later imports can recreate" in message
    assert prompts[0][1]["default"] == "no"
    assert desktop.tads.exists("15")
    desktop.tads.selection_remove("15")
    desktop._show_tad()
    assert str(desktop.tad_delete_button.cget("state")) == "disabled"


def test_delete_selected_date_runs_once_in_background_and_refreshes(desktop, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    calls, prompts = [], []

    def remove(user_id, item_id, *, expected_identity):
        calls.append((user_id, item_id, expected_identity))
        started.set()
        assert finish.wait(3)
        return {"status": "deleted", "ticker": "TEST", "tad": "2027-10", "deleted": 3}

    desktop.ops.delete_tad = remove
    desktop.ops.tad_items = lambda user_id: {"items": [], "total": 0, "identified": 0, "month": 0, "unresolved": 0}
    monkeypatch.setattr("trialdelta.admin_app.messagebox.askyesno", lambda *args, **kwargs: prompts.append(args) or True)
    _select_delete_fixture(desktop)
    desktop._delete_tad()
    try:
        assert started.wait(1)
        assert desktop.busy
        assert str(desktop.tad_delete_button.cget("state")) == "disabled"
        desktop._delete_tad()
        responsive = []
        desktop.root.after(0, lambda: responsive.append(True))
        desktop.root.update()
        assert responsive
    finally:
        finish.set()
    _wait_for_desktop(desktop)
    assert calls == [(7, 15, "b" * 64)]
    assert len(prompts) == 1
    assert not desktop.tads.get_children()
    assert str(desktop.tad_delete_button.cget("state")) == "disabled"
    assert "3 saved records removed" in desktop.tad_notice.get()


def test_delete_failure_keeps_selected_row_and_reenables_action(desktop, monkeypatch):
    errors = []
    monkeypatch.setattr("trialdelta.admin_app.messagebox.askyesno", lambda *args, **kwargs: True)
    monkeypatch.setattr("trialdelta.admin_app.messagebox.showerror", lambda *args, **kwargs: errors.append(args))

    def failed(*args, **kwargs):
        raise ops.TADValidationError("The saved date no longer exists. Refresh dates.")

    desktop.ops.delete_tad = failed
    desktop.ops.safe_error = ops.safe_error
    _select_delete_fixture(desktop)
    desktop._delete_tad()
    _wait_for_desktop(desktop)
    assert errors
    assert desktop.tads.exists("15")
    assert str(desktop.tad_delete_button.cget("state")) == "normal"


def test_unresolved_delete_confirmation_scopes_to_one_record_and_requires_signin(desktop, monkeypatch):
    prompts = []
    monkeypatch.setattr("trialdelta.admin_app.messagebox.askyesno", lambda *args, **kwargs: prompts.append(args) or False)
    _select_delete_fixture(desktop, unresolved=True)
    desktop.identity = None
    desktop._delete_tad()
    assert not prompts
    desktop.identity = {"id": 7}
    desktop._delete_tad()
    assert "Only this unresolved saved record" in prompts[0][1]
    assert "All manual and filing records" not in prompts[0][1]


def test_edit_requires_selection_prefills_form_and_cancel_preserves_it(desktop, monkeypatch):
    notices = []
    monkeypatch.setattr("trialdelta.admin_app.messagebox.showinfo", lambda *args, **kwargs: notices.append(args))
    assert str(desktop.tad_edit_button.cget("state")) == "disabled"
    desktop._edit_tad()
    assert notices and desktop._tad_entry_window is None
    _select_delete_fixture(desktop)
    desktop.tad_rows["15"]["filing_link"] = "https://example.org/original"
    assert str(desktop.tad_edit_button.cget("state")) == "normal"
    desktop._edit_tad()
    assert desktop._tad_entry_window.title() == "Edit target action date"
    assert desktop.tad_entry_fields["ticker"].get() == "TEST"
    assert desktop.tad_entry_fields["tad"].get() == "2027-10"
    assert desktop.tad_entry_fields["source_url"].get() == "https://example.org/original"
    assert desktop.tad_entry_fields["notes"].get("1.0", "end-1c") == "Reported evidence."
    window = desktop._tad_entry_window
    desktop._edit_tad()
    assert desktop._tad_entry_window is window
    desktop._close_tad_entry()
    assert desktop._tad_edit_selection is None
    assert desktop.tads.exists("15")
    desktop._add_tad()
    assert desktop.tad_entry_fields["ticker"].get() == ""
    assert desktop._tad_edit_selection is None
    desktop._close_tad_entry()


def test_edit_saves_snapshotted_selection_once_in_background(desktop):
    started, finish = threading.Event(), threading.Event()
    calls = []

    def edit(user_id, item_id, **values):
        calls.append((user_id, item_id, values))
        started.set()
        assert finish.wait(3)
        return {"status": "updated", "ticker": "TEST", "tad": "2028-02-29", "updated": 3}

    desktop.ops.edit_tad = edit
    desktop.ops.tad_items = lambda user_id: {"items": [], "total": 0, "identified": 0, "month": 0, "unresolved": 0}
    _select_delete_fixture(desktop)
    desktop._edit_tad()
    desktop.tad_entry_fields["tad"].delete(0, "end")
    desktop.tad_entry_fields["tad"].insert(0, "2028-02-29")
    # The row can change behind the open form; save must use its original token.
    desktop.tad_rows["15"]["selection_identity"] = "c" * 64
    desktop._save_tad_entry()
    try:
        assert started.wait(1)
        assert desktop.busy
        assert str(desktop.tad_edit_button.cget("state")) == "disabled"
        assert all(str(button.cget("state")) == "disabled" for button in desktop.tad_entry_controls)
        desktop._save_tad_entry()
        desktop._close_tad_entry()
        assert desktop._tad_entry_window is not None
        responsive = []
        desktop.root.after(0, lambda: responsive.append(True))
        desktop.root.update()
        assert responsive
    finally:
        finish.set()
    _wait_for_desktop(desktop)
    assert calls == [(7, 15, {"expected_identity": "b" * 64, "ticker": "TEST", "tad": "2028-02-29",
                             "source_url": "", "notes": "Reported evidence."})]
    assert desktop._tad_entry_window is None
    assert "Date updated: TEST · 2028-02-29" in desktop.tad_notice.get()


def test_edit_error_preserves_inputs_and_original_snapshot(desktop, monkeypatch):
    errors = []
    monkeypatch.setattr("trialdelta.admin_app.messagebox.showerror", lambda *args, **kwargs: errors.append((args, kwargs)))

    def stale(*args, **kwargs):
        raise ops.TADValidationError("The selected date has changed. Refresh the list.")

    desktop.ops.edit_tad = stale
    desktop.ops.safe_error = ops.safe_error
    _select_delete_fixture(desktop)
    desktop._edit_tad()
    desktop._save_tad_entry()
    _wait_for_desktop(desktop)
    assert errors[0][1]["parent"] is desktop._tad_entry_window
    assert desktop.tad_entry_fields["tad"].get() == "2027-10"
    assert desktop._tad_edit_selection["selection_identity"] == "b" * 64
    assert all(str(button.cget("state")) == "normal" for button in desktop.tad_entry_controls)
    desktop._close_tad_entry()


def test_unresolved_edit_starts_with_blank_date_and_unchanged_save_is_reported(desktop):
    desktop.ops.edit_tad = lambda *args, **kwargs: {"status": "unchanged", "ticker": "TEST", "tad": None}
    desktop.ops.tad_items = lambda user_id: {"items": [], "total": 0, "identified": 0, "month": 0, "unresolved": 0}
    _select_delete_fixture(desktop, unresolved=True)
    desktop._edit_tad()
    assert desktop.tad_entry_fields["tad"].get() == ""
    desktop._save_tad_entry()
    _wait_for_desktop(desktop)
    assert "Date unchanged: TEST · Unidentified" in desktop.tad_notice.get()


def test_edited_details_show_correction_and_original_evidence(desktop):
    _select_delete_fixture(desktop)
    row = desktop.tad_rows["15"]
    row.update(is_edited=True, evidence_method="edited", tad="2027-12", context="Corrected notes.",
               original_tad="2027-10", original_context="Original saved excerpt.",
               edited_at="2026-09-27T10:00:00", original_filing_link="https://example.org/original")
    desktop._show_tad()
    details = desktop.tad_text.get("1.0", "end")
    assert "Edited entry" in details
    assert "Corrected notes." in details
    assert "Original saved date: 2027-10" in details
    assert "Original saved excerpt." in details
    assert "https://example.org/original" in details
