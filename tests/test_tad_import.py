"""Saved filing imports are deterministic, read-only at source, and repeatable."""

import hashlib
import socket
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import DateTime, bindparam, create_engine, event, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from trialdelta import tad
from trialdelta.db import Session, TargetActionDate, TargetActionDateEdit


@pytest.fixture
def saved_filings(monkeypatch):
    source = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    with source.begin() as connection:
        connection.execute(text("ATTACH DATABASE ':memory:' AS filings"))
        connection.execute(text(
            "CREATE TABLE filings.biotech_filings (symbol TEXT, form_type TEXT, filing TEXT, "
            "accession_no TEXT, filed_at DATETIME, filing_link TEXT)"
        ))
    statements, stream_options = [], []

    def track(connection, cursor, statement, parameters, context, many):
        statements.append(statement)
        stream_options.append(context.execution_options)

    event.listen(source, "before_cursor_execute", track)
    monkeypatch.setattr(tad, "engine", source)
    monkeypatch.setattr(tad, "_source_engine", lambda: source)

    def populate(rows):
        with source.begin() as connection:
            connection.execute(text(
                "INSERT INTO filings.biotech_filings "
                "VALUES (:symbol, :form_type, :filing, :accession_no, :filed_at, :filing_link)"
            ).bindparams(bindparam("filed_at", type_=DateTime())), rows)
        statements.clear()
        stream_options.clear()

    yield source, populate, statements, stream_options
    source.dispose()


def filing(symbol, markup, form_type="8-K", *, accession_no=None, filed_at=None, filing_link=None):
    return {
        "symbol": symbol, "form_type": form_type, "filing": markup,
        "accession_no": accession_no, "filed_at": filed_at, "filing_link": filing_link,
    }


def test_import_preserves_months_unknowns_and_outside_coverage_without_network(
    client, db, saved_filings, monkeypatch,
):
    source, populate, statements, stream_options = saved_filings
    rows = [
        filing("ARVN", "<p>Our target <b>action</b> date is October 15, 2027.</p>"),
        filing("OUTSIDE", "<p>The target action date is November 2027.</p>", "10-Q"),
        filing("UNKNOWN", "<p>The target action date has not been determined.</p>"),
        filing("NOMARK", "<p>Other company information: November 20, 2027.</p>"),
    ]
    populate(rows)

    def no_network(*args, **kwargs):
        raise AssertionError("No network API belongs in deterministic date recognition")

    monkeypatch.setattr(socket, "create_connection", no_network)
    result = tad.import_target_action_dates()
    assert result == {
        "status": "complete", "scanned": 4, "matched": 3, "identified": 2,
        "month": 1, "unresolved": 1, "imported": 3, "existing": 0,
    }
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert stream_options[0]["yield_per"] == 1
    assert stream_options[0]["stream_results"] is True
    page = tad.list_target_action_dates(db)
    assert {key: page[key] for key in ("total", "identified", "month", "unresolved")} == {
        "total": 3, "identified": 2, "month": 1, "unresolved": 1,
    }
    exact, month, unknown = sorted(page["items"], key=lambda item: item["symbol"])
    assert exact["company"] == "Arvinas"
    assert exact["tad"] == "2027-10-15"
    assert exact["precision"] == "day"
    assert exact["filing_sha256"] == hashlib.sha256(rows[0]["filing"].encode()).hexdigest()
    assert exact["start"] >= exact["context_start"]
    assert exact["end"] <= exact["context_end"]
    assert "target action date" in exact["context"]
    assert exact["imported_at"]
    assert not any(key in exact for key in ("filing_date", "published_at", "url"))
    assert exact["accession_no"] is exact["filed_at"] is exact["filing_link"] is None
    assert month["tad"] == "2027-11"
    assert month["precision"] == "month"
    assert month["company"] == "OUTSIDE"
    assert unknown["tad"] is None
    assert unknown["status"] == "unidentifiable"
    assert unknown["reason"]
    with source.connect() as connection:
        retained = connection.execute(text(
            "SELECT symbol, form_type, filing, accession_no, filed_at, filing_link "
            "FROM filings.biotech_filings"
        )).mappings().all()
    assert [dict(row) for row in retained] == rows


def test_source_metadata_and_native_datetime_are_preserved(client, db, saved_filings):
    _, populate, _, _ = saved_filings
    filed_at = datetime(2026, 9, 26, 10, 11, 12)
    filing_link = "https://www.sec.gov/Archives/edgar/data/12345/filing.htm"
    populate([filing(
        "OUTSIDE", "The target action date is November 2027.",
        accession_no="0000012345-26-000001", filed_at=filed_at, filing_link=filing_link,
    )])
    assert tad.import_target_action_dates()["imported"] == 1
    row = tad.list_target_action_dates(db)["items"][0]
    assert row["accession_no"] == "0000012345-26-000001"
    assert row["filed_at"] == "2026-09-26T10:11:12"
    assert row["filing_link"] == filing_link
    assert row["tad"] == "2027-11"
    assert row["imported_at"] != row["filed_at"]


@pytest.mark.parametrize("expression,expected", [("November 14, 2026", "2026-11-14"), ("November 2026", "2026-11")])
def test_sentence_fallback_and_full_evidence_survive_import(client, db, saved_filings, expression, expected):
    _, populate, statements, _ = saved_filings
    sentence = "The target action date for " + "a long application description " * 6 + f"is {expression}."
    markup = "<p>Prior announcement.</p><p>" + sentence + "</p><p>Other information.</p>"
    populate([filing("ARVN", markup)])
    summary = tad.import_target_action_dates()
    assert summary["identified"] == summary["imported"] == 1
    assert summary["unresolved"] == 0
    row = tad.list_target_action_dates(db)["items"][0]
    assert row["tad"] == expected
    assert row["context"] == sentence
    assert row["context_end"] - row["context_start"] == len(sentence)
    assert row["matched_text"] == expression
    assert "Same-sentence fallback" in row["reason"]
    assert tad.import_target_action_dates()["existing"] == 1
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)


def test_accessions_distinguish_identical_html_and_count_snapshots_once(client, db, saved_filings):
    _, populate, _, _ = saved_filings
    markup = "The target action date is October 15, 2027."
    first = filing("ARVN", markup, accession_no="0000012345-26-000001")
    populate([
        first, first,
        filing("ARVN", markup, accession_no="0000012345-26-000002"),
        filing("ARVN", markup + " Additional source text.", accession_no="0000012345-26-000001"),
        filing("ARVN", markup),
    ])
    summary = tad.import_target_action_dates()
    assert summary["imported"] == 4
    assert summary["existing"] == 1
    assert tad.import_target_action_dates()["imported"] == 0
    row = tad.list_target_action_dates(db)["items"][0]
    assert row["source_count"] == 3  # Two accession numbers and one hash fallback.
    assert row["occurrence_count"] == 4  # Changed HTML retains another evidence snapshot.


def test_filing_timestamp_does_not_fill_a_missing_target_year(client, db, saved_filings):
    _, populate, _, _ = saved_filings
    populate([filing(
        "ARVN", "The target action date is October 15.", filed_at=datetime(2027, 1, 1),
    )])
    assert tad.import_target_action_dates()["imported"] == 1
    row = tad.list_target_action_dates(db)["items"][0]
    assert row["tad"] is None
    assert row["status"] == "unidentifiable"
    assert row["filed_at"] == "2027-01-01T00:00:00"


def test_repeated_and_duplicate_source_rows_are_idempotent(client, db, saved_filings):
    _, populate, _, _ = saved_filings
    markup = "<p>The target action date is October 15, 2027.</p>"
    populate([
        filing("ARVN", markup), filing("ARVN", markup),
        filing("ARVN", markup + "<p>Another filing reports the same deadline.</p>"),
    ])
    first = tad.import_target_action_dates()
    second = tad.import_target_action_dates()
    assert first["imported"] == 2
    assert first["existing"] == 1
    assert second["status"] == "complete"
    assert second["imported"] == 0
    assert second["existing"] == 3
    assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 2
    page = tad.list_target_action_dates(db)
    assert page["total"] == 1
    assert page["items"][0]["source_count"] == 2
    assert page["items"][0]["occurrence_count"] == 2


def test_single_filing_and_full_import_share_identity_and_metadata(db, saved_filings):
    _, populate, _, _ = saved_filings
    row = filing(
        " arvn ", "<p>The target action date is October 15, 2027.</p>", " 8-k ",
        accession_no=" 0000012345-26-000001 ", filed_at=datetime(2026, 9, 27, 12, 30),
        filing_link="https://www.sec.gov/Archives/filing.htm",
    )
    populate([row])
    counts = tad.save_filing_target_action_dates(db, row)
    assert counts == {
        "matched": 1, "identified": 1, "month": 0,
        "unresolved": 0, "imported": 1, "existing": 0,
    }
    db.commit()
    original = db.scalar(select(TargetActionDate))
    original_id, identity = original.id, original.identity
    assert (original.symbol, original.form_type, original.accession_no) == (
        "ARVN", "8-K", "0000012345-26-000001",
    )
    assert original.filed_at == "2026-09-27T12:30:00"
    assert original.filing_link == row["filing_link"]
    # A durable job may serialize datetime metadata without changing identity.
    counts = tad.save_filing_target_action_dates(db, {**row, "filed_at": "2026-09-27 12:30:00"})
    assert counts["existing"] == 1
    assert counts["imported"] == 0
    db.commit()
    counts = tad.import_target_action_dates()
    assert (counts["scanned"], counts["existing"], counts["imported"]) == (1, 1, 0)
    assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 1
    retained = db.scalar(select(TargetActionDate))
    assert (retained.id, retained.identity) == (original_id, identity)


@pytest.mark.parametrize("expression,tad_value,precision,status", [
    ("November 2027", "2027-11", "month", "identified"),
    ("November 14", None, "unknown", "unidentifiable"),
    ("05/06/2027", None, "unknown", "ambiguous"),
])
def test_single_filing_keeps_month_and_unresolved_evidence(db, expression, tad_value, precision, status):
    row = filing("ARVN", f"The target action date is {expression}.", filed_at="2026-09-27T12:30:00")
    counts = tad.save_filing_target_action_dates(db, row)
    db.commit()
    assert counts["matched"] == counts["imported"] == 1
    assert counts["month"] == int(precision == "month")
    assert counts["unresolved"] == int(status != "identified")
    result = tad.list_target_action_dates(db)["items"][0]
    assert (result["tad"], result["precision"], result["status"]) == (tad_value, precision, status)
    assert expression in result["context"]
    assert result["reason"]


@pytest.mark.parametrize("expression", ["November 14", "05/06/2027"])
def test_unresolved_single_filing_can_be_corrected_and_survives_both_import_paths(db, saved_filings, expression):
    _, populate, _, _ = saved_filings
    row = filing("ARVN", f"The target action date is {expression}.")
    populate([row])
    tad.save_filing_target_action_dates(db, row)
    db.commit()
    original = tad.list_target_action_dates(db, include_identity=True)["items"][0]
    assert original["status"] != "identified"
    correction = tad.edit_target_action_date(
        db, original["id"], expected_identity=original["selection_identity"], ticker="ARVN",
        tad="2027-11-14", source_url="https://example.org/clarification", notes="Date confirmed manually",
        entered_by=1,
    )
    assert correction["updated"] == 1
    db.commit()
    assert tad.save_filing_target_action_dates(db, row)["existing"] == 1
    db.commit()
    assert tad.import_target_action_dates()["existing"] == 1
    result = tad.list_target_action_dates(db)["items"][0]
    assert (result["tad"], result["status"], result["is_edited"]) == ("2027-11-14", "identified", True)
    assert result["original_tad"] is None
    assert result["original_context"] == original["context"]
    assert result["context"] == "Date confirmed manually"
    assert db.scalar(select(func.count()).select_from(TargetActionDateEdit)) == 1


def test_single_filing_without_marker_leaves_manual_entry_available(db):
    assert tad.save_filing_target_action_dates(db, filing("ARVN", "PDUFA date is November 14, 2027.")) == {
        "matched": 0, "identified": 0, "month": 0,
        "unresolved": 0, "imported": 0, "existing": 0,
    }
    assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 0
    tad.add_manual_target_action_date(db, ticker="ARVN", tad="2027-11-14", notes="Entered after filing review", entered_by=1)
    db.commit()
    row = tad.list_target_action_dates(db)["items"][0]
    assert (row["tad"], row["entry_method"]) == ("2027-11-14", "manual")


def test_single_filing_caller_rollback_retains_prior_results(db):
    tad.save_filing_target_action_dates(db, filing("ARVN", "The target action date is October 15, 2027."))
    db.commit()
    assert tad.save_filing_target_action_dates(db, filing("OUTSIDE", "The target action date is November 2027."))["imported"] == 1
    db.rollback()
    with Session() as reopened:
        assert list(reopened.scalars(select(TargetActionDate.symbol))) == ["ARVN"]


@pytest.mark.parametrize("database", ["filings", "other", None])
def test_single_filing_rejects_wrong_mysql_destination_before_processing(database, monkeypatch):
    fake_db = SimpleNamespace(get_bind=lambda: SimpleNamespace(
        dialect=SimpleNamespace(name="mysql"), url=SimpleNamespace(database=database),
    ))

    def forbidden(*args, **kwargs):
        pytest.fail("An incorrect destination must be rejected before extraction")

    monkeypatch.setattr(tad, "extract_target_action_dates", forbidden)
    with pytest.raises(RuntimeError, match="require the trialdelta schema"):
        tad.save_filing_target_action_dates(fake_db, filing("ARVN", "The target action date is November 2027."))


@pytest.mark.parametrize("filed_at", ["not a timestamp", "2026-09-31T12:30:00", 123])
def test_single_filing_rejects_invalid_serialized_timestamps(db, filed_at):
    with pytest.raises(ValueError):
        tad.save_filing_target_action_dates(db, filing("ARVN", "The target action date is November 2027.", filed_at=filed_at))
    assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 0


def test_partial_single_filing_failure_keeps_scan_counts_and_caller_can_rollback(db, saved_filings, monkeypatch):
    _, populate, _, _ = saved_filings
    row = filing("ARVN", "The target action dates are October 15, 2027 and November 20, 2027.")
    populate([row])
    original = tad._save_result
    calls = 0

    def fail_second(session, values):
        nonlocal calls
        calls += 1
        if calls % 2 == 0:
            raise RuntimeError("Simulated persistence failure")
        return original(session, values)

    monkeypatch.setattr(tad, "_save_result", fail_second)
    with pytest.raises(tad.FilingTargetActionDateError) as caught:
        tad.save_filing_target_action_dates(db, row)
    assert caught.value.counts == {
        "matched": 2, "identified": 2, "month": 0,
        "unresolved": 0, "imported": 1, "existing": 0,
    }
    db.rollback()
    assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 0
    db.rollback()
    result = tad.import_target_action_dates()
    assert result == {
        "status": "error", "scanned": 1, "matched": 2, "identified": 2,
        "month": 0, "unresolved": 0, "imported": 0, "existing": 0,
        "detail": "Target action date import failed. Saved results were retained; check database/source availability and configuration.",
    }
    assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 0


def test_repeated_mentions_keep_evidence_but_calendar_groups_date(client, db, saved_filings):
    _, populate, _, _ = saved_filings
    marker = "The target action date is October 15, 2027."
    populate([filing("ARVN", f"<p>{marker}</p>" + "<p>Other information.</p>" * 20 + f"<p>{marker}</p>")])
    assert tad.import_target_action_dates()["imported"] == 2
    page = tad.list_target_action_dates(db)
    assert page["total"] == 1
    assert page["items"][0]["source_count"] == 1
    assert page["items"][0]["occurrence_count"] == 2
    saved = db.scalars(select(TargetActionDate).order_by(TargetActionDate.start)).all()
    assert saved[0].start != saved[1].start


def test_unique_key_race_uses_savepoint_without_losing_other_results(
    client, db, saved_filings, monkeypatch,
):
    _, populate, _, _ = saved_filings
    populate([filing("ARVN", "The target action date is October 15, 2027.")])
    assert tad.import_target_action_dates()["imported"] == 1
    existing = db.scalar(select(TargetActionDate))
    values = {
        key: getattr(existing, key)
        for key in (*tad.RESULT_FIELDS, "identity", "symbol", "form_type", "filing_sha256")
    }
    original_scalar = db.scalar
    calls = 0

    def miss_initial_lookup(query, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None  # Simulate a record appearing after the first lookup.
        return original_scalar(query, *args, **kwargs)

    monkeypatch.setattr(db, "scalar", miss_initial_lookup)
    assert tad._save_result(db, values) is False
    assert tad._save_result(db, {**values, "symbol": "OTHER", "identity": "f" * 64}) is True
    db.commit()
    assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 2


def test_import_failure_rolls_back_this_run_preserving_saved_data(
    client, db, saved_filings, monkeypatch,
):
    _, populate, _, _ = saved_filings
    populate([filing("ARVN", "The target action date is October 15, 2027.")])
    assert tad.import_target_action_dates()["imported"] == 1
    populate([
        filing("OUTSIDE", "The target action date is November 2027."),
        filing("BROKEN", "Source causing a simulated parser failure"),
    ])
    original = tad.extract_target_action_dates

    def fail_later(markup):
        if "simulated" in markup:
            raise ValueError("private source text and secret password")
        return original(markup)

    monkeypatch.setattr(tad, "extract_target_action_dates", fail_later)
    result = tad.import_target_action_dates()
    assert result["status"] == "error"
    assert result["imported"] == 0
    assert "secret" not in str(result)
    assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 1
    assert db.scalar(select(TargetActionDate.symbol)) == "ARVN"


def test_empty_source_and_unconfigured_import_are_safe(client, db, saved_filings, monkeypatch):
    result = tad.import_target_action_dates()
    assert result["status"] == "complete"
    assert result["scanned"] == result["imported"] == 0
    monkeypatch.setattr(tad, "_source_engine", lambda: None)
    assert tad.import_target_action_dates()["status"] == "unconfigured"
    assert tad.list_target_action_dates(db)["items"] == []


def test_source_connection_failure_is_sanitized(client, monkeypatch):
    def unavailable():
        raise OperationalError("mysql://user:secret@private-host", {}, Exception("private details"))

    monkeypatch.setattr(tad, "_source_engine", unavailable)
    result = tad.import_target_action_dates()
    assert result["status"] == "error"
    assert result["imported"] == 0
    assert "secret" not in str(result)


def test_read_filters_pagination_and_order_are_bounded(client, db, saved_filings):
    _, populate, statements, _ = saved_filings
    populate([
        filing("ARVN", "The target action date is October 15, 2027."),
        filing("OUTSIDE", "The target action date is November 2027."),
        filing("ARVN", "The target action date is not known."),
    ])
    tad.import_target_action_dates()
    statements.clear()
    page = tad.list_target_action_dates(db, limit=2)
    assert page["total"] == 3
    assert [row["tad"] for row in page["items"]] == ["2027-11", "2027-10-15"]
    last = tad.list_target_action_dates(db, limit=2, offset=2)
    assert len(last["items"]) == 1
    assert last["items"][0]["status"] == "unidentifiable"
    filtered = tad.list_target_action_dates(db, ticker=" arvn ", status="identified")
    assert filtered["total"] == 1
    assert filtered["items"][0]["ticker"] == "ARVN"
    unresolved = tad.list_target_action_dates(db, status="unresolved")
    assert unresolved["total"] == unresolved["unresolved"] == 1
    assert tad.list_target_action_dates(db, limit=100000)["limit"] == 500
    assert tad.list_target_action_dates(db, offset=-2)["offset"] == 0
    assert not statements  # Calendar reads never touch the filing source.
    with pytest.raises(ValueError):
        tad.list_target_action_dates(db, status="invented")


@pytest.mark.parametrize("status,exit_code", [("complete", 0), ("error", 1), ("unconfigured", 1)])
def test_cli_exit_status_is_useful(monkeypatch, capsys, status, exit_code):
    monkeypatch.setattr(tad, "import_target_action_dates", lambda: {"status": status})
    assert tad.main() == exit_code
    assert status in capsys.readouterr().out
