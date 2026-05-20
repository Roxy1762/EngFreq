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
    Index,
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


class LibraryWord(Base):
    """Personal vocabulary library: per-user starred / saved words."""
    __tablename__ = "library_words"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    headword = Column(String(64), nullable=False, index=True)
    lemma = Column(String(64), nullable=False)
    pos = Column(String(16), nullable=True)
    chinese_meaning = Column(Text, nullable=True)
    english_definition = Column(Text, nullable=True)
    example_sentence = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    tags = Column(String(255), nullable=True)            # comma-separated
    source = Column(String(32), nullable=True)           # provider that generated the entry
    source_exam_code = Column(String(12), nullable=True) # original exam if any
    word_level = Column(String(16), nullable=True)
    cefr_level = Column(String(8), nullable=True)
    zipf_score = Column(String(8), nullable=True)        # stored as text to avoid float precision issues
    # User-managed "I've fully learned this" flag. Separate from review box so users can
    # archive a word from the active queue without losing the saved definition.
    mastered = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        # Dedup lookup by (user, headword) hits a unique-ish path on every save;
        # this composite index turns it into a single B-tree probe.
        Index("ix_library_words_user_headword", "user_id", "headword"),
        Index("ix_library_words_user_created", "user_id", "created_at"),
    )


class ReviewItem(Base):
    """Spaced-repetition queue entry (Leitner-style boxes)."""
    __tablename__ = "review_items"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    headword = Column(String(64), nullable=False, index=True)
    library_word_id = Column(Integer, ForeignKey("library_words.id"), nullable=True, index=True)
    box = Column(Integer, nullable=False, default=0)              # 0..4 — Leitner box
    correct_streak = Column(Integer, nullable=False, default=0)
    review_count = Column(Integer, nullable=False, default=0)
    last_reviewed_at = Column(DateTime, nullable=True)
    due_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        # Queue query: due items for one user, oldest first.
        Index("ix_review_items_user_due", "user_id", "due_at"),
        # Single-row lookup by (user, headword): hit on every enroll/grade.
        # Without the composite, SQLite picks the user_id index then filters in
        # memory; with it, we get one B-tree probe per call.
        Index("ix_review_items_user_headword", "user_id", "headword"),
    )


class ReviewEvent(Base):
    """Append-only log of every review grading.

    Powers heatmap, daily-streak, and longer-term retention analytics that the
    point-in-time `ReviewItem` row can't answer (it only stores the latest box).
    """
    __tablename__ = "review_events"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    headword = Column(String(64), nullable=False)
    quality = Column(String(16), nullable=False)            # remembered | fuzzy | forgot
    box_before = Column(Integer, nullable=False, default=0)
    box_after = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        Index("ix_review_events_user_created", "user_id", "created_at"),
    )


class CoachThread(Base):
    """A multi-turn chat thread between a user and the AI vocabulary coach.

    Each thread keeps an ordered list of CoachMessage rows. Threads scope
    auto-context (saved library snapshot, recent quiz performance) so the
    LLM doesn't re-read the user's whole library on every reply.
    """
    __tablename__ = "coach_threads"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(120), nullable=False, default="未命名对话")
    provider = Column(String(32), nullable=True)        # e.g. claude | deepseek | openai
    model = Column(String(64), nullable=True)
    focus_words = Column(Text, nullable=True)           # JSON list of headwords the user is studying
    pinned = Column(Boolean, nullable=False, default=False)
    archived = Column(Boolean, nullable=False, default=False)
    message_count = Column(Integer, nullable=False, default=0)
    total_input_tokens = Column(Integer, nullable=False, default=0)
    total_output_tokens = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_coach_threads_user_updated", "user_id", "updated_at"),
    )


class CoachMessage(Base):
    """A single message in a CoachThread.

    ``role`` matches the OpenAI / Anthropic convention: ``user`` /
    ``assistant`` / ``system``. We log token usage per message so an admin
    can see which threads burnt the most budget.
    """
    __tablename__ = "coach_messages"

    id = Column(Integer, primary_key=True, index=True)
    thread_id = Column(Integer, ForeignKey("coach_threads.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String(16), nullable=False)                # user | assistant | system
    content = Column(Text, nullable=False)
    provider = Column(String(32), nullable=True)             # for assistant messages
    model = Column(String(64), nullable=True)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    latency_ms = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)                       # captured assistant errors
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        Index("ix_coach_messages_thread_created", "thread_id", "created_at"),
    )


class StudyPlan(Base):
    """A single day's adaptive study plan for one user.

    Generated lazily on first request per day. Contains an immutable list of
    items (review queue + new gap suggestions + an optional quiz) so reloading
    on a different device returns the same plan rather than re-shuffling.
    """
    __tablename__ = "study_plans"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plan_date = Column(String(10), nullable=False, index=True)   # ISO YYYY-MM-DD
    review_target = Column(Integer, nullable=False, default=0)
    learn_target = Column(Integer, nullable=False, default=0)
    quiz_target = Column(Integer, nullable=False, default=0)
    completed_review = Column(Integer, nullable=False, default=0)
    completed_learn = Column(Integer, nullable=False, default=0)
    completed_quiz = Column(Integer, nullable=False, default=0)
    accuracy_pct = Column(String(8), nullable=True)         # rolling accuracy snapshot
    streak_at_creation = Column(Integer, nullable=False, default=0)
    insights_json = Column(Text, nullable=True)             # JSON: weak_levels, momentum, etc
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    items = relationship(
        "StudyPlanItem",
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="StudyPlanItem.position",
    )

    __table_args__ = (
        Index("ix_study_plans_user_date", "user_id", "plan_date", unique=True),
    )


class StudyPlanItem(Base):
    """One word (or quiz placeholder) in a StudyPlan."""
    __tablename__ = "study_plan_items"

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("study_plans.id"), nullable=False, index=True)
    headword = Column(String(64), nullable=False)
    lemma = Column(String(64), nullable=True)
    kind = Column(String(16), nullable=False)                # review | learn | quiz
    source = Column(String(16), nullable=True)               # library | gap | review
    word_level = Column(String(16), nullable=True)
    cefr_level = Column(String(8), nullable=True)
    chinese_meaning = Column(Text, nullable=True)
    english_definition = Column(Text, nullable=True)
    example_sentence = Column(Text, nullable=True)
    position = Column(Integer, nullable=False, default=0)
    completed = Column(Boolean, nullable=False, default=False)
    completed_at = Column(DateTime, nullable=True)

    plan = relationship("StudyPlan", back_populates="items")

    __table_args__ = (
        Index("ix_study_plan_items_plan_position", "plan_id", "position"),
    )


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

        # --- library_words table: new "mastered" flag for archiving learnt entries ---
        lib_cols = {row[1] for row in conn.execute("PRAGMA table_info(library_words)").fetchall()}
        if lib_cols:  # table exists
            _add_col_if_missing(conn, "library_words", lib_cols, "mastered", "INTEGER NOT NULL DEFAULT 0")

        conn.commit()
    finally:
        conn.close()


# Identifier allowlist for lightweight SQLite migrations.
# SQLite doesn't support parameter binding for identifiers/DDL, so we validate
# against a hard-coded set before interpolation rather than accepting arbitrary
# strings.
_ALLOWED_TABLES: frozenset[str] = frozenset({
    "exams", "users", "dicts", "app_settings", "library_words", "review_items",
    "review_events", "coach_threads", "coach_messages", "study_plans", "study_plan_items",
})
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
