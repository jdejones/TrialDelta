"""Synchronize coverage from read-only market classifications and owned names.

Only TrialDelta's company directory is changed. The source database and the
externally maintained company-name file are never modified here.
"""

import json
import os
import re
from collections import Counter
from pathlib import Path

from sqlalchemy import create_engine, select, text

from .company_names import clean_company_name, sponsor_key
from .db import Company, Session, engine, now

REFERENCE_DIRECTORY = Path(r"E:\Market Research\Dataset\Fundamental Data")


class CompanyDiscoveryError(ValueError):
    """A safe source/configuration error that must leave saved coverage intact."""


def _source_engine():
    source_url = os.getenv("SOURCE_DATABASE_URL")
    if source_url:
        return create_engine(source_url, pool_pre_ping=True)
    if engine.dialect.name == "mysql":
        return engine
    raise CompanyDiscoveryError("Market classifications are not configured. Set SOURCE_DATABASE_URL to a source with SELECT access to stocks.symbol_sector_industry.")


def _read_classifications():
    source = None
    try:
        source = _source_engine()
        with source.connect() as connection:
            # Read the WHOLE table in one statement. Absence from Healthcare
            # alone means reclassification, not delisting.
            # The external updater must publish complete snapshots atomically;
            # a nonempty partial rebuild cannot be distinguished from removals.
            rows = connection.execute(text(
                "SELECT symbol, sector, industry FROM stocks.symbol_sector_industry"
            )).mappings().all()
        return _validate_classifications(rows)
    except CompanyDiscoveryError:
        raise
    except Exception:
        raise CompanyDiscoveryError("Market classifications could not be read. Existing coverage was retained; check source access and retry.") from None
    finally:
        if source is not None and source is not engine:
            source.dispose()


def _validate_classifications(rows):
    classifications = {}
    for row in rows:
        symbol, sector, industry = row.get("symbol"), row.get("sector"), row.get("industry")
        if not isinstance(symbol, str) or not symbol.strip() or not isinstance(sector, str) or not sector.strip() or not isinstance(industry, (str, type(None))):
            raise CompanyDiscoveryError("Market classifications are incomplete or invalid. Existing coverage was retained.")
        ticker = symbol.strip().upper()
        if ticker in classifications:
            raise CompanyDiscoveryError("Market classifications contain duplicate symbols. Existing coverage was retained.")
        classifications[ticker] = {"sector": sector.strip(), "industry": industry}
    # An empty upstream rebuild must not retire the whole directory.
    if not classifications or not any(row["sector"].casefold() == "healthcare" for row in classifications.values()):
        raise CompanyDiscoveryError("Market classifications contain no Healthcare companies. Existing coverage was retained; retry after the source refresh finishes.")
    return classifications


def _read_names():
    path = Path(os.getenv("HEALTHCARE_COMPANY_NAMES_PATH") or REFERENCE_DIRECTORY / "company_names.txt")
    try:
        names = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise CompanyDiscoveryError("The company-name reference file is unavailable or invalid. Check HEALTHCARE_COMPANY_NAMES_PATH; existing coverage was retained.") from None
    if not isinstance(names, dict) or not names:
        raise CompanyDiscoveryError("The company-name reference file must contain a nonempty JSON object keyed by ticker. Existing coverage was retained.")
    return names


def _healthcare_records(classifications, names):
    # Known limitation / TODO: names are maintained externally. A newly listed
    # symbol absent from that dataset is skipped, as in the original design.
    # Revisit an authoritative symbol-to-name resolver when one is available;
    # do not invent names, add an online fallback, or surface this note in UI.
    records, skipped = [], 0
    for ticker, name in names.items():
        if not isinstance(ticker, str):
            continue
        classification = classifications.get(ticker.upper())
        if classification is None or classification["sector"].casefold() != "healthcare":
            continue
        sponsor = clean_company_name(name)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.^_-]{0,15}", ticker) or not isinstance(name, str) or not name.strip() or len(name) > 200 or not sponsor or len(sponsor) > 200:
            skipped += 1
            continue
        records.append({"ticker": ticker.upper(), "name": name.strip(), "sponsor": sponsor})
    tickers = Counter(row["ticker"] for row in records)
    sponsors = Counter(sponsor_key(row["sponsor"]) for row in records)
    accepted = [row for row in records if tickers[row["ticker"]] == 1 and sponsors[sponsor_key(row["sponsor"])] == 1]
    skipped += len(records) - len(accepted)
    return sorted(accepted, key=lambda row: row["ticker"]), skipped


def healthcare_companies():
    """Return valid Healthcare ticker/name/sponsor triples, without writes."""
    records, skipped = _healthcare_records(_read_classifications(), _read_names())
    if not records:
        raise CompanyDiscoveryError("No unambiguous Healthcare companies were found in the company-name reference file.")
    return records, skipped


def _unambiguous_updates(records, existing):
    """Reject conflicts against the resulting directory, including kept names.

    Rejecting one proposed rename can expose a collision with its old sponsor,
    so repeat until the remaining updates have stable ownership.
    """
    pending = {record["ticker"]: record for record in records}
    while pending:
        final = {ticker: row.sponsor for ticker, row in existing.items()}
        final.update({ticker: row["sponsor"] for ticker, row in pending.items()})
        counts = Counter(sponsor_key(sponsor) for sponsor in final.values())
        conflicts = [ticker for ticker, row in pending.items() if counts[sponsor_key(row["sponsor"])] > 1]
        if not conflicts:
            break
        for ticker in conflicts:
            del pending[ticker]
    return list(pending.values()), len(records) - len(pending)


def discover_companies(emit=None):
    """Refresh membership atomically; caller holds the registry collection lock.

    Keep every company and all evidence. Missing symbols become delisted;
    present symbols outside Healthcare become out_of_sector, not delisted.
    """
    emit = emit or (lambda message: None)
    classifications = _read_classifications()
    records, skipped = _healthcare_records(classifications, _read_names())
    created, updated = 0, 0
    transitions = {"delisted": 0, "out_of_sector": 0, "reactivated": 0}
    timestamp = now()
    with Session() as db:
        existing = {row.ticker: row for row in db.scalars(select(Company))}
        # Preserve conservative ownership even for a delisted company's trials.
        records, conflicts = _unambiguous_updates(records, existing)
        skipped += conflicts
        for record in records:
            company = existing.get(record["ticker"])
            if company is None:
                company = Company(**record, area="Healthcare")
                db.add(company)
                existing[company.ticker] = company
                created += 1
            elif company.name != record["name"] or company.sponsor != record["sponsor"]:
                if sponsor_key(company.sponsor) != sponsor_key(record["sponsor"]):
                    company.last_checked = None
                    company.last_error = None
                company.name, company.sponsor = record["name"], record["sponsor"]
                updated += 1
        db.flush()
        company_ids = []
        for ticker, company in existing.items():
            classification = classifications.get(ticker.upper())
            status = "delisted" if classification is None else (
                "active" if classification["sector"].casefold() == "healthcare" else "out_of_sector"
            )
            if status != company.coverage_status:
                transitions["reactivated" if status == "active" else status] += 1
            company.coverage_status, company.coverage_updated_at = status, timestamp
            if status == "active":
                company_ids.append(company.id)
        # Preserve previous coverage on an unusable name/sponsor mapping.
        if not company_ids:
            raise CompanyDiscoveryError("No source companies could be matched without a conflict with existing coverage. Existing coverage was retained.")
        db.commit()
    result = {"status": "complete", "discovered": len(company_ids), "created": created, "updated": updated, "skipped": skipped, "company_ids": sorted(company_ids), **transitions}
    emit(f"Healthcare coverage refreshed: {len(company_ids)} companies, {created} added, {updated} updated, {transitions['reactivated']} reactivated, {transitions['delisted']} tagged delisted, {transitions['out_of_sector']} outside Healthcare, {skipped} invalid or ambiguous names skipped.")
    return result

