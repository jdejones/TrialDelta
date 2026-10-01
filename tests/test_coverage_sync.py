"""Coverage lifecycle, source safety and additive upgrades, using isolated DBs."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, inspect, select, text

from trialdelta import company_discovery as discovery
from trialdelta import db as database
from trialdelta.db import Company, Snapshot, Study
from trialdelta.registry import ingest


@pytest.fixture
def source(tmp_path, monkeypatch):
    names = tmp_path / "names.json"
    monkeypatch.setenv("HEALTHCARE_COMPANY_NAMES_PATH", str(names))

    def set_source(classifications, company_names):
        names.write_text(json.dumps(company_names), encoding="utf-8")
        monkeypatch.setattr(discovery, "_read_classifications", lambda: discovery._validate_classifications(classifications))

    return set_source


def classified(symbol, sector="Healthcare"):
    return {"symbol": symbol, "sector": sector, "industry": "Biotechnology"}


def test_delisting_reclassification_and_reactivation_preserve_all_history(db, raw, source):
    arvinas = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    beam = db.scalar(select(Company).where(Company.ticker == "BEAM"))
    ingest(db, arvinas, raw)
    db.commit()
    before = (list(db.scalars(select(Study.id))), list(db.scalars(select(Snapshot.id))))
    source([classified("BEAM", "Technology"), classified("FRESH")], {"FRESH": "Fresh Research Inc"})
    result = discovery.discover_companies()
    db.expire_all()
    assert arvinas.coverage_status == "delisted"
    assert beam.coverage_status == "out_of_sector"
    assert arvinas.coverage_updated_at
    assert result["out_of_sector"] == 1
    assert (list(db.scalars(select(Study.id))), list(db.scalars(select(Snapshot.id)))) == before
    assert arvinas.id not in result["company_ids"]
    assert beam.id not in result["company_ids"]

    # A returning Healthcare listing reuses the company ID and all evidence.
    source([classified("ARVN"), classified("BEAM"), classified("FRESH")], {"ARVN": "Arvinas Inc", "FRESH": "Fresh Research Inc"})
    again = discovery.discover_companies()
    db.expire_all()
    assert arvinas.coverage_status == beam.coverage_status == "active"
    assert again["reactivated"] == 2
    assert {arvinas.id, beam.id} <= set(again["company_ids"])
    assert (list(db.scalars(select(Study.id))), list(db.scalars(select(Snapshot.id)))) == before


def test_missing_names_keep_existing_mapping_and_skip_new_symbols(db, source):
    arvinas = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    original = (arvinas.name, arvinas.sponsor)
    source([classified("ARVN"), classified("UNKNOWN"), classified("READY")], {"READY": "Ready Research Inc"})
    result = discovery.discover_companies()
    db.expire_all()
    assert arvinas.coverage_status == "active"
    assert (arvinas.name, arvinas.sponsor) == original
    assert arvinas.id in result["company_ids"]
    assert db.scalar(select(Company).where(Company.ticker == "UNKNOWN")) is None
    assert db.scalar(select(Company).where(Company.ticker == "READY")) is not None


@pytest.mark.parametrize("rows", [[], [classified("OTHER", "Technology")], [classified("ARVN"), {"symbol": None, "sector": "Healthcare"}], [classified("ARVN"), classified("arvn")]])
def test_invalid_source_never_retires_or_modifies_saved_companies(db, source, rows):
    before = [(c.id, c.coverage_status, c.coverage_updated_at) for c in db.scalars(select(Company))]
    source(rows, {"ARVN": "Arvinas Inc"})
    with pytest.raises(discovery.CompanyDiscoveryError):
        discovery.discover_companies()
    db.expire_all()
    assert [(c.id, c.coverage_status, c.coverage_updated_at) for c in db.scalars(select(Company))] == before


def test_unavailable_names_leave_statuses_and_company_names_unchanged(db, source, monkeypatch):
    source([classified("FRESH")], {"FRESH": "Fresh Research"})
    monkeypatch.setenv("HEALTHCARE_COMPANY_NAMES_PATH", "Z:/no-such-private-name-file.json")
    with pytest.raises(discovery.CompanyDiscoveryError) as error:
        discovery.discover_companies()
    assert "Z:/" not in str(error.value)
    assert all(c.coverage_status == "active" and c.coverage_updated_at is None for c in db.scalars(select(Company)))
    assert db.scalar(select(Company).where(Company.ticker == "FRESH")) is None


def test_rejected_rename_cannot_leave_another_update_with_a_duplicate_sponsor(db, source):
    companies = [Company(ticker=ticker, name=name, sponsor=name) for ticker, name in (
        ("FIRST", "Alpha Research"), ("SECOND", "Beta Research"), ("THIRD", "Gamma Research"),
    )]
    db.add_all(companies)
    db.commit()
    source([classified("FIRST"), classified("SECOND")], {
        "FIRST": "Beta Research Inc", "SECOND": "Gamma Research Inc",
    })
    result = discovery.discover_companies()
    db.expire_all()
    assert result["skipped"] == 2
    assert [company.sponsor for company in companies] == ["Alpha Research", "Beta Research", "Gamma Research"]
    assert [company.coverage_status for company in companies] == ["active", "active", "delisted"]


def test_source_read_is_one_full_select_and_does_not_mutate_source(tmp_path, monkeypatch):
    source_engine = create_engine("sqlite://")
    try:
        with source_engine.begin() as connection:
            connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS stocks")
            connection.exec_driver_sql("CREATE TABLE stocks.symbol_sector_industry (symbol TEXT, sector TEXT, industry TEXT)")
            connection.execute(text("INSERT INTO stocks.symbol_sector_industry VALUES (:symbol, :sector, :industry)"), [classified("BIO"), classified("TECH", "Technology")])
        statements = []
        from sqlalchemy import event
        event.listen(source_engine, "before_cursor_execute", lambda conn, cursor, sql, params, ctx, many: statements.append(sql))
        monkeypatch.setattr(discovery, "_source_engine", lambda: source_engine)
        # Do not dispose this in-memory source before the assertions inspect it.
        monkeypatch.setattr(discovery, "engine", source_engine)
        result = discovery._read_classifications()
        assert result["TECH"]["sector"] == "Technology"
        assert result["BIO"]["sector"] == "Healthcare"
        assert statements == ["SELECT symbol, sector, industry FROM stocks.symbol_sector_industry"]
    finally:
        source_engine.dispose()


def test_source_errors_are_safe_and_external_engine_is_disposed(monkeypatch):
    source_engine = MagicMock()
    source_engine.connect.side_effect = RuntimeError("mysql://user:secret@private/db")
    monkeypatch.setattr(discovery, "_source_engine", lambda: source_engine)
    with pytest.raises(discovery.CompanyDiscoveryError) as error:
        discovery._read_classifications()
    assert "secret" not in str(error.value)
    source_engine.dispose.assert_called_once()


def test_legacy_sqlite_upgrade_preserves_company_data_and_is_idempotent(tmp_path, monkeypatch):
    old = create_engine("sqlite:///" + str(tmp_path / "old.db"))
    try:
        with old.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE td_companies (id INTEGER PRIMARY KEY, ticker TEXT, name TEXT)")
            connection.exec_driver_sql("INSERT INTO td_companies VALUES (7, 'OLD', 'Old Company')")
        monkeypatch.setattr(database, "engine", old)
        assert database.upgrade_company_coverage_storage() == ["coverage_status", "coverage_updated_at"]
        assert database.upgrade_company_coverage_storage() == []
        with old.connect() as connection:
            assert connection.exec_driver_sql("SELECT * FROM td_companies").one() == (7, "OLD", "Old Company", "active", None)
    finally:
        old.dispose()


def test_mysql_upgrade_waits_for_collection_and_releases_lock(monkeypatch):
    conn = MagicMock()
    engine = SimpleNamespace(dialect=SimpleNamespace(name="mysql"), url=SimpleNamespace(database="trialdelta"), connect=MagicMock())
    engine.connect.return_value.__enter__.return_value = conn
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "_missing_coverage_columns", lambda c: ["coverage_status", "coverage_updated_at"])
    conn.scalar.side_effect = [1, 60]
    assert database.upgrade_company_coverage_storage() == ["coverage_status", "coverage_updated_at"]
    sql = [call.args[0] for call in conn.exec_driver_sql.call_args_list]
    assert all("DROP" not in item and "DELETE" not in item for item in sql)
    assert sql[-1] == "SET SESSION lock_wait_timeout = 60"
    assert str(conn.execute.call_args.args[0]) == "SELECT RELEASE_LOCK('trialdelta_registry_sync')"


def test_mysql_upgrade_does_not_change_schema_while_collector_holds_lock(monkeypatch):
    conn = MagicMock()
    conn.scalar.return_value = 0
    engine = SimpleNamespace(dialect=SimpleNamespace(name="mysql"), url=SimpleNamespace(database="trialdelta"), connect=MagicMock())
    engine.connect.return_value.__enter__.return_value = conn
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "_missing_coverage_columns", lambda c: ["coverage_status"])
    with pytest.raises(RuntimeError, match="waiting for collection"):
        database.upgrade_company_coverage_storage()
    conn.exec_driver_sql.assert_not_called()
