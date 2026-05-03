"""SQLite database models and session management (SQLAlchemy)."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event as _sa_event,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DB_PATH = os.environ.get("DB_PATH", "app.db")


def _set_sqlite_pragmas(dbapi_conn, _connection_record):
    """Enable WAL mode and a short busy timeout on every new connection.

    WAL (Write-Ahead Logging) allows readers and writers to coexist without
    blocking each other — critical for FastAPI's async request handlers running
    alongside background analysis tasks.  The busy timeout prevents the
    "database is locked" OperationalError when a second write arrives while
    the first is still in progress.
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")   # ms
    cursor.execute("PRAGMA synchronous=NORMAL")   # safe with WAL; faster than FULL
    cursor.close()


engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)
_sa_event.listen(engine, "connect", _set_sqlite_pragmas)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(128), nullable=False)
    is_admin = Column(Boolean, default=False)
    email = Column(String(128), nullable=True)
    display_name = Column(String(64), nullable=True)
    preferred_provider = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    exams = relationship("Exam", back_populates="user", cascade="all, delete-orphan")
    dicts = relationship("Dict", back_populates="user", cascade="all, delete-orphan")


class Exam(Base):
    __tablename__ = "exams"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    task_id = Column(String(64), nullable=False)
    filename = Column(String(256), nullable=False)
    exam_code = Column(String(12), unique=True, nullable=False, index=True)
    result_json = Column(Text, nullable=False)
    raw_parse_backend = Column(String(32), nullable=False, default="local")
    raw_parse_result_json = Column(Text, nullable=True)
    is_combined = Column(Boolean, default=False)         # True for multi-exam aggregations
    source_exam_codes = Column(Text, nullable=True)      # JSON array of source exam codes
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="exams")
    dicts = relationship("Dict", back_populates="exam")


class Dict(Base):
    __tablename__ = "dicts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    exam_id = Column(Integer, ForeignKey("exams.id"), nullable=True)
    task_id = Column(String(64), nullable=True)
    filename = Column(String(256), nullable=False, default="")
    dict_code = Column(String(12), unique=True, nullable=False, index=True)
    vocab_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="dicts")
    exam = relationship("Exam", back_populates="dicts")


class AppSetting(Base):
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(64), unique=True, nullable=False, index=True)
    value_json = Column(Text, nullable=False, default="{}")
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


def get_db():
    """FastAPI dependency: yield a DB session, close on exit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables if they don't exist."""
    Base.metadata.create_all(bind=engine)
    _ensure_schema_columns()


def _ensure_schema_columns() -> None:
    """Add new columns to older SQLite databases without dropping data."""
    conn = sqlite3.connect(DB_PATH)
    try:
        # --- exams table ---
        exam_cols = {row[1] for row in conn.execute("PRAGMA table_info(exams)").fetchall()}
        _add_col_if_missing(conn, "exams", exam_cols, "raw_parse_backend", "TEXT NOT NULL DEFAULT 'local'")
        _add_col_if_missing(conn, "exams", exam_cols, "raw_parse_result_json", "TEXT")
        _add_col_if_missing(conn, "exams", exam_cols, "is_combined", "INTEGER NOT NULL DEFAULT 0")
        _add_col_if_missing(conn, "exams", exam_cols, "source_exam_codes", "TEXT")

        # --- users table ---
        user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        _add_col_if_missing(conn, "users", user_cols, "email", "TEXT")
        _add_col_if_missing(conn, "users", user_cols, "display_name", "TEXT")
        _add_col_if_missing(conn, "users", user_cols, "preferred_provider", "TEXT")

        conn.commit()
    finally:
        conn.close()


# Identifier allowlist for lightweight SQLite migrations.
# SQLite doesn't support parameter binding for identifiers/DDL, so we validate
# against a hard-coded set before interpolation rather than accepting arbitrary
# strings.
_ALLOWED_TABLES: frozenset[str] = frozenset({"exams", "users", "dicts", "app_settings"})
_ALLOWED_COLUMN_DEFINITIONS: frozenset[str] = frozenset({
    "TEXT", "TEXT NOT NULL", "TEXT NOT NULL DEFAULT 'local'",
    "INTEGER", "INTEGER NOT NULL DEFAULT 0", "INTEGER NOT NULL DEFAULT 1",
})


def _is_safe_identifier(name: str) -> bool:
    """Conservative: letters, digits, underscore only; must start with letter."""
    if not name or not name[0].isalpha():
        return False
    return all(ch.isalnum() or ch == "_" for ch in name)


def _add_col_if_missing(conn, table: str, existing_cols: set, col: str, definition: str) -> None:
    if col in existing_cols:
        return
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"Refusing ALTER TABLE on unknown table: {table!r}")
    if not _is_safe_identifier(col):
        raise ValueError(f"Refusing ALTER TABLE with unsafe column name: {col!r}")
    if definition not in _ALLOWED_COLUMN_DEFINITIONS:
        raise ValueError(f"Refusing ALTER TABLE with unrecognized definition: {definition!r}")
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
