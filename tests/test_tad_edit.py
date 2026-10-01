"""Append-only corrections change displayed dates while retaining all evidence."""

import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select

from trialdelta import tad
from trialdelta.db import Session, TargetActionDate, TargetActionDateEdit


def filing(db, key, *, ticker="ARVN", value="2028-02-29", precision="day", status="identified", notes="Original filing evidence"):
    identity = hashlib.sha256(key.encode()).hexdigest()
    row = TargetActionDate(
        identity=identity, symbol=ticker, form_type="8-K", filing_sha256=identity,
        filing_link="https://example.org/original", tad=value, precision=precision,
        status=status, matched_text=value or "", reason="Saved filing evidence",
        start=0, end=10, context_start=0, context_end=len(notes), context=notes,
    )
    db.add(row)
    db.flush()
    return row.id


def manual(db, *, ticker="ARVN", value="2028-02-29", notes="Operator note"):
    return tad.add_manual_target_action_date(db, ticker=ticker, tad=value, notes=notes, entered_by=1)["id"]


def snapshot(db, item_id=None):
    rows = tad.list_target_action_dates(db, include_identity=True)["items"]
    return next(row for row in rows if item_id is None or row["id"] == item_id)


def edit(db, item_id, *, expected_identity=None, **fields):
    if expected_identity is None:
        expected_identity = snapshot(db, item_id)["selection_identity"]
    return tad.edit_target_action_date(db, item_id, expected_identity=expected_identity, **{
        "ticker": "ARVN", "tad": "2029-03", "source_url": "https://example.org/correction",
        "notes": "Operator correction", "entered_by": 1, **fields,
    })


def originals(db):
    return {
        row.id: {column.name: getattr(row, column.name) for column in TargetActionDate.__table__.columns}
        for row in db.scalars(select(TargetActionDate).order_by(TargetActionDate.id))
    }


def history_count(db):
    return db.scalar(select(func.count()).select_from(TargetActionDateEdit))


@pytest.mark.parametrize("new_date,precision", [("2029-03", "month"), ("2029-03-15", "day")])
@pytest.mark.parametrize("method", ["filing", "manual"])
def test_edit_retains_original_and_displays_corrected_precision(db, new_date, precision, method):
    item_id = filing(db, "source") if method == "filing" else manual(db)
    db.commit()
    original = originals(db)
    prior = snapshot(db, item_id)
    result = edit(db, item_id, ticker=" outside ", tad=new_date)
    assert result == {
        "status": "updated", "id": item_id, "ticker": "OUTSIDE", "tad": new_date, "precision": precision, "updated": 1,
    }
    db.commit()
    with Session() as reopened:
        assert originals(reopened) == original
        row = snapshot(reopened, item_id)
        assert (row["ticker"], row["tad"], row["precision"]) == ("OUTSIDE", new_date, precision)
        assert row["entry_method"] == method
        assert row["evidence_method"] == "edited"
        assert row["is_edited"] is True
        assert row["edited_count"] == 1
        assert row["edited_at"]
        assert row["original_tad"] == prior["tad"]
        assert row["original_context"] == prior["context"]
        assert row["original_filing_link"] == prior["filing_link"]
        assert row["context"] == "Operator correction"
        assert row["filing_link"] == "https://example.org/correction"
        assert row["source_count"] == prior["source_count"]
        assert row["manual_count"] == prior["manual_count"]
        assert row["selection_identity"] != prior["selection_identity"]
        correction = reopened.scalar(select(TargetActionDateEdit))
        assert correction.entered_by == 1
        assert correction.target_identity == original[item_id]["identity"]
        public = tad.list_target_action_dates(reopened)["items"][0]
        assert not {"selection_identity", "entered_by", "revision_token"} & public.keys()


@pytest.mark.parametrize("status", ["unidentifiable", "ambiguous"])
def test_unresolved_edit_corrects_only_selected_match(db, status):
    selected = filing(db, "selected", value=None, precision="unknown", status=status)
    other = filing(db, "other", value=None, precision="unknown", status=status)
    db.commit()
    before = originals(db)
    assert edit(db, selected)["updated"] == 1
    db.commit()
    assert originals(db) == before
    page = tad.list_target_action_dates(db)
    assert (page["total"], page["identified"], page["month"], page["unresolved"]) == (2, 1, 1, 1)
    assert snapshot(db, other)["status"] == status


def test_group_edit_appends_for_every_original_without_replacing_evidence(db):
    selected = filing(db, "first")
    filing(db, "second")
    manual(db)
    different = filing(db, "other date", value="2030-01", precision="month")
    db.commit()
    before = originals(db)
    assert edit(db, selected)["updated"] == 3
    db.commit()
    assert originals(db) == before
    assert history_count(db) == 3
    row = snapshot(db, selected)
    assert (row["source_count"], row["manual_count"], row["occurrence_count"], row["edited_count"]) == (2, 1, 3, 3)
    assert snapshot(db, different)["is_edited"] is False


def test_repeated_edits_append_history_and_guard_prior_revision(db):
    selected = filing(db, "source")
    db.commit()
    original = originals(db)
    first_token = snapshot(db, selected)["selection_identity"]
    edit(db, selected)
    db.commit()
    second_token = snapshot(db, selected)["selection_identity"]
    edit(db, selected, tad="2030-04-15")
    db.commit()
    third_token = snapshot(db, selected)["selection_identity"]
    assert len({first_token, second_token, third_token}) == 3
    assert history_count(db) == 2
    assert originals(db) == original
    for token in (first_token, second_token):
        with pytest.raises(tad.TADValidationError, match="Refresh the list"):
            edit(db, selected, expected_identity=token)
        with pytest.raises(tad.TADValidationError, match="Refresh the list"):
            tad.delete_target_action_date(db, selected, expected_identity=token)
    assert history_count(db) == 2


@pytest.mark.parametrize("kind", ["filing", "manual", "group", "unresolved", "edited", "long metadata"])
def test_unchanged_display_fields_preserve_all_records_and_history(db, kind):
    if kind == "manual":
        selected = manual(db)
    else:
        selected = filing(db, "selected", value=None if kind == "unresolved" else "2028-02-29",
                          precision="unknown" if kind == "unresolved" else "day",
                          status="unidentifiable" if kind == "unresolved" else "identified",
                          notes="x" * 5000 if kind == "long metadata" else "Original filing evidence")
    if kind == "group":
        filing(db, "grouped")
    if kind == "long metadata":
        db.get(TargetActionDate, selected).symbol = "A" * 40
        db.get(TargetActionDate, selected).filing_link = "not a valid URL"
    db.commit()
    if kind == "edited":
        edit(db, selected)
        db.commit()
    before = originals(db)
    before_history = history_count(db)
    row = snapshot(db, selected)
    result = edit(db, selected, ticker=" " + row["ticker"].lower() + " ", tad=row["tad"] or "",
                  source_url=row["filing_link"] or "", notes=" " + row["context"] + " ")
    assert result == {"status": "unchanged", "id": selected, "ticker": row["ticker"],
                      "tad": row["tad"], "precision": row["precision"], "updated": 0}
    db.commit()
    assert originals(db) == before
    assert history_count(db) == before_history


@pytest.mark.parametrize("bad_fields", [
    {"tad": "2029-02-29"}, {"tad": "2029-13"}, {"tad": ""}, {"tad": None},
    {"ticker": "A B"}, {"ticker": ""}, {"source_url": "javascript:alert(1)"},
    {"source_url": "https://user:secret@example.org/"}, {"notes": "x" * 4001}, {"notes": None},
    {"entered_by": None}, {"entered_by": True},
])
def test_changed_invalid_fields_do_not_touch_originals_or_history(db, bad_fields):
    selected = filing(db, "retained")
    db.commit()
    before = originals(db)
    with pytest.raises(tad.TADValidationError):
        edit(db, selected, **bad_fields)
    db.commit()
    assert originals(db) == before
    assert history_count(db) == 0


def test_edit_never_reads_source_filings(db, monkeypatch):
    selected = filing(db, "source")
    db.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("Manual correction cannot read or alter source filings")

    monkeypatch.setattr(tad, "_source_engine", forbidden)
    monkeypatch.setattr(tad, "extract_target_action_dates", forbidden)
    assert edit(db, selected)["updated"] == 1


def test_caller_rollback_removes_entire_new_history(db):
    selected = filing(db, "first")
    filing(db, "second")
    db.commit()
    before = originals(db)
    assert edit(db, selected)["updated"] == 2
    db.rollback()
    with Session() as reopened:
        assert history_count(reopened) == 0
        assert originals(reopened) == before
        assert snapshot(reopened, selected)["tad"] == "2028-02-29"


def test_partial_append_failure_rolls_back_corrections_even_if_caller_commits(db):
    selected = filing(db, "first")
    filing(db, "second")
    db.commit()
    before = originals(db)
    calls = 0

    def fail_second(mapper, connection, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Simulated append failure")

    event.listen(TargetActionDateEdit, "before_insert", fail_second)
    try:
        with pytest.raises(RuntimeError, match="Simulated append"):
            edit(db, selected)
    finally:
        event.remove(TargetActionDateEdit, "before_insert", fail_second)
    db.commit()
    assert history_count(db) == 0
    assert originals(db) == before


def test_effective_values_drive_filtering_pagination_counts_and_company_name(db):
    selected = filing(db, "old", ticker="OUTSIDE", value=None, precision="unknown", status="unidentifiable")
    filing(db, "middle", ticker="MIDDLE", value="2029-01", precision="month")
    filing(db, "late", ticker="LATE", value="2031-01-01")
    db.commit()
    edit(db, selected, ticker="ARVN", tad="2030-12")
    db.commit()
    first = tad.list_target_action_dates(db, limit=1)
    second = tad.list_target_action_dates(db, limit=1, offset=1)
    assert first["items"][0]["ticker"] == "LATE"
    assert second["items"][0]["ticker"] == "ARVN"
    assert second["items"][0]["company"] == "Arvinas"
    assert (first["total"], first["identified"], first["month"], first["unresolved"]) == (3, 3, 2, 0)
    assert tad.list_target_action_dates(db, ticker="OUTSIDE")["total"] == 0
    assert tad.list_target_action_dates(db, ticker=" arvn ", status="identified")["total"] == 1
    assert tad.list_target_action_dates(db, status="unresolved")["items"] == []


def test_edit_merges_effective_duplicate_groups_preserving_both_originals(db):
    existing = manual(db, value="2029-03", notes="Operator correction")
    selected = filing(db, "selected")
    db.commit()
    before = originals(db)
    assert edit(db, selected, source_url="")["updated"] == 1
    db.commit()
    assert originals(db) == before
    page = tad.list_target_action_dates(db)
    assert page["total"] == 1
    assert page["items"][0]["id"] == existing
    assert page["items"][0]["occurrence_count"] == 2
    assert page["items"][0]["edited_count"] == 1
    # Correcting the merged displayed row reaches both current originals.
    assert edit(db, existing, tad="2030-05")["updated"] == 2
    db.commit()
    assert {row["tad"] for row in tad.list_target_action_dates(db)["items"]} == {"2030-05"}
    assert history_count(db) == 3
    assert originals(db) == before


def test_group_selection_excludes_records_previously_corrected_elsewhere(db):
    first = filing(db, "first")
    db.commit()
    edit(db, first, ticker="OTHER", tad="2030-01")
    db.commit()
    second = filing(db, "second")  # Same original group, currently a different displayed group.
    db.commit()
    assert edit(db, second, tad="2031-01")["updated"] == 1
    db.commit()
    assert snapshot(db, first)["tad"] == "2030-01"
    assert snapshot(db, second)["tad"] == "2031-01"


def test_import_idempotence_keeps_corrections_and_original_identity(db):
    selected = filing(db, "source")
    db.commit()
    original = originals(db)[selected]
    edit(db, selected)
    db.commit()
    assert tad._save_result(db, {key: value for key, value in original.items() if key != "id"}) is False
    db.commit()
    assert snapshot(db, selected)["tad"] == "2029-03"
    assert history_count(db) == 1
    assert originals(db)[selected] == original


def test_repeated_add_reports_effective_date_of_corrected_manual_entry(db):
    selected = manual(db)
    db.commit()
    edit(db, selected, ticker="OTHER", tad="2030-04")
    db.commit()
    result = tad.add_manual_target_action_date(db, ticker="ARVN", tad="2028-02-29", notes="Operator note", entered_by=1)
    assert result == {"status": "existing", "id": selected, "ticker": "OTHER", "tad": "2030-04", "precision": "month", "is_edited": True}
    assert history_count(db) == 1
    assert len(originals(db)) == 1


def test_explicit_delete_uses_effective_group_retains_history_and_reimport_restores_correction(db):
    selected = filing(db, "source")
    db.commit()
    saved = originals(db)[selected]
    edit(db, selected)
    db.commit()
    corrected = snapshot(db, selected)
    # Another original with the old date is not a member of the corrected group.
    retained = filing(db, "unrelated old date")
    db.commit()
    deleted = tad.delete_target_action_date(db, selected, expected_identity=corrected["selection_identity"])
    assert (deleted["tad"], deleted["deleted"]) == ("2029-03", 1)
    db.commit()
    assert set(originals(db)) == {retained}
    assert history_count(db) == 1
    assert tad._save_result(db, {key: value for key, value in saved.items() if key != "id"}) is True
    db.commit()
    restored = next(row for row in tad.list_target_action_dates(db)["items"] if row["is_edited"])
    assert restored["tad"] == "2029-03"
    assert history_count(db) == 1


def test_stale_reused_id_cannot_append_correction_to_another_original(db):
    selected = filing(db, "old")
    db.commit()
    prior = snapshot(db, selected)
    tad.delete_target_action_date(db, selected, expected_identity=prior["selection_identity"])
    db.commit()
    replacement = filing(db, "new", ticker="OTHER")
    db.commit()
    assert replacement == selected
    with pytest.raises(tad.TADValidationError, match="Refresh the list"):
        edit(db, selected, expected_identity=prior["selection_identity"])
    assert history_count(db) == 0
    assert snapshot(db, replacement)["ticker"] == "OTHER"


def test_edit_refuses_non_trialdelta_mysql_schema(db, monkeypatch):
    selected = filing(db, "source")
    db.commit()
    prior = snapshot(db, selected)
    binding = SimpleNamespace(dialect=SimpleNamespace(name="mysql"), url=SimpleNamespace(database="filings"))
    monkeypatch.setattr(db, "get_bind", lambda *args, **kwargs: binding)
    with pytest.raises(RuntimeError, match="trialdelta schema"):
        edit(db, selected, expected_identity=prior["selection_identity"])
    with Session() as reopened:
        assert history_count(reopened) == 0


def test_revision_token_is_deterministic_from_previous_revision_and_values(db):
    import json

    selected = filing(db, "source")
    db.commit()
    prior = snapshot(db, selected)
    original_identity = db.get(TargetActionDate, selected).identity
    edit(db, selected)
    expected = hashlib.sha256(json.dumps(
        [original_identity, prior["selection_identity"], "ARVN", "2029-03", "https://example.org/correction", "Operator correction", 1],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    assert snapshot(db, selected)["selection_identity"] == expected
