import json

import pytest
from sqlalchemy import select

from trialdelta import company_discovery as discovery
from trialdelta.company_names import clean_company_name, sponsor_key
from trialdelta.db import Company, Snapshot, Study
from trialdelta.registry import ingest


@pytest.fixture
def reference_files(tmp_path, monkeypatch):
    classifications_path = tmp_path / "symbol_sector_industry.txt"
    names_path = tmp_path / "company_names.txt"
    monkeypatch.setenv("HEALTHCARE_CLASSIFICATIONS_PATH", str(classifications_path))
    monkeypatch.setenv("HEALTHCARE_COMPANY_NAMES_PATH", str(names_path))

    def read_source():
        values = json.loads(classifications_path.read_text(encoding="utf-8-sig"))
        if not isinstance(values, dict):
            raise discovery.CompanyDiscoveryError("Invalid classification source")
        # The classification fixture represents the SQL table, where symbol is
        # unique; mixed-case duplicate names still exercise name-map rejection.
        rows = {symbol.upper(): {"symbol": symbol.upper(), **value} for symbol, value in values.items() if isinstance(value, dict)}
        return discovery._validate_classifications(list(rows.values()))

    monkeypatch.setattr(discovery, "_read_classifications", read_source)

    def write(classifications, names):
        classifications_path.write_text(json.dumps(classifications), encoding="utf-8-sig")
        names_path.write_text(json.dumps(names), encoding="utf-8-sig")
        return classifications_path, names_path

    return write


@pytest.mark.parametrize("original,expected", [
    ("Vertex Pharmaceuticals", "Vertex Pharmaceuticals"),
    ("Example Therapeutics Holdings, Inc.", "Example Therapeutics"),
    ("Example Therapeutics plc", "Example Therapeutics"),
    ("Example Therapeutics NV", "Example Therapeutics"),
    ("Example Therapeutics SE", "Example Therapeutics"),
    ("Example Therapeutics N.V.", "Example Therapeutics"),
    ("Example Therapeutics S.E.", "Example Therapeutics"),
    ("Example Therapeutics L.L.C.", "Example Therapeutics"),
    ("Johnson & Johnson", "Johnson and Johnson"),
    ("Service Company Research Inc.", "Service Company Research"),
    ("  Example   Therapeutics, Incorporated  ", "Example Therapeutics"),
    (None, ""),
    ("Inc.", ""),
])
def test_company_names_clean_endings_without_dropping_unsuffixed_names(original, expected):
    assert clean_company_name(original) == expected


def test_sponsor_keys_match_case_punctuation_and_ampersands():
    assert sponsor_key("JOHNSON & JOHNSON, INC.") == sponsor_key("Johnson and Johnson")
    assert sponsor_key("Example Therapeutics N.V.") == sponsor_key("example therapeutics NV")


def test_reference_maps_filter_healthcare_and_clean_company_names(reference_files):
    reference_files({
        "BIO": {"sector": "Healthcare", "industry": "Biotechnology"},
        "MED": {"sector": " healthcare ", "industry": "Medical Devices"},
        "PLAIN": {"sector": "Healthcare"},
        "BANK": {"sector": "Financial Services"},
        "MISSINGNAME": {"sector": "Healthcare"},
        "BADCLASS": "Healthcare",
    }, {
        "MED": "Medical Tools & Devices Holdings, Inc.",
        "BIO": "Biologic Therapeutics plc",
        "PLAIN": "No Suffix Therapeutics",
        "BANK": "A Bank Corporation",
        "UNKNOWN": "Unclassified Research",
        "BADCLASS": "Wrong Shape Research",
    })
    rows, skipped = discovery.healthcare_companies()
    assert rows == [
        {"ticker": "BIO", "name": "Biologic Therapeutics plc", "sponsor": "Biologic Therapeutics"},
        {"ticker": "MED", "name": "Medical Tools & Devices Holdings, Inc.", "sponsor": "Medical Tools and Devices"},
        {"ticker": "PLAIN", "name": "No Suffix Therapeutics", "sponsor": "No Suffix Therapeutics"},
    ]
    assert skipped == 0


def test_invalid_and_ambiguous_reference_companies_are_counted_and_skipped(reference_files):
    names = {
        "GOOD": "Distinct Therapeutics",
        "PAIR1": "Research & Development Inc.",
        "PAIR2": "Research and Development plc",
        "case": "First Ticker Research",
        "CASE": "Second Ticker Research",
        "BAD TICKER": "Invalid Ticker Research",
        "EMPTY": "Inc.",
        "TYPE": {"name": "Invalid Name Shape"},
        "LONG": "x" * 201,
    }
    reference_files({ticker: {"sector": "Healthcare"} for ticker in names}, names)
    rows, skipped = discovery.healthcare_companies()
    assert [row["ticker"] for row in rows] == ["GOOD"]
    assert skipped == 8


@pytest.mark.parametrize("problem", ["missing", "malformed", "wrong_classifications_type", "wrong_names_type", "no_healthcare"])
def test_invalid_reference_files_raise_safe_errors(reference_files, monkeypatch, tmp_path, problem):
    classifications_path, names_path = reference_files({"SAFE": {"sector": "Healthcare"}}, {"SAFE": "Safe Therapeutics"})
    if problem == "missing":
        monkeypatch.setenv("HEALTHCARE_COMPANY_NAMES_PATH", str(tmp_path / "private-password-supersecret.txt"))
    elif problem == "malformed":
        names_path.write_text("secret-token-supersecret: not JSON", encoding="utf-8")
    elif problem == "wrong_classifications_type":
        classifications_path.write_text('["supersecret"]', encoding="utf-8")
    elif problem == "wrong_names_type":
        names_path.write_text('["supersecret"]', encoding="utf-8")
    else:
        classifications_path.write_text('{"SAFE": {"sector": "Technology"}}', encoding="utf-8")
    with pytest.raises(discovery.CompanyDiscoveryError) as error:
        discovery.healthcare_companies()
    assert "supersecret" not in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_discovery_upsert_is_idempotent_preserves_history_and_returns_only_source_ids(db, raw, reference_files):
    existing = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    ingest(db, existing, raw)
    existing.last_checked = "2026-09-01T00:00:00+00:00"
    existing.last_error = "old source error"
    db.commit()
    original_ids = set(db.scalars(select(Company.id)))
    original_study_ids = set(db.scalars(select(Study.id)))
    original_snapshot_ids = set(db.scalars(select(Snapshot.id)))
    reference_files({
        "ARVN": {"sector": "Healthcare"}, "FRESH": {"sector": "Healthcare"}, "TECH": {"sector": "Technology"},
    }, {
        "ARVN": "Arvinas Therapeutics Holdings plc", "FRESH": "Fresh Healthcare Inc.", "TECH": "Other Technology Inc.",
    })
    logs = []
    first = discovery.discover_companies(logs.append)
    second = discovery.discover_companies()
    db.expire_all()
    fresh = db.scalar(select(Company).where(Company.ticker == "FRESH"))
    updated = db.get(Company, existing.id)
    assert first["discovered"] == 2
    assert first["created"] == 1
    assert first["updated"] == 1
    assert first["skipped"] == 0
    assert set(first["company_ids"]) == {existing.id, fresh.id}
    assert second["company_ids"] == first["company_ids"]
    assert second["created"] == 0
    assert second["updated"] == 0
    assert fresh.area == "Healthcare"
    assert fresh.sponsor == "Fresh Healthcare"
    assert updated.sponsor == "Arvinas Therapeutics"
    assert updated.last_checked is None
    assert updated.last_error is None
    assert set(db.scalars(select(Company.id))) == original_ids | {fresh.id}
    assert set(db.scalars(select(Study.id))) == original_study_ids
    assert set(db.scalars(select(Snapshot.id))) == original_snapshot_ids
    assert db.scalar(select(Company).where(Company.ticker == "TECH")) is None
    assert logs and "2 companies" in logs[0]


def test_equivalent_sponsor_cleanup_keeps_existing_freshness(db, reference_files):
    company = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    company.last_checked = "2026-09-01T00:00:00+00:00"
    db.commit()
    reference_files({"ARVN": {"sector": "Healthcare"}}, {"ARVN": "Arvinas Incorporated"})
    result = discovery.discover_companies()
    db.refresh(company)
    assert result["updated"] == 1
    assert company.sponsor == "Arvinas"
    assert company.last_checked == "2026-09-01T00:00:00+00:00"


def test_discovery_skips_sponsors_already_owned_by_other_tickers(db, reference_files):
    original = db.scalar(select(Company).where(Company.ticker == "ARVN"))
    reference_files({"DUP": {"sector": "Healthcare"}, "UNIQUE": {"sector": "Healthcare"}}, {
        "DUP": "Arvinas Holdings plc", "UNIQUE": "Unique Healthcare Inc.",
    })
    result = discovery.discover_companies()
    assert result["discovered"] == 1
    assert result["created"] == 1
    assert result["skipped"] == 1
    assert original.id not in result["company_ids"]
    assert db.scalar(select(Company).where(Company.ticker == "DUP")) is None
    assert db.get(Company, result["company_ids"][0]).ticker == "UNIQUE"


def test_all_conflicting_sponsors_fail_without_changing_directory(db, reference_files):
    before = set(db.scalars(select(Company.id)))
    reference_files({"DUP": {"sector": "Healthcare"}}, {"DUP": "Arvinas Holdings plc"})
    with pytest.raises(discovery.CompanyDiscoveryError, match="conflict with existing coverage"):
        discovery.discover_companies()
    assert set(db.scalars(select(Company.id))) == before
