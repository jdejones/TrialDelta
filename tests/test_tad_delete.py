"""Delete the selected saved calendar row while retaining source filings."""

import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

from trialdelta import tad
from trialdelta.db import Session, TargetActionDate


def filing(db, key, *, ticker="ARVN", value="2028-02-29", precision="day", status="identified"):
    identity = hashlib.sha256(key.encode()).hexdigest()
    row = TargetActionDate(
        identity=identity, symbol=ticker, form_type="8-K", filing_sha256=identity,
        tad=value, precision=precision, status=status, matched_text=value or "",
        reason="Saved filing evidence", start=0, end=10, context_start=0,
        context_end=44, context="The target action date is February 29, 2028.",
    )
    db.add(row)
    db.flush()
    return row.id


def manual(db, *, ticker="ARVN", value="2028-02-29", notes=""):
    return tad.add_manual_target_action_date(
        db, ticker=ticker, tad=value, notes=notes, entered_by=1,
    )["id"]


def ids(db):
    return set(db.scalars(select(TargetActionDate.id)))


def delete_saved(db, item_id, *, expected_identity=None):
    if expected_identity is None and isinstance(item_id, int) and not isinstance(item_id, bool) and item_id > 0:
        expected_identity = db.scalar(select(TargetActionDate.identity).where(TargetActionDate.id == item_id))
    return tad.delete_target_action_date(db, item_id, expected_identity=expected_identity or "a" * 64)


@pytest.mark.parametrize("value,precision", [("2028-02-29", "day"), ("2028-02", "month")])
@pytest.mark.parametrize("entry_method", ["manual", "filing"])
def test_delete_exact_or_month_date_with_either_provenance(db, value, precision, entry_method):
    item_id = manual(db, value=value) if entry_method == "manual" else filing(db, "source", value=value, precision=precision)
    db.commit()
    assert delete_saved(db, item_id) == {
        "status": "deleted", "ticker": "ARVN", "tad": value, "precision": precision, "deleted": 1,
    }
    assert tad.list_target_action_dates(db)["items"] == []
    db.commit()
    with Session() as reopened:
        assert reopened.get(TargetActionDate, item_id) is None


@pytest.mark.parametrize("selected_index", [0, 2, 4])
def test_identified_group_deletes_every_filing_and_manual_match(db, selected_index, monkeypatch):
    group_ids = [filing(db, f"source-{index}") for index in range(3)]
    group_ids += [manual(db, notes=note) for note in ("First confirmation", "Second confirmation")]
    db.commit()
    page = tad.list_target_action_dates(db)
    assert page["total"] == 1
    assert page["items"][0]["entry_method"] == "mixed"
    assert page["items"][0]["occurrence_count"] == 5

    def forbidden(*args, **kwargs):
        pytest.fail("Deleting saved results must not read or mutate source filings")

    monkeypatch.setattr(tad, "_source_engine", forbidden)
    monkeypatch.setattr(tad, "extract_target_action_dates", forbidden)
    result = delete_saved(db, group_ids[selected_index])
    assert result["deleted"] == 5
    db.commit()
    with Session() as reopened:
        assert ids(reopened) == set()


def test_deleting_identified_group_keeps_other_dates_tickers_precisions_and_statuses(db):
    target = filing(db, "selected", value="2028-02", precision="month")
    manual(db, value="2028-02")
    retained = {
        filing(db, "other ticker", ticker="OTHER", value="2028-02", precision="month"),
        filing(db, "other month", value="2028-03", precision="month"),
        filing(db, "exact day", value="2028-02-01", precision="day"),
        filing(db, "different precision", value="2028-02", precision="unknown"),
        filing(db, "different status", value="2028-02", precision="month", status="ambiguous"),
        filing(db, "unresolved", value=None, precision="unknown", status="unidentifiable"),
    }
    db.commit()
    assert delete_saved(db, target)["deleted"] == 2
    db.commit()
    with Session() as reopened:
        assert ids(reopened) == retained


@pytest.mark.parametrize("status", ["ambiguous", "unidentifiable"])
def test_unresolved_delete_removes_only_selected_evidence(db, status):
    selected = filing(db, "selected", value=None, precision="unknown", status=status)
    retained = {
        filing(db, "same unresolved", value=None, precision="unknown", status=status),
        filing(db, "other unresolved", ticker="OTHER", value=None, precision="unknown", status=status),
        filing(db, "identified"),
        manual(db),
    }
    db.commit()
    result = delete_saved(db, selected)
    assert result["deleted"] == 1
    assert result["tad"] is None
    db.commit()
    assert ids(db) == retained


def test_caller_can_roll_back_entire_group_deletion(db):
    saved_ids = {filing(db, "source"), manual(db)}
    db.commit()
    assert delete_saved(db, min(saved_ids))["deleted"] == 2
    assert not ids(db)
    db.rollback()
    with Session() as reopened:
        assert ids(reopened) == saved_ids
        assert tad.list_target_action_dates(reopened)["total"] == 1


@pytest.mark.parametrize("item_id", [None, 0, -1, True, False, "1", "", 1.0])
def test_invalid_selection_is_rejected_without_deleting(db, item_id):
    saved_id = filing(db, "retained")
    db.commit()
    with pytest.raises(tad.TADValidationError, match="Select a saved"):
        delete_saved(db, item_id)
    assert ids(db) == {saved_id}


def test_nonexistent_selection_is_rejected_without_deleting(db):
    saved_id = filing(db, "retained")
    db.commit()
    with pytest.raises(tad.TADValidationError, match="no longer exists"):
        delete_saved(db, saved_id + 100)
    assert ids(db) == {saved_id}


def test_stale_selected_id_does_not_delete_remaining_group(db):
    stale_id = filing(db, "stale")
    retained_id = filing(db, "retained")
    db.commit()
    stale = db.get(TargetActionDate, stale_id)
    assert stale is not None
    expected_identity = stale.identity
    with Session() as other:
        other.execute(delete(TargetActionDate).where(TargetActionDate.id == stale_id))
        other.commit()
    with pytest.raises(tad.TADValidationError, match="no longer exists"):
        delete_saved(db, stale_id, expected_identity=expected_identity)
    assert ids(db) == {retained_id}


def test_selected_row_uses_current_group_fields_despite_identity_map(db):
    selected = filing(db, "selected")
    old_group = filing(db, "old group")
    new_group = filing(db, "new group", ticker="OTHER")
    db.commit()
    cached = db.get(TargetActionDate, selected)
    assert cached.symbol == "ARVN"
    with Session() as other:
        other.get(TargetActionDate, selected).symbol = "OTHER"
        other.commit()
    # The locked current row must be refreshed, not grouped using cached data.
    result = delete_saved(db, selected)
    assert result["ticker"] == "OTHER"
    assert result["deleted"] == 2
    db.commit()
    assert ids(db) == {old_group}


def test_delete_refuses_any_other_mysql_schema(db, monkeypatch):
    saved_id = filing(db, "retained")
    db.commit()
    expected_identity = db.get(TargetActionDate, saved_id).identity
    binding = SimpleNamespace(dialect=SimpleNamespace(name="mysql"), url=SimpleNamespace(database="filings"))
    monkeypatch.setattr(db, "get_bind", lambda *args, **kwargs: binding)
    with pytest.raises(RuntimeError, match="trialdelta schema"):
        delete_saved(db, saved_id, expected_identity=expected_identity)
    with Session() as reopened:
        assert ids(reopened) == {saved_id}


def test_deleting_a_saved_match_does_not_suppress_later_explicit_import(db):
    saved_id = filing(db, "source")
    db.commit()
    saved = db.get(TargetActionDate, saved_id)
    values = {column.name: getattr(saved, column.name) for column in TargetActionDate.__table__.columns if column.name != "id"}
    delete_saved(db, saved_id)
    db.commit()
    # The importer's existing idempotent storage path can recreate a deleted
    # match. There is deliberately no tombstone or source-table modification.
    assert tad._save_result(db, values) is True
    db.commit()
    assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 1


def test_selection_identity_is_available_only_when_explicitly_requested(db):
    saved_id = filing(db, "selected")
    db.commit()
    public_row = tad.list_target_action_dates(db)["items"][0]
    assert "selection_identity" not in public_row
    admin_row = tad.list_target_action_dates(db, include_identity=True)["items"][0]
    assert admin_row["selection_identity"] == db.get(TargetActionDate, saved_id).identity
    assert {key: value for key, value in admin_row.items() if key != "selection_identity"} == public_row


@pytest.mark.parametrize("token", [None, "", "a" * 63, "a" * 65, "g" * 64, "A" * 64, True, 1])
def test_delete_requires_valid_snapshot_identity(db, token):
    saved_id = filing(db, "retained")
    db.commit()
    with pytest.raises(tad.TADValidationError, match="Refresh the list"):
        tad.delete_target_action_date(db, saved_id, expected_identity=token)
    assert ids(db) == {saved_id}


def test_stale_snapshot_cannot_delete_a_different_date_reusing_the_id(db):
    selected_id = filing(db, "old selection")
    db.commit()
    old_snapshot = tad.list_target_action_dates(db, include_identity=True)["items"][0]
    delete_saved(db, selected_id, expected_identity=old_snapshot["selection_identity"])
    db.commit()
    replacement_id = filing(db, "replacement", ticker="OTHER", value="2029-03", precision="month")
    db.commit()
    assert replacement_id == selected_id  # SQLite reuses the deleted maximum ID.
    with pytest.raises(tad.TADValidationError, match="has changed"):
        delete_saved(db, selected_id, expected_identity=old_snapshot["selection_identity"])
    db.rollback()
    with Session() as reopened:
        replacement = reopened.get(TargetActionDate, replacement_id)
        assert (replacement.symbol, replacement.tad) == ("OTHER", "2029-03")


def test_wrong_snapshot_identity_cannot_delete_an_existing_group(db):
    selected_id = filing(db, "selected")
    another_id = filing(db, "another", ticker="OTHER")
    db.commit()
    wrong_identity = db.get(TargetActionDate, another_id).identity
    with pytest.raises(tad.TADValidationError, match="has changed"):
        delete_saved(db, selected_id, expected_identity=wrong_identity)
    assert ids(db) == {selected_id, another_id}
