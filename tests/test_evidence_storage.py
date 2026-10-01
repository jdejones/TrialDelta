"""Large registry evidence must survive storage and upgrades without truncation."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.schema import CreateTable

from trialdelta import db as database


def test_large_evidence_columns_use_mysql_longtext_without_changing_sqlite():
    for model, names in (
        (database.Snapshot, ("raw_json", "normalized")),
        (database.Change, ("before_value", "after_value")),
    ):
        mysql_ddl = str(CreateTable(model.__table__).compile(dialect=mysql.dialect()))
        sqlite_ddl = str(CreateTable(model.__table__).compile(dialect=sqlite.dialect()))
        for name in names:
            assert f"{name} LONGTEXT" in mysql_ddl
            assert f"{name} TEXT" in sqlite_ddl
        # Compiling one backend must not mutate metadata for another backend.
        assert str(CreateTable(model.__table__).compile(dialect=mysql.dialect())) == mysql_ddl


def column(table, name, data_type="text", nullable="NO", collation="utf8mb4_bin"):
    return dict(table_name=table, column_name=name, data_type=data_type,
                is_nullable=nullable, charset="utf8mb4", collation=collation)


@pytest.fixture
def mysql_connection(monkeypatch):
    conn = MagicMock()
    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="mysql"),
        url=SimpleNamespace(database="trialdelta"),
        connect=MagicMock(),
    )
    engine.connect.return_value.__enter__.return_value = conn
    monkeypatch.setattr(database, "engine", engine)
    return conn


def set_columns(conn, *snapshots):
    conn.execute.return_value.mappings.side_effect = snapshots


def statements(conn):
    return [str(call.args[0]) for call in conn.exec_driver_sql.call_args_list]


def test_existing_text_columns_are_widened_preserving_encoding_and_nullability(mysql_connection):
    conn = mysql_connection
    rows = [
        column("td_snapshots", "raw_json", "longtext"),
        column("td_snapshots", "normalized"),
        column("td_changes", "before_value", nullable="YES"),
        column("td_changes", "after_value", "mediumtext", nullable="YES"),
        column("td_changes", "review_note"),
    ]
    set_columns(conn, rows, rows, [dict(row, data_type="longtext") for row in rows])
    conn.scalar.side_effect = [1, 31536000]

    assert database.upgrade_mysql_evidence_storage() == ["td_snapshots", "td_changes"]
    assert statements(conn) == [
        "SET SESSION lock_wait_timeout = 10",
        "ALTER TABLE `td_snapshots` MODIFY COLUMN `normalized` LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL",
        "ALTER TABLE `td_changes` MODIFY COLUMN `before_value` LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL, MODIFY COLUMN `after_value` LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL",
        "SET SESSION lock_wait_timeout = 31536000",
    ]
    assert str(conn.execute.call_args.args[0]) == "SELECT RELEASE_LOCK('trialdelta_registry_sync')"
    ddl_count = conn.exec_driver_sql.call_count
    assert database.upgrade_mysql_evidence_storage() == []
    assert conn.exec_driver_sql.call_count == ddl_count
    assert conn.scalar.call_count == 2


def test_upgrade_rechecks_after_waiting_for_another_startup(mysql_connection):
    conn = mysql_connection
    row = column("td_snapshots", "normalized")
    set_columns(conn, [row], [dict(row, data_type="longtext")])
    conn.scalar.side_effect = [1, 30]
    assert database.upgrade_mysql_evidence_storage() == []
    assert not any(sql.startswith("ALTER") for sql in statements(conn))
    assert str(conn.execute.call_args.args[0]) == "SELECT RELEASE_LOCK('trialdelta_registry_sync')"


def test_upgrade_does_not_modify_tables_during_collection(mysql_connection):
    conn = mysql_connection
    set_columns(conn, [column("td_snapshots", "normalized")])
    conn.scalar.return_value = 0
    with pytest.raises(RuntimeError, match="Retry after the current run finishes"):
        database.upgrade_mysql_evidence_storage()
    conn.exec_driver_sql.assert_not_called()


def test_failed_ddl_releases_collection_lock_and_restores_timeout(mysql_connection):
    conn = mysql_connection
    rows = [column("td_snapshots", "normalized")]
    set_columns(conn, rows, rows)
    conn.scalar.side_effect = [1, 30]
    conn.exec_driver_sql.side_effect = [None, RuntimeError("DDL failed"), None]
    with pytest.raises(RuntimeError, match="DDL failed"):
        database.upgrade_mysql_evidence_storage()
    assert statements(conn)[-1] == "SET SESSION lock_wait_timeout = 30"
    assert str(conn.execute.call_args.args[0]) == "SELECT RELEASE_LOCK('trialdelta_registry_sync')"


def test_upgrade_refuses_non_trialdelta_schema(mysql_connection):
    database.engine.url.database = "other_database"
    with pytest.raises(RuntimeError, match="restricted to the trialdelta schema"):
        database.upgrade_mysql_evidence_storage()
    database.engine.connect.assert_not_called()


def test_sqlite_needs_no_storage_migration(mysql_connection):
    database.engine.dialect.name = "sqlite"
    assert database.upgrade_mysql_evidence_storage() == []
    database.engine.connect.assert_not_called()
