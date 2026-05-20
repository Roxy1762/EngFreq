"""
ECDICT vocabulary provider — offline Chinese-English dictionary.

ECDICT is a free, open-source Chinese-English dictionary with 3M+ entries.
It operates entirely offline using a local SQLite database.

Setup:
  1. Download ecdict.csv from https://github.com/skywind3000/ECDICT
  2. Set ECDICT_PATH=/path/to/ecdict.db (or ecdict.csv) in .env
     - If .csv is given, it will be auto-converted to SQLite on first run

No API key required. Fast lookups from local DB.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from pathlib import Path
from typing import List, Optional

from backend.config import settings
from backend.models.schemas import LemmaEntry, VocabEntry
from backend.providers.base_provider import BaseVocabProvider

logger = logging.getLogger(__name__)

_ECDICT_COLS = (
    "word", "phonetic", "definition", "translation",
    "pos", "exchange", "tag", "oxford", "collins", "bnc", "frq",
)

_POS_REMAP = {
    "n": "noun", "v": "verb", "a": "adj", "ad": "adv",
    "prep": "other", "conj": "other", "pron": "other",
    "vt": "verb", "vi": "verb", "num": "other", "int": "other",
}


def _csv_to_sqlite(csv_path: Path, db_path: Path) -> None:
    import csv

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stardict (
            word TEXT PRIMARY KEY,
            phonetic TEXT, definition TEXT, translation TEXT,
            pos TEXT, exchange TEXT, tag TEXT,
            oxford INTEGER, collins INTEGER, bnc INTEGER, frq INTEGER
        )
    """)

    with open(csv_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        batch = []
        for row in reader:
            batch.append((
                row.get("word", ""),
                row.get("phonetic", ""),
                row.get("definition", ""),
                row.get("translation", ""),
                row.get("pos", ""),
                row.get("exchange", ""),
                row.get("tag", ""),
                row.get("oxford", 0) or 0,
                row.get("collins", 0) or 0,
                row.get("bnc", 0) or 0,
                row.get("frq", 0) or 0,
            ))
            if len(batch) >= 5000:
                cur.executemany(
                    "INSERT OR IGNORE INTO stardict VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    batch
                )
                batch.clear()
        if batch:
            cur.executemany(
                "INSERT OR IGNORE INTO stardict VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                batch
            )

    conn.commit()
    conn.close()
    logger.info("ECDICT: converted %s → %s", csv_path.name, db_path.name)


def _get_ecdict_path() -> str:
    try:
        from backend.services.runtime_config import get_runtime_config
        path = get_runtime_config().dict_providers.ecdict_path
        if path:
            return path
    except Exception:
        pass
    return settings.ecdict_path or ""


class ECDICTProvider(BaseVocabProvider):
    name = "ecdict"

    # One read-only connection per thread. SQLite handles multiple readers
    # fine; per-thread storage keeps us from contending on a single
    # connection's serial cursor while still avoiding the connection
    # open/close cost of the previous one-conn-per-word pattern.
    _thread_local = threading.local()

    def __init__(self):
        raw_path = _get_ecdict_path()
        if not raw_path:
            raise RuntimeError(
                "ECDICT_PATH not configured. "
                "Set it in the admin panel under '词典工具密钥' (ECDICT 路径) or via ECDICT_PATH in .env. "
                "Download ecdict.csv from https://github.com/skywind3000/ECDICT"
            )

        path = Path(raw_path)
        if not path.exists():
            raise RuntimeError(f"ECDICT file not found: {path}")

        if path.suffix.lower() == ".csv":
            db_path = path.with_suffix(".db")
            if not db_path.exists():
                logger.info("Converting ECDICT CSV to SQLite (one-time, may take a minute)...")
                _csv_to_sqlite(path, db_path)
            self._db_path = str(db_path)
        else:
            self._db_path = str(path)

    def _get_connection(self) -> sqlite3.Connection:
        """Per-thread read-only connection, opened lazily."""
        conn = getattr(self._thread_local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                isolation_level=None,
                timeout=5.0,
            )
            conn.row_factory = sqlite3.Row
            # Read-only PRAGMAs keep the connection cheap for repeated lookups.
            conn.execute("PRAGMA query_only = ON")
            self._thread_local.conn = conn
        return conn

    def _lookup(self, word: str) -> Optional[dict]:
        # Open-close-on-every-call was sub-ms wasted plus a real connection
        # leak whenever the row fetch raised mid-cursor — the conn.close()
        # was outside the try block. Using a per-thread cached connection
        # also lets the SQLite cache stay warm across the batch.
        try:
            conn = self._get_connection()
            cur = conn.execute(
                "SELECT * FROM stardict WHERE word = ? COLLATE NOCASE LIMIT 1",
                (word.lower(),),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        except Exception as exc:
            logger.warning("ECDICT lookup failed for '%s': %s", word, exc)
            # Drop the cached connection so the next call re-opens cleanly.
            self._close_connection()
            return None

    def _close_connection(self) -> None:
        conn = getattr(self._thread_local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:   # noqa: BLE001
                pass
            self._thread_local.conn = None

    def _to_vocab_entry(self, word: str, row: dict, base: LemmaEntry) -> VocabEntry:
        raw_pos = (row.get("pos") or "").split("/")[0].strip()
        pos = _POS_REMAP.get(raw_pos, "other") if raw_pos else (base.pos.lower() if base.pos else "other")

        translation = row.get("translation") or ""
        # ECDICT translation format: "n. 名词\nv. 动词" — take first meaningful line
        trans_lines = [l.strip() for l in translation.splitlines() if l.strip()]
        chinese_meaning = "；".join(trans_lines[:3]) if trans_lines else ""

        definition = row.get("definition") or ""
        def_lines = [l.strip() for l in definition.splitlines() if l.strip()]
        english_def = " | ".join(def_lines[:2]) if def_lines else ""

        phonetic = row.get("phonetic") or ""
        notes = f"/{phonetic}/" if phonetic else ""

        return VocabEntry(
            headword=word,
            lemma=base.lemma,
            family=base.family_id,
            pos=pos,
            chinese_meaning=chinese_meaning or "—",
            english_definition=english_def or f"See dictionary: {word}",
            example_sentence="",
            notes=notes,
            body_count=base.body_count,
            stem_count=base.stem_count,
            option_count=base.option_count,
            total_count=base.total_count,
            score=base.score,
            source=self.name,
        )

    async def enrich(self, entries: List[LemmaEntry], context_text: str = "") -> List[VocabEntry]:
        if not entries:
            return []

        # Dispatch the SQLite lookups to the default thread pool so the
        # event loop stays responsive while a 50-word batch is enriched.
        # The per-thread connection cache makes each lookup sub-ms once warm.
        def _do_one(entry: LemmaEntry) -> VocabEntry:
            row = self._lookup(entry.lemma)
            if row:
                return self._to_vocab_entry(entry.lemma, row, entry)
            return VocabEntry(
                headword=entry.lemma,
                lemma=entry.lemma,
                family=entry.family_id,
                pos=entry.pos,
                chinese_meaning="",
                english_definition="",
                body_count=entry.body_count,
                stem_count=entry.stem_count,
                option_count=entry.option_count,
                total_count=entry.total_count,
                score=entry.score,
                source="ecdict_not_found",
            )

        return await asyncio.to_thread(lambda: [_do_one(e) for e in entries])
