import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import (
    create_engine,
    text,
    event,
    inspect,
    Column,
    Integer,
    String,
    Text,
    Boolean,
    ForeignKey,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.dialects.mysql import LONGTEXT

load_dotenv()
Path("data").mkdir(exist_ok=True)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/trialdelta.db")
if os.getenv("USE_LOCAL_MYSQL", "false").lower() == "true":
    from sqlalchemy import URL

    password = os.getenv("gptdb")
    if not password:
        raise ValueError("TrialDelta gptdb password environment variable is missing.")
    DATABASE_URL = URL.create(
        "mysql+pymysql",
        username=os.getenv("MYSQL_USER", "gptdb"),
        password=password,
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        database="trialdelta",
        query={"charset": "utf8mb4"},
    )
IS_SQLITE = str(DATABASE_URL).startswith("sqlite")
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    **({"connect_args": {"check_same_thread": False}} if IS_SQLITE else {})
)
if IS_SQLITE:

    @event.listens_for(engine, "connect")
    def sqlite_setup(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")


Session = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Company(Base):
    __tablename__ = "td_companies"
    id = Column(Integer, primary_key=True)
    ticker = Column(String(16), unique=True, nullable=False)
    name = Column(String(200), nullable=False)
    sponsor = Column(String(200), nullable=False)
    area = Column(String(80), default="Healthcare")
    website = Column(String(500), default="")
    last_checked = Column(String(40))
    last_error = Column(String(500))
    coverage_status = Column(String(24), nullable=False, default="active", server_default="active")
    coverage_updated_at = Column(String(40))


class Study(Base):
    __tablename__ = "td_studies"
    id = Column(String(16), primary_key=True)
    company_id = Column(
        Integer, ForeignKey("td_companies.id"), nullable=False, index=True
    )
    title = Column(Text, nullable=False)
    drug = Column(String(500), default="")
    conditions = Column(Text, default="[]")
    targets = Column(Text, default="[]")
    phase = Column(String(80), default="")
    status = Column(String(100), default="")
    primary_completion = Column(String(30))
    completion = Column(String(30))
    last_checked = Column(String(40))


class Snapshot(Base):
    __tablename__ = "td_snapshots"
    id = Column(Integer, primary_key=True)
    study_id = Column(
        String(16), ForeignKey("td_studies.id"), nullable=False, index=True
    )
    captured_at = Column(String(40), default=now, nullable=False)
    source_updated = Column(String(30))
    digest = Column(String(64), nullable=False)
    raw_json = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    normalized = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)


class Evidence(Base):
    __tablename__ = "td_evidence"
    id = Column(Integer, primary_key=True)
    company_id = Column(
        Integer, ForeignKey("td_companies.id"), nullable=False, index=True
    )
    study_id = Column(String(16), ForeignKey("td_studies.id"))
    source_type = Column(String(40), nullable=False)
    title = Column(String(500), nullable=False)
    url = Column(Text, nullable=False)
    published_at = Column(String(30), nullable=False)
    captured_at = Column(String(40), default=now, nullable=False)
    quote = Column(Text, nullable=False)
    category = Column(String(40), nullable=False)
    value = Column(String(1000), nullable=False)
    period_start = Column(String(10))
    period_end = Column(String(10))
    precision = Column(String(30), default="uncertain")
    reason = Column(Text)
    reason_checked = Column(Boolean, default=False)
    review_status = Column(String(20), default="pending", nullable=False)
    reviewed_at = Column(String(40))
    reviewed_by = Column(Integer)
    created_by = Column(Integer, nullable=False)


class Change(Base):
    __tablename__ = "td_changes"
    id = Column(Integer, primary_key=True)
    company_id = Column(
        Integer, ForeignKey("td_companies.id"), nullable=False, index=True
    )
    study_id = Column(String(16), ForeignKey("td_studies.id"))
    category = Column(String(40), nullable=False)
    title = Column(String(500), nullable=False)
    before_value = Column(Text().with_variant(LONGTEXT(), "mysql"))
    after_value = Column(Text().with_variant(LONGTEXT(), "mysql"))
    detected_at = Column(String(40), default=now, nullable=False, index=True)
    source = Column(String(30), nullable=False)
    before_snapshot = Column(Integer, ForeignKey("td_snapshots.id"))
    after_snapshot = Column(Integer, ForeignKey("td_snapshots.id"))
    before_evidence = Column(Integer, ForeignKey("td_evidence.id"))
    after_evidence = Column(Integer, ForeignKey("td_evidence.id"))
    reviewed = Column(Boolean, default=False)
    review_status = Column(String(20), default="pending")
    review_note = Column(Text)
    reviewed_by = Column(Integer)
    reviewed_at = Column(String(40))


class SourceCandidate(Base):
    __tablename__ = "td_source_inbox"
    id = Column(Integer, primary_key=True)
    company_id = Column(
        Integer, ForeignKey("td_companies.id"), nullable=False, index=True
    )
    title = Column(Text, nullable=False)
    url = Column(Text, nullable=False)
    source = Column(String(200))
    published_at = Column(String(40))
    discovered_at = Column(String(40), default=now)
    identity = Column(String(64), nullable=False, unique=True)
    status = Column(String(20), nullable=False, default="unreviewed")


class TargetActionDate(Base):
    """Deterministic filing matches and explicitly entered administrator dates.

    Source metadata is retained when present. The content hash and normalized
    text offsets identify the precise evidence even if the source has no key.
    Symbols need not already be in the application's company coverage.
    """

    __tablename__ = "td_target_action_dates"
    id = Column(Integer, primary_key=True)
    identity = Column(String(64), nullable=False, unique=True)
    entry_method = Column(String(16), nullable=False, default="filing", server_default="filing")
    entered_by = Column(Integer)
    symbol = Column(String(255), nullable=False, index=True)
    form_type = Column(String(64), nullable=False, default="")
    filing_sha256 = Column(String(64), nullable=False, index=True)
    accession_no = Column(Text)
    filed_at = Column(String(40))
    filing_link = Column(Text)
    tad = Column(String(10), index=True)
    precision = Column(String(10), nullable=False)
    status = Column(String(20), nullable=False, index=True)
    matched_text = Column(Text, nullable=False, default="")
    reason = Column(Text, nullable=False, default="")
    start = Column(Integer, nullable=False)
    end = Column(Integer, nullable=False)
    context_start = Column(Integer, nullable=False)
    context_end = Column(Integer, nullable=False)
    context = Column(Text, nullable=False)
    imported_at = Column(String(40), default=now, nullable=False)


class TargetActionDateEdit(Base):
    """Append-only administrator corrections; original evidence stays intact.

    Identity, rather than a row-ID foreign key, keeps correction history after
    an explicit delete and lets a later import restore the same corrected match.
    """

    __tablename__ = "td_target_action_date_edits"
    id = Column(Integer, primary_key=True)
    target_identity = Column(String(64), nullable=False, index=True)
    revision_token = Column(String(64), nullable=False, unique=True)
    symbol = Column(String(255), nullable=False)
    tad = Column(String(10), nullable=False)
    precision = Column(String(10), nullable=False)
    source_url = Column(Text, nullable=False, default="")
    notes = Column(Text, nullable=False, default="")
    entered_by = Column(Integer, nullable=False)
    edited_at = Column(String(40), default=now, nullable=False)
    __table_args__ = (Index("ix_td_tad_edits_group", "symbol", "tad", "precision"),)


class User(Base):
    __tablename__ = "td_users"
    id = Column(Integer, primary_key=True)
    email = Column(String(254), unique=True, nullable=False)
    password_hash = Column(String(300), nullable=False)
    is_admin = Column(Boolean, default=False)
    digest_enabled = Column(Boolean, default=False)
    created_at = Column(String(40), default=now)


class Login(Base):
    __tablename__ = "td_sessions"
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("td_users.id"), nullable=False)
    expires_at = Column(String(40), nullable=False)


class AccountProfile(Base):
    """Additive metadata; existing users are preserved during upgrades."""

    __tablename__ = "td_account_profiles"
    user_id = Column(Integer, ForeignKey("td_users.id"), primary_key=True)
    display_name = Column(String(100), nullable=False, default="")
    email_verified_at = Column(String(40))


class AccountToken(Base):
    __tablename__ = "td_account_tokens"
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("td_users.id"), nullable=False, index=True)
    purpose = Column(String(20), nullable=False)
    expires_at = Column(String(40), nullable=False, index=True)
    created_at = Column(String(40), default=now, nullable=False)


class AuthRateLimit(Base):
    """Atomic, shared rate-limit counters for multiple application workers."""

    __tablename__ = "td_auth_rate_limits"
    bucket_hash = Column(String(64), primary_key=True)
    window_started_at = Column(Integer, nullable=False, index=True)
    attempts = Column(Integer, nullable=False, default=0)

class Follow(Base):
    __tablename__ = "td_follows"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("td_users.id"), nullable=False, index=True)
    kind = Column(String(20), nullable=False)
    value = Column(String(200), nullable=False)
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "value", name="uq_td_follow"),
    )


class DigestDelivery(Base):
    __tablename__ = "td_digest_deliveries"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("td_users.id"), nullable=False)
    day = Column(String(10), nullable=False)
    sent_at = Column(String(40))
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_td_digest_day"),)


class Job(Base):
    __tablename__ = "td_jobs"
    id = Column(Integer, primary_key=True)
    started_at = Column(String(40), default=now)
    finished_at = Column(String(40))
    status = Column(String(20), default="running")
    summary = Column(Text, default="")


class BulkCheckpoint(Base):
    """Frozen query scope and next-page cursor, committed with each saved page."""

    __tablename__ = "td_bulk_checkpoints"
    job_id = Column(Integer, ForeignKey("td_jobs.id"), primary_key=True)
    payload = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)


evidence_change_index = Index(
    "uq_td_change_after_evidence", Change.after_evidence, unique=True
)


_MYSQL_EVIDENCE_COLUMNS = {
    "td_snapshots": ("raw_json", "normalized"),
    "td_changes": ("before_value", "after_value"),
}


def _mysql_evidence_upgrades(connection):
    """Plan only widening changes, retaining each column's encoding/nullability."""
    rows = connection.execute(text(
        "SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name, "
        "DATA_TYPE AS data_type, IS_NULLABLE AS is_nullable, "
        "CHARACTER_SET_NAME AS charset, COLLATION_NAME AS collation "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME IN ('td_snapshots', 'td_changes')"
    )).mappings()
    changes = {}
    for row in rows:
        table, column = row["table_name"], row["column_name"]
        if column not in _MYSQL_EVIDENCE_COLUMNS.get(table, ()):
            continue
        if row["data_type"].lower() == "longtext":
            continue
        if row["data_type"].lower() not in ("tinytext", "text", "mediumtext"):
            raise RuntimeError("Unexpected evidence column type; database upgrade requires review.")
        # Identifiers come from fixed application tables and server metadata.
        if not all(re.fullmatch(r"[A-Za-z0-9_]+", row[key] or "") for key in ("charset", "collation")):
            raise RuntimeError("Unexpected evidence column encoding; database upgrade requires review.")
        nullable = "NULL" if row["is_nullable"] == "YES" else "NOT NULL"
        changes.setdefault(table, []).append(
            f"MODIFY COLUMN `{column}` LONGTEXT CHARACTER SET {row['charset']} "
            f"COLLATE {row['collation']} {nullable}"
        )
    return changes


def upgrade_mysql_evidence_storage():
    """Upgrade existing MySQL evidence columns; create_all cannot alter them.

    The collection lock prevents a page writer from overlapping the upgrade.
    Rechecking after acquiring it makes concurrent app startups idempotent.
    MySQL DDL commits implicitly, so each widening must be safe to retry alone.
    """
    if engine.dialect.name != "mysql":
        return []
    if engine.url.database != "trialdelta":
        raise RuntimeError("TrialDelta writes are restricted to the trialdelta schema.")
    with engine.connect() as connection:
        if not _mysql_evidence_upgrades(connection):
            return []
        acquired = connection.scalar(text("SELECT GET_LOCK('trialdelta_registry_sync', 10)"))
        if acquired != 1:
            raise RuntimeError("Database storage upgrade is waiting for collection. Retry after the current run finishes.")
        lock_timeout = None
        try:
            lock_timeout = connection.scalar(text("SELECT @@SESSION.lock_wait_timeout"))
            connection.exec_driver_sql("SET SESSION lock_wait_timeout = 10")
            changes = _mysql_evidence_upgrades(connection)
            for table, clauses in changes.items():
                connection.exec_driver_sql(f"ALTER TABLE `{table}` " + ", ".join(clauses))
            return list(changes)
        finally:
            try:
                if lock_timeout is not None:
                    connection.exec_driver_sql(f"SET SESSION lock_wait_timeout = {int(lock_timeout)}")
            finally:
                connection.execute(text("SELECT RELEASE_LOCK('trialdelta_registry_sync')"))


_TAD_ADDITIVE_COLUMNS = {
    "entry_method": "VARCHAR(16) NOT NULL DEFAULT 'filing'",
    "entered_by": "INTEGER NULL",
}


def _missing_tad_columns(connection):
    inspector = inspect(connection)
    if not inspector.has_table("td_target_action_dates"):
        return []
    existing = {column["name"] for column in inspector.get_columns("td_target_action_dates")}
    return [name for name in _TAD_ADDITIVE_COLUMNS if name not in existing]


def upgrade_target_action_date_storage():
    """Add manual-entry provenance without rewriting existing filing evidence.

    SQLite's write transaction and MySQL's named lock serialize app startups.
    Each addition is independently retryable because MySQL DDL commits itself.
    ``entered_by`` retains the operator ID even if that account is later removed,
    matching the application's existing evidence audit columns.
    """
    dialect = engine.dialect.name
    if dialect == "mysql" and engine.url.database != "trialdelta":
        raise RuntimeError("TrialDelta writes are restricted to the trialdelta schema.")
    if dialect not in ("sqlite", "mysql"):
        return []
    with engine.connect() as connection:
        if not _missing_tad_columns(connection):
            return []
        if dialect == "sqlite":
            # SQLite's legacy driver does not start a database transaction for
            # the introspection SELECT above. Reserve the writer before recheck.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                changes = _missing_tad_columns(connection)
                for name in changes:
                    connection.exec_driver_sql(
                        f"ALTER TABLE td_target_action_dates ADD COLUMN {name} {_TAD_ADDITIVE_COLUMNS[name]}"
                    )
                connection.commit()
                return changes
            except Exception:
                connection.rollback()
                raise
        acquired = connection.scalar(text("SELECT GET_LOCK('trialdelta_tad_storage', 10)"))
        if acquired != 1:
            raise RuntimeError("Target action date storage upgrade is busy. Retry after the current upgrade finishes.")
        lock_timeout = None
        try:
            lock_timeout = connection.scalar(text("SELECT @@SESSION.lock_wait_timeout"))
            connection.exec_driver_sql("SET SESSION lock_wait_timeout = 10")
            changes = _missing_tad_columns(connection)
            for name in changes:
                connection.exec_driver_sql(
                    f"ALTER TABLE `td_target_action_dates` ADD COLUMN `{name}` {_TAD_ADDITIVE_COLUMNS[name]}"
                )
            return changes
        finally:
            try:
                if lock_timeout is not None:
                    connection.exec_driver_sql(f"SET SESSION lock_wait_timeout = {int(lock_timeout)}")
            finally:
                connection.execute(text("SELECT RELEASE_LOCK('trialdelta_tad_storage')"))


_COVERAGE_ADDITIVE_COLUMNS = {
    "coverage_status": "VARCHAR(24) NOT NULL DEFAULT 'active'",
    "coverage_updated_at": "VARCHAR(40) NULL",
}


def _missing_coverage_columns(connection):
    inspector = inspect(connection)
    if not inspector.has_table("td_companies"):
        return []
    existing = {column["name"] for column in inspector.get_columns("td_companies")}
    return [name for name in _COVERAGE_ADDITIVE_COLUMNS if name not in existing]


def upgrade_company_coverage_storage():
    """Add coverage lifecycle metadata without deleting companies or evidence."""
    dialect = engine.dialect.name
    if dialect == "mysql" and engine.url.database != "trialdelta":
        raise RuntimeError("TrialDelta writes are restricted to the trialdelta schema.")
    if dialect not in ("sqlite", "mysql"):
        return []
    with engine.connect() as connection:
        if not _missing_coverage_columns(connection):
            return []
        if dialect == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                changes = _missing_coverage_columns(connection)
                for name in changes:
                    connection.exec_driver_sql(
                        f"ALTER TABLE td_companies ADD COLUMN {name} {_COVERAGE_ADDITIVE_COLUMNS[name]}"
                    )
                connection.commit()
                return changes
            except Exception:
                connection.rollback()
                raise
        acquired = connection.scalar(text("SELECT GET_LOCK('trialdelta_registry_sync', 10)"))
        if acquired != 1:
            raise RuntimeError("Company coverage upgrade is waiting for collection. Retry after the current run finishes.")
        lock_timeout = None
        try:
            lock_timeout = connection.scalar(text("SELECT @@SESSION.lock_wait_timeout"))
            connection.exec_driver_sql("SET SESSION lock_wait_timeout = 10")
            changes = _missing_coverage_columns(connection)
            for name in changes:
                connection.exec_driver_sql(
                    f"ALTER TABLE `td_companies` ADD COLUMN `{name}` {_COVERAGE_ADDITIVE_COLUMNS[name]}"
                )
            return changes
        finally:
            try:
                if lock_timeout is not None:
                    connection.exec_driver_sql(f"SET SESSION lock_wait_timeout = {int(lock_timeout)}")
            finally:
                connection.execute(text("SELECT RELEASE_LOCK('trialdelta_registry_sync')"))


def initialize():
    if engine.dialect.name == "mysql" and engine.url.database != "trialdelta":
        raise RuntimeError("TrialDelta writes are restricted to the trialdelta schema.")
    Base.metadata.create_all(engine)
    upgrade_company_coverage_storage()
    upgrade_target_action_date_storage()
    upgrade_mysql_evidence_storage()
    evidence_change_index.create(engine, checkfirst=True)


def get_db():
    with Session() as db:
        yield db
