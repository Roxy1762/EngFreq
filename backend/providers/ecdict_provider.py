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

import logging
import sqlite3
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


class ECDICTProvider(BaseVocabProvider):
    name = "ecdict"

    def __init__(self):
        raw_path = settings.ecdict_path
        if not raw_path:
            raise RuntimeError(
                "ECDICT_PATH not configured. "
                "Download ecdict.csv from https://github.com/skywind3000/ECDICT "
                "and set ECDICT_PATH in .env"
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

    def _lookup(self, word: str) -> Optional[dict]:
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM stardict WHERE word = ? COLLATE NOCASE LIMIT 1",
                (word.lower(),)
            )
            row = cur.fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as exc:
            logger.warning("ECDICT lookup failed for '%s': %s", word, exc)
            return None

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
        results: List[VocabEntry] = []

        for entry in entries:
            row = self._lookup(entry.lemma)
            if row:
                results.append(self._to_vocab_entry(entry.lemma, row, entry))
            else:
                results.append(VocabEntry(
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
                ))

        return results
