"""Manual dates retain precision and provenance alongside imported evidence."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, func, inspect, select, text

from trialdelta import db as database, tad
from trialdelta.db import Session, TargetActionDate, User


def add(db, **overrides):
    admin = db.scalar(select(User).where(User.email == "local@trialdelta.local"))
    return tad.add_manual_target_action_date(db, **{
        "ticker": " arvn ", "tad": "2028-02-29", "source_url": "", "notes": "",
        "entered_by": admin.id, **overrides,
    })


@pytest.mark.parametrize("value,precision", [("2028-02-29", "day"), ("2028-02", "month")])
def test_manual_precision_and_provenance_survive_commit(db, monkeypatch, value, precision):
    def forbidden(*args, **kwargs):
        pytest.fail("Manual entry must not parse or read source filings")

    monkeypatch.setattr(tad, "_source_engine", forbidden)
    monkeypatch.setattr(tad, "extract_target_action_dates", forbidden)
    result = add(db, tad=value, source_url=" https://example.org/announcement ", notes=" Evidence note ")
    assert result["status"] == "saved"
    assert result["ticker"] == "ARVN"
    assert result["tad"] == value
    assert result["precision"] == precision
    db.commit()
    with Session() as reopened:
        row = reopened.get(TargetActionDate, result["id"])
        assert row.tad == value
        assert row.precision == precision
        assert row.entry_method == "manual"
        assert row.entered_by
        assert row.context == "Evidence note"
        assert row.matched_text == ""
        assert (row.start, row.end, row.context_start, row.context_end) == (0, 0, 0, 0)
        feed = tad.list_target_action_dates(reopened)["items"][0]
        assert feed["entry_method"] == feed["evidence_method"] == "manual"
        assert feed["source_count"] == 0
        assert feed["manual_count"] == feed["occurrence_count"] == 1
        assert feed["filing_sha256"] is feed["accession_no"] is feed["filed_at"] is None
        assert feed["filing_link"] == "https://example.org/announcement"
        assert "entered_by" not in feed


@pytest.mark.parametrize("value", [
    "2027-02-29", "2028-02-30", "2028-04-31", "2028-00", "2028-13", "0000-01",
    "2028-2", "2028-2-9", "2028", "February 2028", "02/29/2028", "2028-02-29T00:00:00",
    "2028-02-29 and 2028-03-01", "２０２８-０２", "", None,
])
def test_invalid_manual_dates_do_not_write(db, value):
    with pytest.raises(tad.TADValidationError):
        add(db, tad=value)
    assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 0


@pytest.mark.parametrize("ticker", ["", " ", "A" * 17, "A B", "<script>", ".ABC", "ABC-", "A..B", None])
def test_invalid_tickers_do_not_write(db, ticker):
    with pytest.raises(tad.TADValidationError, match="ticker"):
        add(db, ticker=ticker)
    assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 0


@pytest.mark.parametrize("ticker", ["BRK.B", "BIO-A", "123", "AB123"])
def test_common_ticker_variants_are_supported(db, ticker):
    assert add(db, ticker=ticker.lower())["ticker"] == ticker


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "file:///c:/temp/a", "https:///missing-host", "https://user:secret@example.org/",
    "https://user@example.org/", "https://@example.org/", "//example.org/file", "https://example.org:99999/",
    "https://example.org:invalid/", "https://example.org/a b", "https://example.org/a\nb",
    "https://example.org\\other", "https://example.org/" + "a" * 2048, None,
])
def test_invalid_source_links_do_not_write(db, url):
    with pytest.raises(tad.TADValidationError, match="URL"):
        add(db, source_url=url)
    assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 0


def test_notes_are_optional_bounded_and_not_treated_as_html(db):
    result = add(db, notes="<b>Operator note</b>")
    assert db.get(TargetActionDate, result["id"]).context == "<b>Operator note</b>"
    with pytest.raises(tad.TADValidationError, match="4,000"):
        add(db, notes="x" * 4001)
    with pytest.raises(tad.TADValidationError, match="4,000"):
        add(db, notes=None)
    assert add(db, ticker="OTHER", notes="x" * 4000)["status"] == "saved"


def test_duplicates_are_idempotent_and_retain_original_operator(db):
    first = add(db, notes=" Note ", source_url=" HTTPS://example.org/a ")
    db.commit()
    second = add(db, ticker="ARVN", notes="Note", source_url="https://example.org/a", entered_by=999)
    assert second == {**first, "status": "existing"}
    row = db.get(TargetActionDate, first["id"])
    assert row.entered_by != 999
    assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 1
    assert add(db, notes="A different source note", source_url="https://example.org/a")["status"] == "saved"


def test_caller_rollback_does_not_commit_the_insert_savepoint(db):
    result = add(db)
    db.rollback()
    with Session() as reopened:
        assert reopened.get(TargetActionDate, result["id"]) is None


def test_concurrent_duplicate_returns_id_despite_an_older_read_snapshot(db, monkeypatch):
    first = add(db)
    db.commit()
    admin_id = db.scalar(select(User.id).where(User.email == "local@trialdelta.local"))
    scalar = db.scalar

    def snapshot_read(statement, *args, **kwargs):
        # Simulate MySQL REPEATABLE READ when another writer committed the
        # duplicate after this session's first snapshot: current locking reads
        # see it, while ordinary reads retain the earlier empty snapshot.
        if statement._for_update_arg is None:
            return None
        return scalar(statement, *args, **kwargs)

    monkeypatch.setattr(db, "scalar", snapshot_read)
    duplicate = tad.add_manual_target_action_date(
        db, ticker="ARVN", tad="2028-02-29", entered_by=admin_id,
    )
    assert duplicate == {**first, "status": "existing"}
    db.commit()
    with Session() as reopened:
        assert reopened.scalar(select(func.count()).select_from(TargetActionDate)) == 1


def test_manual_and_filing_records_keep_distinct_identity_and_group_counts(db):
    result = tad.extract_target_action_dates("The target action date is February 29, 2028.")[0]
    filing_values = {
        **result, "identity": "a" * 64, "symbol": "ARVN", "form_type": "8-K",
        "filing_sha256": "b" * 64, "accession_no": "0000123456-28-000001",
    }
    db.add(TargetActionDate(**filing_values))
    db.commit()
    before = db.scalar(select(TargetActionDate))
    before_values = {column.name: getattr(before, column.name) for column in TargetActionDate.__table__.columns}
    manual = add(db, notes="Manual confirmation")
    db.commit()
    assert manual["id"] != before.id
    assert {column.name: getattr(before, column.name) for column in TargetActionDate.__table__.columns} == before_values
    page = tad.list_target_action_dates(db)
    assert page["total"] == 1
    item = page["items"][0]
    assert item["entry_method"] == "mixed"
    assert item["evidence_method"] == "filing"
    assert item["source_count"] == item["manual_count"] == 1
    assert item["occurrence_count"] == 2
    assert item["filing_sha256"] == "b" * 64
    assert tad._save_result(db, filing_values) is False
    assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 2


def test_group_provenance_is_honest_when_manual_entry_precedes_filing(db):
    add(db)
    result = tad.extract_target_action_dates("The target action date is February 29, 2028.")[0]
    db.add(TargetActionDate(**{
        **result, "identity": "a" * 64, "symbol": "ARVN", "form_type": "8-K", "filing_sha256": "b" * 64,
    }))
    db.commit()
    item = tad.list_target_action_dates(db)["items"][0]
    assert item["entry_method"] == "mixed"
    assert item["evidence_method"] == "manual"
    assert item["filing_sha256"] is None
    assert item["manual_count"] == item["source_count"] == 1


def test_manual_service_refuses_other_mysql_destination(db, monkeypatch):
    binding = SimpleNamespace(dialect=SimpleNamespace(name="mysql"), url=SimpleNamespace(database="filings"))
    monkeypatch.setattr(db, "get_bind", lambda *args, **kwargs: binding)
    with pytest.raises(RuntimeError, match="trialdelta schema"):
        tad.add_manual_target_action_date(db, ticker="ARVN", tad="2028-02", entered_by=1)


@pytest.mark.parametrize("entered_by", [None, 0, -1, True, "1"])
def test_manual_service_requires_operator_audit_id(db, entered_by):
    with pytest.raises(tad.TADValidationError, match="administrator"):
        add(db, entered_by=entered_by)


def test_sqlite_upgrade_preserves_old_rows_and_is_idempotent(tmp_path, monkeypatch):
    legacy = create_engine("sqlite:///" + str(tmp_path / "legacy.db"))
    monkeypatch.setattr(database, "engine", legacy)
    try:
        with legacy.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE td_target_action_dates (id INTEGER PRIMARY KEY, tad VARCHAR(10), context TEXT)")
            connection.execute(text("INSERT INTO td_target_action_dates VALUES (1, '2028-02', 'Original filing evidence')"))
        assert database.upgrade_target_action_date_storage() == ["entry_method", "entered_by"]
        assert database.upgrade_target_action_date_storage() == []
        with legacy.connect() as connection:
            row = connection.execute(text("SELECT * FROM td_target_action_dates")).mappings().one()
            assert dict(row) == {
                "id": 1, "tad": "2028-02", "context": "Original filing evidence", "entry_method": "filing", "entered_by": None,
            }
    finally:
        legacy.dispose()


def test_initialize_adds_columns_to_existing_complete_table(tmp_path, monkeypatch):
    legacy = create_engine("sqlite:///" + str(tmp_path / "startup.db"))
    monkeypatch.setattr(database, "engine", legacy)
    try:
        # Build the current table without its two additive columns to represent
        # an installed database. initialize must preserve all tables and rows.
        from sqlalchemy import MetaData, Table
        from sqlalchemy.schema import CreateTable

        old = Table("td_target_action_dates", MetaData(), *[
            column._copy() for column in TargetActionDate.__table__.columns
            if column.name not in ("entry_method", "entered_by")
        ])
        with legacy.begin() as connection:
            connection.execute(CreateTable(old))
            connection.execute(old.insert().values(
                identity="a" * 64, symbol="TEST", form_type="8-K", filing_sha256="b" * 64,
                tad="2028-02", precision="month", status="identified", matched_text="February 2028",
                reason="Month and year", start=0, end=13, context_start=0, context_end=13, context="February 2028",
            ))
        database.initialize()
        database.initialize()
        assert {"entry_method", "entered_by"}.issubset({column["name"] for column in inspect(legacy).get_columns("td_target_action_dates")})
        with legacy.connect() as connection:
            row = connection.execute(text("SELECT tad, entry_method, entered_by FROM td_target_action_dates")).one()
            assert tuple(row) == ("2028-02", "filing", None)
    finally:
        legacy.dispose()


@pytest.fixture
def mysql_upgrade(monkeypatch):
    connection = MagicMock()
    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="mysql"), url=SimpleNamespace(database="trialdelta"), connect=MagicMock(),
    )
    engine.connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(database, "engine", engine)
    missing = MagicMock(return_value=["entry_method", "entered_by"])
    monkeypatch.setattr(database, "_missing_tad_columns", missing)
    connection.scalar.side_effect = [1, 30]
    return engine, connection, missing


def test_mysql_upgrade_only_adds_columns_under_lock(mysql_upgrade):
    _, connection, missing = mysql_upgrade
    assert database.upgrade_target_action_date_storage() == ["entry_method", "entered_by"]
    assert missing.call_count == 2
    assert [call.args[0] for call in connection.exec_driver_sql.call_args_list] == [
        "SET SESSION lock_wait_timeout = 10",
        "ALTER TABLE `td_target_action_dates` ADD COLUMN `entry_method` VARCHAR(16) NOT NULL DEFAULT 'filing'",
        "ALTER TABLE `td_target_action_dates` ADD COLUMN `entered_by` INTEGER NULL",
        "SET SESSION lock_wait_timeout = 30",
    ]
    assert str(connection.execute.call_args.args[0]) == "SELECT RELEASE_LOCK('trialdelta_tad_storage')"


def test_mysql_upgrade_rechecks_after_another_startup(mysql_upgrade):
    _, connection, missing = mysql_upgrade
    missing.side_effect = [["entry_method", "entered_by"], []]
    assert database.upgrade_target_action_date_storage() == []
    assert not any(call.args[0].startswith("ALTER") for call in connection.exec_driver_sql.call_args_list)


def test_mysql_upgrade_failure_releases_lock_and_can_retry_partial_upgrade(mysql_upgrade):
    _, connection, missing = mysql_upgrade
    missing.return_value = ["entered_by"]
    connection.exec_driver_sql.side_effect = [None, RuntimeError("DDL failed"), None]
    with pytest.raises(RuntimeError, match="DDL failed"):
        database.upgrade_target_action_date_storage()
    assert connection.exec_driver_sql.call_args.args[0] == "SET SESSION lock_wait_timeout = 30"
    assert str(connection.execute.call_args.args[0]) == "SELECT RELEASE_LOCK('trialdelta_tad_storage')"


def test_mysql_upgrade_refuses_wrong_schema(mysql_upgrade):
    engine, connection, _ = mysql_upgrade
    engine.url.database = "filings"
    with pytest.raises(RuntimeError, match="trialdelta schema"):
        database.upgrade_target_action_date_storage()
    engine.connect.assert_not_called()


def test_mysql_upgrade_does_not_write_if_lock_unavailable(mysql_upgrade):
    _, connection, _ = mysql_upgrade
    connection.scalar.side_effect = [0]
    with pytest.raises(RuntimeError, match="busy"):
        database.upgrade_target_action_date_storage()
    connection.exec_driver_sql.assert_not_called()


def test_existing_manual_columns_require_no_upgrade_lock(mysql_upgrade):
    _, connection, missing = mysql_upgrade
    missing.return_value = []
    assert database.upgrade_target_action_date_storage() == []
    connection.scalar.assert_not_called()
    connection.exec_driver_sql.assert_not_called()
