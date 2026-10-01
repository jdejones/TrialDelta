"""Target-date headings sort displayed results without changing their evidence."""

import gc
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
        app.metrics.clear()
        for name in tuple(vars(app)):
            if isinstance(getattr(app, name), tkinter.Variable):
                setattr(app, name, None)
        root.destroy()
        del app, root
        gc.collect()


@pytest.fixture
def rows():
    common = {
        "reason": "Reported target action date",
        "context": "Evidence from the respective filing.",
        "imported_at": "2026-09-27T10:00:00",
        "filing_sha256": "a" * 64,
    }
    return [
        dict(common, id=101, ticker="zulu", tad="2027-01-02", precision="day",
             status="identified", form_type="8-K", accession_no="0000320193-26-000010",
             source_count=2, manual_count=8),
        dict(common, id=102, ticker="alpha", tad="2026-12-31", precision="day",
             status="identified", form_type="10-K", accession_no="0000320193-26-000002",
             source_count=2),
        dict(common, id=103, ticker="Bravo", tad="2026-12", precision="month",
             status="identified", form_type="6-K", accession_no="0000320193-25-000100",
             source_count=9),
        dict(common, id=104, ticker="delta", tad=None, precision="unknown",
             status="unidentifiable", form_type="", accession_no=None,
             entry_method="manual", source_count=0, manual_count=1),
    ]


def show(app, rows):
    app._show_tads({
        "items": rows,
        "total": len(rows),
        "identified": sum(row["status"] == "identified" for row in rows),
        "month": sum(row["precision"] == "month" for row in rows),
        "unresolved": sum(row["status"] != "identified" for row in rows),
    })


def click_heading(app, column):
    command = app.tads.heading(column, "command")
    assert command, f"{column} has no clickable heading"
    app.tads.tk.call(command)


def assert_order(app, ids):
    assert app.tads.get_children() == tuple(str(item_id) for item_id in ids)


def assert_indicator(app, active, arrow):
    for column in app.tads["columns"]:
        label = app.tads.heading(column, "text")
        if column == active:
            assert label.endswith(arrow)
        else:
            assert "▲" not in label and "▼" not in label


@pytest.mark.parametrize("column,ascending,descending", [
    ("ticker", [102, 103, 104, 101], [101, 104, 103, 102]),
    ("tad", [103, 102, 101, 104], [101, 102, 103, 104]),
    ("precision", [101, 102, 103, 104], [104, 103, 101, 102]),
    ("status", [101, 102, 103, 104], [104, 101, 102, 103]),
    ("form_type", [102, 103, 101, 104], [104, 101, 103, 102]),
    ("accession_no", [103, 102, 101, 104], [101, 102, 103, 104]),
    ("sources", [104, 102, 103, 101], [101, 103, 102, 104]),
])
def test_each_heading_sorts_values_and_only_marks_active_column(desktop, rows, column, ascending, descending):
    show(desktop, rows)
    assert_order(desktop, [101, 102, 103, 104])
    assert_indicator(desktop, None, "")

    click_heading(desktop, column)
    assert_order(desktop, ascending)
    assert_indicator(desktop, column, "▲")
    click_heading(desktop, column)
    assert_order(desktop, descending)
    assert_indicator(desktop, column, "▼")

    next_column = "tad" if column == "ticker" else "ticker"
    click_heading(desktop, next_column)
    assert_order(desktop, [103, 102, 101, 104] if next_column == "tad" else [102, 103, 104, 101])
    assert_indicator(desktop, next_column, "▲")


def test_accession_column_keeps_exact_identifier_and_missing_values_last(desktop, rows):
    rows.append(dict(rows[-1], id=105, accession_no=""))
    show(desktop, rows)
    columns = tuple(desktop.tads["columns"])
    assert columns.index("accession_no") == columns.index("form_type") + 1
    for row in rows:
        assert desktop.tads.set(str(row["id"]), "accession_no") == (row["accession_no"] or "—")
    click_heading(desktop, "accession_no")
    assert_order(desktop, [103, 102, 101, 104, 105])
    click_heading(desktop, "accession_no")
    assert_order(desktop, [101, 102, 103, 104, 105])


def test_sort_preserves_selected_row_focus_evidence_and_edit_target(desktop, rows):
    show(desktop, rows)
    desktop.tads.selection_set("101")
    desktop.tads.focus("101")
    desktop._show_tad()
    evidence = desktop.tad_text.get("1.0", "end")
    original_rows = dict(desktop.tad_rows)

    for column in ("ticker", "sources", "sources"):
        click_heading(desktop, column)
        assert desktop.tads.selection() == ("101",)
        assert desktop.tads.focus() == "101"
        assert desktop.tad_rows == original_rows
        assert desktop.tad_text.get("1.0", "end") == evidence
        assert str(desktop.tad_edit_button["state"]) == "normal"
        assert str(desktop.tad_delete_button["state"]) == "normal"

    desktop._edit_tad()
    assert desktop.tad_entry_fields["ticker"].get() == "zulu"
    assert desktop.tad_entry_fields["tad"].get() == "2027-01-02"
    desktop._close_tad_entry()


def test_sort_persists_across_reload_filter_and_empty_results(desktop, rows):
    show(desktop, rows)
    click_heading(desktop, "ticker")
    click_heading(desktop, "ticker")
    show(desktop, list(reversed(rows)))
    assert_order(desktop, [101, 104, 103, 102])
    assert_indicator(desktop, "ticker", "▼")

    desktop.tad_view.set("Unresolved")
    unresolved = [dict(row, tad=None, precision="unknown", status="unidentifiable") for row in rows[1:]]
    show(desktop, unresolved)
    assert_order(desktop, [104, 103, 102])
    assert_indicator(desktop, "ticker", "▼")

    show(desktop, [])
    click_heading(desktop, "sources")
    click_heading(desktop, "sources")
    assert_order(desktop, [])
    assert_indicator(desktop, "sources", "▼")
    desktop.tad_view.set("All saved")
    show(desktop, rows)
    assert_order(desktop, [101, 103, 102, 104])
    assert_indicator(desktop, "sources", "▼")
