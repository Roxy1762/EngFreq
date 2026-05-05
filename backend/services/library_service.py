"""
Personal vocabulary library + spaced-repetition review service.

Two responsibilities:
  1. Add / update / remove entries from a user's personal word library.
     Supports bulk import from any saved Dict (dict_code) or ad-hoc entries.
  2. Schedule and grade spaced-repetition review sessions.

Design notes:
  * The library is the source of truth for review items — each library entry
    can be promoted into the review queue. Removing a library word also
    removes its review item.
  * Spacing follows a small Leitner schedule so users get something useful
    even without a heavy SRS library.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from backend.database import Dict as DictModel
from backend.database import LibraryWord, ReviewItem
from backend.models.schemas import LibraryAddRequest

logger = logging.getLogger(__name__)


# ── Spacing schedule ──────────────────────────────────────────────────────────
# Box index → next-due offset in days. Box 0 is "fresh / forgot",
# Box 4 is "long-term remembered".
_BOX_INTERVAL_DAYS: Dict[int, int] = {
    0: 0,      # review again same session
    1: 1,      # tomorrow
    2: 3,
    3: 7,
    4: 21,
}
_MAX_BOX = max(_BOX_INTERVAL_DAYS)


@dataclass
class SchedulerOutcome:
    box_before: int
    box_after: int
    due_at: datetime


# ── Library operations ────────────────────────────────────────────────────────

def list_library(
    db: Session, *, user_id: int, tag: Optional[str] = None,
    search: Optional[str] = None, limit: int = 200, offset: int = 0,
) -> List[Dict[str, Any]]:
    q = db.query(LibraryWord).filter(LibraryWord.user_id == user_id)
    if tag:
        q = q.filter(LibraryWord.tags.like(f"%{tag.strip().lower()}%"))
    if search:
        like = f"%{search.strip().lower()}%"
        q = q.filter(LibraryWord.headword.ilike(like))
    q = q.order_by(LibraryWord.created_at.desc()).offset(offset).limit(limit)
    return [_row_to_dict(row) for row in q.all()]


def add_library_word(
    db: Session, *, user_id: int, payload: LibraryAddRequest,
) -> Tuple[LibraryWord, bool]:
    """Insert a new library entry, or update the existing one if headword already saved."""
    headword = payload.headword.strip()
    existing = (
        db.query(LibraryWord)
        .filter_by(user_id=user_id, headword=headword)
        .first()
    )
    created = False
    if existing is None:
        existing = LibraryWord(user_id=user_id, headword=headword, lemma=payload.lemma or headword)
        db.add(existing)
        created = True

    # Always update fields the caller provided — gives us free dedup-and-merge.
    if payload.lemma:
        existing.lemma = payload.lemma
    if payload.pos is not None:
        existing.pos = payload.pos
    if payload.chinese_meaning is not None:
        existing.chinese_meaning = payload.chinese_meaning
    if payload.english_definition is not None:
        existing.english_definition = payload.english_definition
    if payload.example_sentence is not None:
        existing.example_sentence = payload.example_sentence
    if payload.notes is not None:
        existing.notes = payload.notes
    if payload.tags is not None:
        existing.tags = _normalize_tags(payload.tags)
    if payload.source:
        existing.source = payload.source
    if payload.source_exam_code:
        existing.source_exam_code = payload.source_exam_code.upper()
    if payload.word_level:
        existing.word_level = payload.word_level
    if payload.cefr_level:
        existing.cefr_level = payload.cefr_level
    if payload.zipf_score is not None:
        existing.zipf_score = f"{payload.zipf_score:.2f}"
    db.commit()
    db.refresh(existing)
    return existing, created


def update_library_word(
    db: Session, *, user_id: int, word_id: int, fields: Dict[str, Any],
) -> Optional[LibraryWord]:
    row = db.query(LibraryWord).filter_by(id=word_id, user_id=user_id).first()
    if row is None:
        return None
    for k, v in fields.items():
        if v is None:
            continue
        if k == "tags":
            v = _normalize_tags(v)
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return row


def remove_library_word(db: Session, *, user_id: int, word_id: int) -> bool:
    row = db.query(LibraryWord).filter_by(id=word_id, user_id=user_id).first()
    if row is None:
        return False
    # Cascade: remove any associated review item
    db.query(ReviewItem).filter_by(user_id=user_id, library_word_id=row.id).delete()
    db.delete(row)
    db.commit()
    return True


def bulk_add_from_dict(
    db: Session, *, user_id: int, dict_code: str,
    headwords: Optional[Iterable[str]] = None,
) -> Dict[str, int]:
    """Import all (or a subset of) entries from a saved Dict into the user's library.

    Returns counts: {created, updated, skipped}.
    """
    record = (
        db.query(DictModel)
        .filter_by(dict_code=dict_code.upper())
        .first()
    )
    if record is None:
        raise ValueError(f"dict_code {dict_code} not found")
    if record.user_id != user_id:
        raise PermissionError("not your dict")

    try:
        items = json.loads(record.vocab_json) or []
    except Exception:
        items = []

    headword_filter = None
    if headwords:
        headword_filter = {h.strip().lower() for h in headwords if h and h.strip()}

    created = 0
    updated = 0
    skipped = 0
    for item in items:
        hw = (item.get("headword") or item.get("lemma") or "").strip()
        if not hw:
            skipped += 1
            continue
        if headword_filter and hw.lower() not in headword_filter:
            skipped += 1
            continue
        if not headword_filter and not item.get("selected", True):
            # When importing without an explicit list, only auto-add selected words.
            skipped += 1
            continue
        payload = LibraryAddRequest(
            headword=hw,
            lemma=item.get("lemma") or hw,
            pos=item.get("pos") or None,
            chinese_meaning=item.get("chinese_meaning"),
            english_definition=item.get("english_definition"),
            example_sentence=item.get("example_sentence"),
            notes=item.get("notes"),
            tags=item.get("tags"),
            source=item.get("source"),
            source_exam_code=record.exam.exam_code if record.exam else None,
            word_level=item.get("word_level"),
            cefr_level=item.get("cefr_level"),
            zipf_score=item.get("zipf_score"),
        )
        _, was_created = add_library_word(db, user_id=user_id, payload=payload)
        if was_created:
            created += 1
        else:
            updated += 1
    return {"created": created, "updated": updated, "skipped": skipped}


# ── Review (spaced repetition) ────────────────────────────────────────────────

def enroll_in_review(db: Session, *, user_id: int, headword: str) -> ReviewItem:
    """Add a library word to the review queue (or no-op if already enrolled)."""
    headword = headword.strip()
    library_row = (
        db.query(LibraryWord)
        .filter_by(user_id=user_id, headword=headword)
        .first()
    )
    library_id = library_row.id if library_row else None
    existing = (
        db.query(ReviewItem)
        .filter_by(user_id=user_id, headword=headword)
        .first()
    )
    if existing is not None:
        if library_id and existing.library_word_id != library_id:
            existing.library_word_id = library_id
            db.commit()
        return existing
    item = ReviewItem(
        user_id=user_id,
        headword=headword,
        library_word_id=library_id,
        box=0,
        due_at=datetime.utcnow(),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def enroll_many(db: Session, *, user_id: int, headwords: Iterable[str]) -> int:
    count = 0
    for hw in headwords:
        if not hw:
            continue
        enroll_in_review(db, user_id=user_id, headword=hw)
        count += 1
    return count


def remove_from_review(db: Session, *, user_id: int, headword: str) -> bool:
    deleted = (
        db.query(ReviewItem)
        .filter_by(user_id=user_id, headword=headword.strip())
        .delete()
    )
    db.commit()
    return bool(deleted)


def get_review_queue(
    db: Session, *, user_id: int, limit: int = 50, include_future: bool = False,
) -> List[Dict[str, Any]]:
    """Return review items due now (or in the future when include_future=True)."""
    now = datetime.utcnow()
    q = db.query(ReviewItem, LibraryWord).outerjoin(
        LibraryWord, ReviewItem.library_word_id == LibraryWord.id,
    ).filter(ReviewItem.user_id == user_id)
    if not include_future:
        q = q.filter(ReviewItem.due_at <= now)
    q = q.order_by(ReviewItem.due_at.asc()).limit(limit)

    out: List[Dict[str, Any]] = []
    for item, lib in q.all():
        out.append({
            "id": item.id,
            "headword": item.headword,
            "box": item.box,
            "review_count": item.review_count,
            "correct_streak": item.correct_streak,
            "due_at": item.due_at.isoformat() if item.due_at else None,
            "last_reviewed_at": item.last_reviewed_at.isoformat() if item.last_reviewed_at else None,
            "library": _row_to_dict(lib) if lib else None,
        })
    return out


def submit_review_feedback(
    db: Session, *, user_id: int, headword: str, quality: str,
) -> Optional[SchedulerOutcome]:
    """Apply the Leitner schedule based on user's quality rating."""
    item = (
        db.query(ReviewItem)
        .filter_by(user_id=user_id, headword=headword.strip())
        .first()
    )
    if item is None:
        # Auto-enroll on first review.
        item = enroll_in_review(db, user_id=user_id, headword=headword)

    box_before = int(item.box or 0)
    if quality == "remembered":
        box_after = min(box_before + 1, _MAX_BOX)
        item.correct_streak = (item.correct_streak or 0) + 1
    elif quality == "fuzzy":
        box_after = max(box_before, 1)
        item.correct_streak = 0
    else:  # forgot
        box_after = 0
        item.correct_streak = 0

    interval = _BOX_INTERVAL_DAYS.get(box_after, 1)
    now = datetime.utcnow()
    item.box = box_after
    item.review_count = (item.review_count or 0) + 1
    item.last_reviewed_at = now
    item.due_at = now + timedelta(days=interval)
    db.commit()
    db.refresh(item)
    return SchedulerOutcome(box_before=box_before, box_after=box_after, due_at=item.due_at)


def review_stats(db: Session, *, user_id: int) -> Dict[str, Any]:
    total = db.query(ReviewItem).filter_by(user_id=user_id).count()
    now = datetime.utcnow()
    due = db.query(ReviewItem).filter(
        ReviewItem.user_id == user_id, ReviewItem.due_at <= now
    ).count()
    by_box: Dict[int, int] = {b: 0 for b in _BOX_INTERVAL_DAYS}
    for item in db.query(ReviewItem).filter_by(user_id=user_id).all():
        by_box[int(item.box or 0)] = by_box.get(int(item.box or 0), 0) + 1
    library_total = db.query(LibraryWord).filter_by(user_id=user_id).count()
    return {
        "library_total": library_total,
        "review_total": total,
        "review_due": due,
        "by_box": by_box,
        "box_intervals_days": _BOX_INTERVAL_DAYS,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_tags(raw: str) -> str:
    parts = [p.strip().lower() for p in (raw or "").split(",") if p.strip()]
    return ",".join(sorted(set(parts)))


def _row_to_dict(row: Optional[LibraryWord]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {
        "id": row.id,
        "headword": row.headword,
        "lemma": row.lemma,
        "pos": row.pos,
        "chinese_meaning": row.chinese_meaning,
        "english_definition": row.english_definition,
        "example_sentence": row.example_sentence,
        "notes": row.notes,
        "tags": [t for t in (row.tags or "").split(",") if t],
        "source": row.source,
        "source_exam_code": row.source_exam_code,
        "word_level": row.word_level,
        "cefr_level": row.cefr_level,
        "zipf_score": float(row.zipf_score) if row.zipf_score else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
