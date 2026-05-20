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
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from backend.config import settings

logger = logging.getLogger(__name__)

_DB_FILENAME = "dict_cache.db"
_DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 30   # 30 days
_DEFAULT_MAX_ROWS = 50_000

# In-memory LRU front cache. Hot words (e.g. "the", "and", or whatever the
# user looks up repeatedly during a session) used to hit SQLite on every
# request — this layer collapses them to a dict lookup.
_MEMORY_CACHE_SIZE = 2048
_memory_cache: "OrderedDict[Tuple[str, str], Tuple[float, CachedDefinition]]" = OrderedDict()
_memory_lock = threading.RLock()

# Negative cache: words we've already determined the upstream API doesn't
# know. Short TTL so we don't re-spam the API for the same misses, but not
# permanent because some providers (especially iCIBA) occasionally return
# transient failures we don't want to remember forever.
_NEGATIVE_TTL_SECONDS = 60 * 60 * 6   # 6 hours
_NEGATIVE_CACHE_SIZE = 4096
_negative_cache: "OrderedDict[Tuple[str, str], float]" = OrderedDict()


# Track on-disk row count in process so we don't run SELECT COUNT(*) on every
# put() call. Initialised lazily on first access; falls back to a re-count
# when the cache is cleared from another path.
_row_count: Optional[int] = None
_row_count_lock = threading.RLock()


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


def _memory_get(key: Tuple[str, str], ttl_seconds: int) -> Optional[CachedDefinition]:
    with _memory_lock:
        entry = _memory_cache.get(key)
        if entry is None:
            return None
        stored_at, cached = entry
        if ttl_seconds and (time.time() - stored_at) > ttl_seconds:
            _memory_cache.pop(key, None)
            return None
        _memory_cache.move_to_end(key)
        return cached


def _memory_put(key: Tuple[str, str], stored_at: float, cached: CachedDefinition) -> None:
    with _memory_lock:
        _memory_cache[key] = (stored_at, cached)
        _memory_cache.move_to_end(key)
        while len(_memory_cache) > _MEMORY_CACHE_SIZE:
            _memory_cache.popitem(last=False)


def _memory_drop(key: Tuple[str, str]) -> None:
    with _memory_lock:
        _memory_cache.pop(key, None)


def _negative_hit(key: Tuple[str, str]) -> bool:
    with _memory_lock:
        ts = _negative_cache.get(key)
        if ts is None:
            return False
        if time.time() - ts > _NEGATIVE_TTL_SECONDS:
            _negative_cache.pop(key, None)
            return False
        _negative_cache.move_to_end(key)
        return True


def _negative_mark(key: Tuple[str, str]) -> None:
    with _memory_lock:
        _negative_cache[key] = time.time()
        _negative_cache.move_to_end(key)
        while len(_negative_cache) > _NEGATIVE_CACHE_SIZE:
            _negative_cache.popitem(last=False)


def is_known_miss(source: str, word: str) -> bool:
    """True if we've recently determined this (source, word) returns no data.

    Used by HTTP providers to skip the network round-trip for words the API
    has already 404ed on in the recent past. Decays after a short TTL so
    transient failures don't lock us out of a fix.
    """
    if not source or not word:
        return False
    return _negative_hit(_normalize_key(source, word))


def mark_miss(source: str, word: str) -> None:
    """Remember that this (source, word) returned no data."""
    if not source or not word:
        return
    _negative_mark(_normalize_key(source, word))


# ── Public API ────────────────────────────────────────────────────────────────

def get(source: str, word: str, *, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> Optional[CachedDefinition]:
    if not source or not word:
        return None
    key = _normalize_key(source, word)

    # In-memory layer first — avoids the SQLite trip entirely for hot lookups.
    cached_mem = _memory_get(key, ttl_seconds)
    if cached_mem is not None:
        return cached_mem

    src, w = key
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
                _adjust_row_count(-1)
                return None
            try:
                cached = CachedDefinition.from_json(row["payload_json"])
            except Exception as exc:   # noqa: BLE001
                logger.debug("dict_cache: corrupt row for %s/%s — %s", src, w, exc)
                conn.execute("DELETE FROM dict_cache WHERE source = ? AND word = ?", (src, w))
                conn.commit()
                _adjust_row_count(-1)
                return None
            conn.execute(
                "UPDATE dict_cache SET last_used_at = ? WHERE source = ? AND word = ?",
                (now, src, w),
            )
            conn.commit()
            _memory_put(key, float(row["stored_at"]), cached)
            return cached
        finally:
            conn.close()


def put(source: str, word: str, definition: CachedDefinition) -> None:
    if not source or not word:
        return
    if not (definition.english_definition or definition.chinese_meaning or definition.example_sentence):
        # Don't cache empty results — would mask transient API failures.
        return
    key = _normalize_key(source, word)
    src, w = key
    now = time.time()
    payload = definition.to_json()
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            existed = conn.execute(
                "SELECT 1 FROM dict_cache WHERE source = ? AND word = ?",
                (src, w),
            ).fetchone() is not None
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
            if not existed:
                _adjust_row_count(+1)
                _maybe_evict(conn)
            _memory_put(key, now, definition)
        finally:
            conn.close()


def _current_row_count(conn: sqlite3.Connection) -> int:
    global _row_count
    with _row_count_lock:
        if _row_count is None:
            _row_count = int(conn.execute("SELECT COUNT(*) FROM dict_cache").fetchone()[0] or 0)
        return _row_count


def _adjust_row_count(delta: int) -> None:
    global _row_count
    with _row_count_lock:
        if _row_count is None:
            return   # not initialised yet; next access will count fresh
        _row_count = max(0, _row_count + delta)


def _reset_row_count() -> None:
    global _row_count
    with _row_count_lock:
        _row_count = None


def _maybe_evict(conn: sqlite3.Connection, max_rows: int = _DEFAULT_MAX_ROWS) -> None:
    """Evict LRU entries when the in-process count exceeds the cap.

    The previous version ran SELECT COUNT(*) on every put() — O(n) per insert
    when the table reaches significant size. The in-process counter keeps the
    common path O(1); we only run the eviction DELETE when we cross the cap,
    and prune in larger batches (10% headroom) so the next 5000 inserts don't
    re-trigger eviction.
    """
    count = _current_row_count(conn)
    if count <= max_rows:
        return
    # Prune to 90% of the cap so we don't churn on every following insert.
    target = int(max_rows * 0.9)
    excess = max(1, count - target)
    deleted = conn.execute(
        """
        DELETE FROM dict_cache WHERE rowid IN (
            SELECT rowid FROM dict_cache ORDER BY last_used_at ASC LIMIT ?
        )
        """,
        (excess,),
    ).rowcount or 0
    conn.commit()
    _adjust_row_count(-int(deleted))


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
                src = source.strip().lower()
                cur = conn.execute("DELETE FROM dict_cache WHERE source = ?", (src,))
                # Drop matching in-memory entries for this source
                with _memory_lock:
                    for key in list(_memory_cache.keys()):
                        if key[0] == src:
                            _memory_cache.pop(key, None)
                    for key in list(_negative_cache.keys()):
                        if key[0] == src:
                            _negative_cache.pop(key, None)
            else:
                cur = conn.execute("DELETE FROM dict_cache")
                with _memory_lock:
                    _memory_cache.clear()
                    _negative_cache.clear()
            conn.commit()
            _reset_row_count()
            return cur.rowcount or 0
        finally:
            conn.close()


def memory_stats() -> Dict[str, Any]:
    """Snapshot of the in-memory layer — for diagnostics."""
    with _memory_lock:
        return {
            "lru_size": len(_memory_cache),
            "lru_capacity": _MEMORY_CACHE_SIZE,
            "negative_size": len(_negative_cache),
            "negative_capacity": _NEGATIVE_CACHE_SIZE,
        }


def prune_expired() -> Dict[str, int]:
    """Evict expired entries from both in-memory layers.

    The OrderedDict-based caches only check TTL on read; stale entries that
    nobody asks about pile up until the size cap evicts them. In a long-lived
    server that's harmless for the positive cache (caps at 2048) but bad for
    the negative cache, which silently keeps 4096 dead-API entries for months.
    Call this periodically (every few hours) to keep the negative cache hot
    with genuinely "recent" misses.
    """
    now = time.time()
    dropped_neg = 0
    dropped_pos = 0
    with _memory_lock:
        for key, ts in list(_negative_cache.items()):
            if now - ts > _NEGATIVE_TTL_SECONDS:
                _negative_cache.pop(key, None)
                dropped_neg += 1
        for key, (stored_at, _) in list(_memory_cache.items()):
            if now - stored_at > _DEFAULT_TTL_SECONDS:
                _memory_cache.pop(key, None)
                dropped_pos += 1
    return {"negative_dropped": dropped_neg, "positive_dropped": dropped_pos}
