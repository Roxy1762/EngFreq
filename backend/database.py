"""SQLite database models and session management (SQLAlchemy)."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DB_PATH = os.environ.get("DB_PATH", "app.db")
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)
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
    created_at = Column(DateTime, default=datetime.utcnow)

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
    created_at = Column(DateTime, default=datetime.utcnow)

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
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="dicts")
    exam = relationship("Exam", back_populates="dicts")


class AppSetting(Base):
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(64), unique=True, nullable=False, index=True)
    value_json = Column(Text, nullable=False, default="{}")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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


def _add_col_if_missing(conn, table: str, existing_cols: set, col: str, definition: str) -> None:
    if col not in existing_cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
