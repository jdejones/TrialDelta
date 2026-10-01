import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import (
    create_engine,
    event,
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

load_dotenv()
Path("data").mkdir(exist_ok=True)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/trialdelta.db")
if os.getenv("USE_LOCAL_MYSQL", "false").lower() == "true":
    from sqlalchemy import URL

    DATABASE_URL = URL.create(
        "mysql+pymysql",
        username=os.getenv("MYSQL_USER", "root"),
        password=os.environ["DATABASE_PASSWORD"],
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
    area = Column(String(80), default="Oncology")
    website = Column(String(500), default="")
    last_checked = Column(String(40))
    last_error = Column(String(500))


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
    raw_json = Column(Text, nullable=False)
    normalized = Column(Text, nullable=False)


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
    before_value = Column(Text)
    after_value = Column(Text)
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


evidence_change_index = Index(
    "uq_td_change_after_evidence", Change.after_evidence, unique=True
)


def initialize():
    if engine.dialect.name == "mysql" and engine.url.database != "trialdelta":
        raise RuntimeError("TrialDelta writes are restricted to the trialdelta schema.")
    if engine.dialect.name == "mysql":
        from sqlalchemy.dialects.mysql import LONGTEXT

        Snapshot.__table__.c.raw_json.type = LONGTEXT()
    Base.metadata.create_all(engine)
    evidence_change_index.create(engine, checkfirst=True)


def get_db():
    with Session() as db:
        yield db
