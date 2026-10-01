"""One supplied filing is recognized and committed without watching the stream."""

import io
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.orm import Session

from trialdelta import tad, tad_worker
from trialdelta.db import Base, TargetActionDate
from trialdelta.tad_worker import (
    JOBS, FilingRecognitionWorker, configured_destination, configured_urls,
    filing_identity, normalize_filing, read_filing,
)


def filing(markup="The target action date is November 14, 2027.", **fields):
    return {
        "symbol": "ARVN", "form_type": "8-K", "filing": markup,
        "accession_no": "0000012345-26-000001", "filed_at": datetime(2026, 9, 27, 10, 30),
        "filing_link": "https://www.sec.gov/Archives/example.htm", **fields,
    }


def input_stream(row=None):
    return io.StringIO(json.dumps({"command": "filing", "row": row or filing()}, default=str))


@pytest.fixture
def worker_setup(tmp_path):
    destination = create_engine("sqlite:///" + str(tmp_path / "destination.db"))
    Base.metadata.create_all(destination)
    logs = []
    worker = FilingRecognitionWorker(destination, tad.save_filing_target_action_dates, log=logs.append)
    worker.initialize()
    yield worker, destination, logs
    destination.dispose()


def receipts(destination):
    with destination.connect() as connection:
        return connection.execute(select(JOBS).order_by(JOBS.c.identity)).mappings().all()


@pytest.mark.parametrize("values", [
    {}, {"DATABASE_URL": "sqlite:///wrong.db"},
    {"DATABASE_URL": "mysql+pymysql://localhost/stocks"},
    {"USE_LOCAL_MYSQL": "true"},
    {"DATABASE_URL": "postgresql://localhost/trialdelta"},
])
def test_configuration_refuses_missing_or_wrong_mysql_destination(values):
    with pytest.raises(ValueError):
        configured_destination(values)


def test_destination_is_pinned_independently_of_unused_source_configuration():
    settings = {
        "USE_LOCAL_MYSQL": "true", "gptdb": "gptdb-test-password",
        "DATABASE_PASSWORD": "unused-root-password",
        "MYSQL_HOST": "destination-host", "MYSQL_PORT": "3307",
        "DATABASE_URL": "sqlite:///must-not-be-used.db",
        "SOURCE_DATABASE_URL": "sqlite:///unused-source.db",
    }
    destination = configured_destination(settings)
    assert destination.database == "trialdelta"
    assert destination.username == "gptdb"
    assert destination.password == "gptdb-test-password"
    assert destination.host == "destination-host" and destination.port == 3307
    with pytest.raises(ValueError):
        configured_urls(settings)
    settings["SOURCE_DATABASE_URL"] = "mysql+pymysql://reader:password@source-host/filings"
    checked_destination, source = configured_urls(settings)
    assert checked_destination == destination
    assert source.database == "filings" and source.host == "source-host"


@pytest.mark.parametrize("gptdb_password", [None, ""])
def test_local_destination_requires_gptdb_password_despite_root_credential(gptdb_password):
    settings = {"USE_LOCAL_MYSQL": "true", "DATABASE_PASSWORD": "unused-root-password"}
    if gptdb_password is not None:
        settings["gptdb"] = gptdb_password
    with pytest.raises(ValueError, match="gptdb"):
        configured_destination(settings)


def test_explicit_destination_url_does_not_require_local_mysql_credentials():
    destination = configured_destination({
        "USE_LOCAL_MYSQL": "false",
        "DATABASE_URL": "mysql+pymysql://url-user:url-password@db-host/trialdelta",
    })
    assert destination.username == "url-user"
    assert destination.password == "url-password"
    assert destination.host == "db-host"


def test_worker_rejects_wrong_mysql_database_before_connecting():
    destination = SimpleNamespace(
        dialect=SimpleNamespace(name="mysql"), url=SimpleNamespace(database="filings"),
    )
    with pytest.raises(ValueError, match="trialdelta"):
        FilingRecognitionWorker(destination, tad.save_filing_target_action_dates)


def test_source_and_handoff_timestamp_forms_have_same_identity():
    original = normalize_filing(filing(symbol=" arvn ", form_type="8-k"))
    incoming = normalize_filing(filing(filed_at="2026-09-27 10:30:00"))
    assert original == incoming
    assert filing_identity(original) == filing_identity(incoming)
    assert filing_identity(normalize_filing(filing(filing="Changed HTML"))) != filing_identity(original)


def test_identified_month_unresolved_and_no_marker_complete(worker_setup):
    worker, destination, _ = worker_setup
    for index, markup in enumerate([
        "The target action date is November 14, 2027.",
        "The target action date is December 2027.",
        "The target action date has not been determined.",
        "The target action date is 05/06/2027.",
        "No relevant wording in this filing.",
    ]):
        assert worker.process_filing(filing(markup, accession_no=str(index)))
    jobs = receipts(destination)
    assert len(jobs) == 5
    assert all(row["state"] == "complete" and row["payload"] is None for row in jobs)
    assert all(row["attempts"] == 1 for row in jobs)
    assert sorted(row["matched"] for row in jobs) == [0, 1, 1, 1, 1]
    with Session(destination) as db:
        page = tad.list_target_action_dates(db)
        assert page["total"] == 4
        assert page["identified"] == 2 and page["month"] == 1 and page["unresolved"] == 2
        assert {row["status"] for row in page["items"]} == {"identified", "ambiguous", "unidentifiable"}


@pytest.mark.parametrize("old_state", ["pending", "retry", "failed"])
def test_only_supplied_filing_is_processed_with_unrelated_jobs_untouched(worker_setup, old_state):
    worker, destination, _ = worker_setup
    old = filing(accession_no="old-unrelated")
    assert worker.enqueue(old)
    old_identity = filing_identity(normalize_filing(old))
    with destination.begin() as connection:
        connection.execute(update(JOBS).where(JOBS.c.identity == old_identity).values(
            state=old_state, attempts=4, retry_at=123.0,
        ))
    before = dict(receipts(destination)[0])
    seen, statements = [], []

    def save(db, row):
        seen.append(row["accession_no"])
        return tad.save_filing_target_action_dates(db, row)

    worker.save_filing = save
    event.listen(destination, "before_cursor_execute", lambda c, cu, statement, p, ctx, m: statements.append(statement))
    assert worker.process_filing(filing("The target action date is December 2027.", accession_no="new-supplied"))
    assert seen == ["new-supplied"]
    retained = next(row for row in receipts(destination) if row["identity"] == old_identity)
    assert dict(retained) == before
    assert not any("biotech_filings" in statement.lower() for statement in statements)
    with Session(destination) as db:
        assert list(db.scalars(select(TargetActionDate.accession_no))) == ["new-supplied"]


def test_duplicate_invocation_does_not_reparse_completed_filing(worker_setup):
    worker, destination, _ = worker_setup
    row = filing()
    assert worker.process_filing(row)
    before = [dict(receipt) for receipt in receipts(destination)]

    def no_repeat(db, row):
        pytest.fail("An already completed receipt must not parse again")

    second = FilingRecognitionWorker(destination, no_repeat, log=lambda message: None)
    second.initialize()
    assert second.process_filing(row)
    assert [dict(receipt) for receipt in receipts(destination)] == before
    with Session(destination) as db:
        assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 1


def test_failure_rolls_back_results_and_only_explicit_reinvocation_retries(worker_setup):
    worker, destination, logs = worker_setup
    bad = filing(accession_no="retry-me")
    good = filing("The target action date is December 2027.", accession_no="later-filing")

    def fail_after_insert(db, row):
        tad.save_filing_target_action_dates(db, row)
        raise RuntimeError("secret-password private filing HTML")

    worker.save_filing = fail_after_insert
    assert worker.process_filing(bad) is False
    failed = receipts(destination)[0]
    assert failed["state"] == "failed" and failed["attempts"] == 1
    assert failed["retry_at"] == 0 and json.loads(failed["payload"])["accession_no"] == "retry-me"
    with Session(destination) as db:
        assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 0
    assert "secret-password" not in " ".join(logs)
    assert any("Import stored filings" in line for line in logs)

    fresh = FilingRecognitionWorker(destination, tad.save_filing_target_action_dates, log=logs.append)
    assert fresh.process_filing(good)
    assert dict(next(row for row in receipts(destination) if row["identity"] == failed["identity"])) == dict(failed)
    assert fresh.process_filing(bad)
    jobs = receipts(destination)
    assert all(row["state"] == "complete" and row["payload"] is None for row in jobs)
    assert next(row for row in jobs if row["identity"] == failed["identity"])["attempts"] == 2
    with Session(destination) as db:
        assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 2


def test_success_receipt_write_failure_rolls_back_extracted_results(worker_setup):
    worker, destination, _ = worker_setup
    fail_once = [True]

    def reject_complete(connection, cursor, statement, parameters, context, many):
        if fail_once[0] and statement.startswith("UPDATE td_tad_filing_jobs") and "complete" in parameters:
            fail_once[0] = False
            raise RuntimeError("Simulated receipt write failure")

    event.listen(destination, "before_cursor_execute", reject_complete)
    assert worker.process_filing(filing()) is False
    with Session(destination) as db:
        assert db.scalar(select(func.count()).select_from(TargetActionDate)) == 0
    assert receipts(destination)[0]["state"] == "failed"
    assert worker.process_filing(filing())
    assert receipts(destination)[0]["state"] == "complete"


@pytest.mark.parametrize("markup", [
    "The target action date has not been determined.", "The target action date is 05/06/2027.",
])
def test_unresolved_manual_correction_survives_repeat_and_manual_import_paths(worker_setup, markup):
    worker, destination, _ = worker_setup
    row = filing(markup)
    assert worker.process_filing(row)
    with Session(destination) as db:
        original = tad.list_target_action_dates(db, include_identity=True)["items"][0]
        assert original["tad"] is None and original["status"] != "identified"
        tad.edit_target_action_date(db, original["id"], expected_identity=original["selection_identity"],
                                   ticker="ARVN", tad="2028-01-15", notes="Manually verified", entered_by=1)
        db.commit()
    assert worker.process_filing(row)
    with Session(destination) as db:
        assert tad.save_filing_target_action_dates(db, row)["existing"] == 1
        db.commit()
        saved = tad.list_target_action_dates(db)["items"][0]
        assert saved["tad"] == "2028-01-15" and saved["is_edited"]
        assert saved["original_tad"] is None and saved["original_context"] == original["context"]


def test_invalid_timestamp_is_retained_as_failed_receipt_without_affecting_next_filing(worker_setup):
    worker, destination, _ = worker_setup
    assert worker.process_filing(filing(filed_at="0000-00-00 00:00:00", accession_no="legacy")) is False
    failed = receipts(destination)[0]
    assert failed["state"] == "failed"
    assert json.loads(failed["payload"])["filed_at"] == "0000-00-00 00:00:00"
    assert worker.process_filing(filing(accession_no="valid"))
    with Session(destination) as db:
        assert list(db.scalars(select(TargetActionDate.accession_no))) == ["valid"]


INVALID_INPUTS = [
    "", "not json", "[]", "null", '{"command":"stop"}', '{"command":"reconcile"}',
    '{"command":"filing","row":{}}', '{"command":"filing","row":[]}',
    input_stream().getvalue() + "\n" + input_stream().getvalue(),
]


@pytest.mark.parametrize("message", INVALID_INPUTS)
def test_read_filing_rejects_control_or_multiple_messages(message):
    with pytest.raises(ValueError):
        read_filing(io.StringIO(message))


def test_read_filing_accepts_exactly_one_complete_handoff():
    row = read_filing(input_stream())
    assert normalize_filing(row) == normalize_filing(filing())


@pytest.mark.parametrize("message", INVALID_INPUTS)
def test_invalid_once_input_is_rejected_before_database_configuration(message, monkeypatch, capsys):
    monkeypatch.setattr(tad_worker.sys, "stdin", io.StringIO(message))

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid messages must be rejected before database setup")

    monkeypatch.setattr(tad_worker, "configured_destination", forbidden)
    monkeypatch.setattr(tad_worker, "create_engine", forbidden)
    assert tad_worker.main(["--once"]) == 1
    assert "Import stored filings" in capsys.readouterr().out


def test_no_arguments_exits_without_reading_stdin_or_database(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No-argument execution must never read input or open the database")

    monkeypatch.setattr(tad_worker, "read_filing", forbidden)
    monkeypatch.setattr(tad_worker, "create_engine", forbidden)
    with pytest.raises(SystemExit) as stopped:
        tad_worker.main([])
    assert stopped.value.code == 2


@pytest.mark.parametrize("succeeds", [True, False])
def test_once_main_opens_only_destination_processes_one_filing_and_returns(worker_setup, monkeypatch, succeeds, capsys):
    original_worker, destination, _ = worker_setup
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(tad_worker.sys, "stdin", input_stream())
    destination_url = object()
    monkeypatch.setattr(tad_worker, "configured_destination", lambda: destination_url)
    opened, processed, statements = [], [], []
    original_save = original_worker.save_filing

    def make_engine(url, **kwargs):
        opened.append(url)
        assert url is destination_url
        return destination

    def no_source(*args, **kwargs):
        pytest.fail("One-shot recognition must not configure or access a source database")

    def save(db, row):
        processed.append(row["accession_no"])
        result = original_save(db, row)
        if not succeeds:
            raise RuntimeError("secret-password private filing HTML")
        return result

    monkeypatch.setattr(tad_worker, "create_engine", make_engine)
    monkeypatch.setattr(tad_worker, "configured_urls", no_source)
    monkeypatch.setattr(tad, "save_filing_target_action_dates", save)
    event.listen(destination, "before_cursor_execute", lambda c, cu, statement, p, ctx, m: statements.append(statement))
    assert tad_worker.main(["--once"]) == (0 if succeeds else 1)
    assert opened == [destination_url] and processed == ["0000012345-26-000001"]
    assert not any("biotech_filings" in statement.lower() for statement in statements)
    assert receipts(destination)[0]["state"] == ("complete" if succeeds else "failed")
    assert "secret-password" not in capsys.readouterr().out

