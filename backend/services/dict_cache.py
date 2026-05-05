"""
On-disk dictionary lookup cache.

All HTTP-based dictionary providers (free_dict, merriam_webster, iciba, …) push
their parsed VocabEntry objects through this layer so that repeated lookups —
across exams, across providers, across users — never re-hit a remote API.

The cache is intentionally small and self-contained:
  * SQLite, single table, source+word composite primary key
  * TTL-bounded reads (default 30 days)
  * Configurable size cap (LRU-style eviction by `last_used_at`)

Counts/score metadata stored on a VocabEntry are *exam-specific*, so we only
cache the dictionary fields (definitions, POS, examples, phonetic, etc.) and
the caller re-attaches per-entry counts.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from backend.config import settings

logger = logging.getLogger(__name__)

_DB_FILENAME = "dict_cache.db"
_DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 30   # 30 days
_DEFAULT_MAX_ROWS = 50_000


# ── Cached dictionary fields ──────────────────────────────────────────────────

@dataclass
class CachedDefinition:
    """Provider-agnostic dictionary fields safe to cache across users/exams."""
    headword: str
    pos: str = ""
    chinese_meaning: str = ""
    english_definition: str = ""
    example_sentence: str = ""
    notes: str = ""
    collocations: str = ""
    confusables: str = ""
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "headword": self.headword,
                "pos": self.pos,
                "chinese_meaning": self.chinese_meaning,
                "english_definition": self.english_definition,
                "example_sentence": self.example_sentence,
                "notes": self.notes,
                "collocations": self.collocations,
                "confusables": self.confusables,
                "extras": self.extras or {},
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> "CachedDefinition":
        data = json.loads(raw)
        return cls(
            headword=data.get("headword", ""),
            pos=data.get("pos", ""),
            chinese_meaning=data.get("chinese_meaning", ""),
            english_definition=data.get("english_definition", ""),
            example_sentence=data.get("example_sentence", ""),
            notes=data.get("notes", ""),
            collocations=data.get("collocations", ""),
            confusables=data.get("confusables", ""),
            extras=data.get("extras", {}) or {},
        )


# ── Storage layer ─────────────────────────────────────────────────────────────

_lock = threading.RLock()
_db_path_cache: Optional[str] = None


def _db_path() -> str:
    global _db_path_cache
    if _db_path_cache is None:
        os.makedirs(settings.ocr_cache_dir, exist_ok=True)
        _db_path_cache = os.path.join(settings.ocr_cache_dir, _DB_FILENAME)
    return _db_path_cache


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dict_cache (
            source       TEXT NOT NULL,
            word         TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            stored_at    REAL NOT NULL,
            last_used_at REAL NOT NULL,
            PRIMARY KEY (source, word)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dict_cache_used ON dict_cache(last_used_at)")
    conn.commit()


def _normalize_key(source: str, word: str) -> tuple[str, str]:
    return source.strip().lower(), word.strip().lower()


# ── Public API ────────────────────────────────────────────────────────────────

def get(source: str, word: str, *, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> Optional[CachedDefinition]:
    if not source or not word:
        return None
    src, w = _normalize_key(source, word)
    now = time.time()
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT payload_json, stored_at FROM dict_cache WHERE source = ? AND word = ?",
                (src, w),
            ).fetchone()
            if row is None:
                return None
            if ttl_seconds and (now - float(row["stored_at"])) > ttl_seconds:
                conn.execute("DELETE FROM dict_cache WHERE source = ? AND word = ?", (src, w))
                conn.commit()
                return None
            try:
                cached = CachedDefinition.from_json(row["payload_json"])
            except Exception as exc:   # noqa: BLE001
                logger.debug("dict_cache: corrupt row for %s/%s — %s", src, w, exc)
                conn.execute("DELETE FROM dict_cache WHERE source = ? AND word = ?", (src, w))
                conn.commit()
                return None
            conn.execute(
                "UPDATE dict_cache SET last_used_at = ? WHERE source = ? AND word = ?",
                (now, src, w),
            )
            conn.commit()
            return cached
        finally:
            conn.close()


def put(source: str, word: str, definition: CachedDefinition) -> None:
    if not source or not word:
        return
    if not (definition.english_definition or definition.chinese_meaning or definition.example_sentence):
        # Don't cache empty results — would mask transient API failures.
        return
    src, w = _normalize_key(source, word)
    now = time.time()
    payload = definition.to_json()
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO dict_cache (source, word, payload_json, stored_at, last_used_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source, word) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    stored_at    = excluded.stored_at,
                    last_used_at = excluded.last_used_at
                """,
                (src, w, payload, now, now),
            )
            conn.commit()
            _maybe_evict(conn)
        finally:
            conn.close()


def _maybe_evict(conn: sqlite3.Connection, max_rows: int = _DEFAULT_MAX_ROWS) -> None:
    count = conn.execute("SELECT COUNT(*) FROM dict_cache").fetchone()[0]
    if count <= max_rows:
        return
    excess = count - max_rows
    conn.execute(
        """
        DELETE FROM dict_cache WHERE rowid IN (
            SELECT rowid FROM dict_cache ORDER BY last_used_at ASC LIMIT ?
        )
        """,
        (excess,),
    )
    conn.commit()


def stats() -> Dict[str, Any]:
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            row = conn.execute("SELECT COUNT(*) AS n FROM dict_cache").fetchone()
            by_source = conn.execute(
                "SELECT source, COUNT(*) AS n FROM dict_cache GROUP BY source ORDER BY n DESC"
            ).fetchall()
            return {
                "total": int(row["n"] or 0),
                "by_source": {r["source"]: int(r["n"]) for r in by_source},
                "db_path": _db_path(),
            }
        finally:
            conn.close()


def clear(source: Optional[str] = None) -> int:
    """Drop all cached entries, or just one provider's. Returns rows deleted."""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            if source:
                cur = conn.execute("DELETE FROM dict_cache WHERE source = ?", (source.strip().lower(),))
            else:
                cur = conn.execute("DELETE FROM dict_cache")
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()
