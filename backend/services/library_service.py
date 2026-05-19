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
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from backend.database import Dict as DictModel
from backend.database import LibraryWord, ReviewEvent, ReviewItem
from backend.models.schemas import LibraryAddRequest

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC now — non-deprecated replacement for ``datetime.utcnow()``.

    Returned naive (no tzinfo) so it round-trips cleanly through SQLite's tz-less
    DateTime columns and stays comparable with timestamps written before this
    helper existed.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    include_mastered: bool = True, only_mastered: bool = False,
) -> List[Dict[str, Any]]:
    q = db.query(LibraryWord).filter(LibraryWord.user_id == user_id)
    if only_mastered:
        q = q.filter(LibraryWord.mastered.is_(True))
    elif not include_mastered:
        q = q.filter(LibraryWord.mastered.is_(False))
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


def bulk_delete_library(
    db: Session, *, user_id: int, word_ids: Iterable[int],
) -> int:
    """Delete many library entries (and their review items) in one round trip."""
    ids = [int(i) for i in word_ids if i is not None]
    if not ids:
        return 0
    owned = (
        db.query(LibraryWord.id)
        .filter(LibraryWord.user_id == user_id, LibraryWord.id.in_(ids))
        .all()
    )
    owned_ids = [row[0] for row in owned]
    if not owned_ids:
        return 0
    db.query(ReviewItem).filter(
        ReviewItem.user_id == user_id,
        ReviewItem.library_word_id.in_(owned_ids),
    ).delete(synchronize_session=False)
    deleted = (
        db.query(LibraryWord)
        .filter(LibraryWord.user_id == user_id, LibraryWord.id.in_(owned_ids))
        .delete(synchronize_session=False)
    )
    db.commit()
    return int(deleted or 0)


def bulk_set_mastered(
    db: Session, *, user_id: int, word_ids: Iterable[int], mastered: bool,
) -> int:
    """Toggle the `mastered` flag on many entries. When mastering, remove them from
    the active review queue so the user isn't quizzed on archived words."""
    ids = [int(i) for i in word_ids if i is not None]
    if not ids:
        return 0
    updated = (
        db.query(LibraryWord)
        .filter(LibraryWord.user_id == user_id, LibraryWord.id.in_(ids))
        .update({LibraryWord.mastered: bool(mastered)}, synchronize_session=False)
    )
    if mastered and updated:
        db.query(ReviewItem).filter(
            ReviewItem.user_id == user_id,
            ReviewItem.library_word_id.in_(ids),
        ).delete(synchronize_session=False)
    db.commit()
    return int(updated or 0)


def bulk_apply_tags(
    db: Session, *, user_id: int, word_ids: Iterable[int],
    add: Optional[Iterable[str]] = None, remove: Optional[Iterable[str]] = None,
) -> int:
    """Add and/or remove tags on many entries. Returns count actually modified."""
    add_set = {t.strip().lower() for t in (add or []) if t and t.strip()}
    remove_set = {t.strip().lower() for t in (remove or []) if t and t.strip()}
    if not add_set and not remove_set:
        return 0
    ids = [int(i) for i in word_ids if i is not None]
    if not ids:
        return 0
    rows = (
        db.query(LibraryWord)
        .filter(LibraryWord.user_id == user_id, LibraryWord.id.in_(ids))
        .all()
    )
    modified = 0
    for row in rows:
        current = {t for t in (row.tags or "").split(",") if t}
        new = (current | add_set) - remove_set
        normalised = ",".join(sorted(new)) or None
        if normalised != row.tags:
            row.tags = normalised
            modified += 1
    if modified:
        db.commit()
    return modified


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

    # `headword_filter is None` → import all selected; `set()` (possibly empty) → restrict to that set.
    # Distinguishing "no filter requested" from "filter requested but empty after sanitising" was
    # broken before — an all-whitespace filter list silently fell through to the unfiltered import.
    headword_filter: Optional[set[str]] = None
    if headwords is not None:
        headword_filter = {h.strip().lower() for h in headwords if h and h.strip()}

    created = 0
    updated = 0
    skipped = 0
    for item in items:
        hw = (item.get("headword") or item.get("lemma") or "").strip()
        if not hw:
            skipped += 1
            continue
        if headword_filter is not None and hw.lower() not in headword_filter:
            skipped += 1
            continue
        if headword_filter is None and not item.get("selected", True):
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
        due_at=_utcnow(),
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
    now = _utcnow()
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
    now = _utcnow()
    item.box = box_after
    item.review_count = (item.review_count or 0) + 1
    item.last_reviewed_at = now
    item.due_at = now + timedelta(days=interval)
    # Log every grading so heatmap/streak analytics aren't capped at "last state".
    db.add(ReviewEvent(
        user_id=user_id,
        headword=item.headword,
        quality=quality,
        box_before=box_before,
        box_after=box_after,
        created_at=now,
    ))
    db.commit()
    db.refresh(item)
    return SchedulerOutcome(box_before=box_before, box_after=box_after, due_at=item.due_at)


def library_stats(db: Session, *, user_id: int) -> Dict[str, Any]:
    """Aggregate counts for a user's personal library — used by the dashboard.

    Returns counts broken down by word_level, cefr_level, and source provider,
    plus a count of how many entries are enrolled in the review queue.
    All breakdowns use SQL GROUP BY so we don't materialise every row.
    """
    base = db.query(LibraryWord).filter(LibraryWord.user_id == user_id)
    week_cutoff = _utcnow() - timedelta(days=7)

    total, mastered, last_week = db.query(
        func.count(LibraryWord.id),
        func.coalesce(func.sum(case((LibraryWord.mastered.is_(True), 1), else_=0)), 0),
        func.coalesce(func.sum(case((LibraryWord.created_at >= week_cutoff, 1), else_=0)), 0),
    ).filter(LibraryWord.user_id == user_id).one()

    by_level = {
        (lvl or "未分级"): cnt
        for lvl, cnt in db.query(LibraryWord.word_level, func.count(LibraryWord.id))
            .filter(LibraryWord.user_id == user_id)
            .group_by(LibraryWord.word_level)
            .all()
    }
    by_cefr = {
        lvl: cnt
        for lvl, cnt in db.query(LibraryWord.cefr_level, func.count(LibraryWord.id))
            .filter(LibraryWord.user_id == user_id, LibraryWord.cefr_level.isnot(None))
            .group_by(LibraryWord.cefr_level)
            .all()
    }
    by_source = {
        src: cnt
        for src, cnt in db.query(LibraryWord.source, func.count(LibraryWord.id))
            .filter(LibraryWord.user_id == user_id, LibraryWord.source.isnot(None))
            .group_by(LibraryWord.source)
            .all()
    }

    # Tags still need a single full scan of the (small) tags column, but we
    # only pull the column itself, not whole rows.
    tag_counts: Dict[str, int] = {}
    for (raw,) in base.with_entities(LibraryWord.tags).filter(LibraryWord.tags.isnot(None)).all():
        for tag in (raw or "").split(","):
            tag = tag.strip()
            if tag:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

    review_enrolled = db.query(func.count(ReviewItem.id)).filter(ReviewItem.user_id == user_id).scalar() or 0
    return {
        "total": int(total or 0),
        "mastered": int(mastered or 0),
        "added_last_7_days": int(last_week or 0),
        "review_enrolled": int(review_enrolled),
        "by_word_level": by_level,
        "by_cefr_level": by_cefr,
        "by_source": by_source,
        "top_tags": sorted(
            ({"tag": t, "count": c} for t, c in tag_counts.items()),
            key=lambda r: (-r["count"], r["tag"]),
        )[:50],
    }


def list_tags(db: Session, *, user_id: int) -> List[Dict[str, Any]]:
    """Return every tag the user has applied with its frequency. Useful for autocomplete."""
    rows = db.query(LibraryWord.tags).filter_by(user_id=user_id).all()
    counts: Dict[str, int] = {}
    for (raw,) in rows:
        for tag in (raw or "").split(","):
            tag = tag.strip()
            if tag:
                counts[tag] = counts.get(tag, 0) + 1
    return sorted(
        ({"tag": t, "count": c} for t, c in counts.items()),
        key=lambda r: (-r["count"], r["tag"]),
    )


def review_stats(db: Session, *, user_id: int) -> Dict[str, Any]:
    now = _utcnow()
    total, due = db.query(
        func.count(ReviewItem.id),
        func.coalesce(func.sum(case((ReviewItem.due_at <= now, 1), else_=0)), 0),
    ).filter(ReviewItem.user_id == user_id).one()

    by_box: Dict[int, int] = {b: 0 for b in _BOX_INTERVAL_DAYS}
    for box, cnt in db.query(ReviewItem.box, func.count(ReviewItem.id))\
            .filter(ReviewItem.user_id == user_id)\
            .group_by(ReviewItem.box)\
            .all():
        by_box[int(box or 0)] = int(cnt or 0)

    library_total = db.query(func.count(LibraryWord.id))\
        .filter(LibraryWord.user_id == user_id).scalar() or 0
    return {
        "library_total": int(library_total),
        "review_total": int(total or 0),
        "review_due": int(due or 0),
        "by_box": by_box,
        "box_intervals_days": _BOX_INTERVAL_DAYS,
    }


# ── Review analytics: heatmap + streak ────────────────────────────────────────

def review_heatmap(
    db: Session, *, user_id: int, days: int = 30,
) -> Dict[str, Any]:
    """Return per-day review activity for the last `days` days.

    Output schema:
      {
        "days": <int>,
        "start": "YYYY-MM-DD",
        "end":   "YYYY-MM-DD",
        "buckets": [ { "date": "YYYY-MM-DD",
                       "total": int, "remembered": int, "fuzzy": int, "forgot": int }, ... ]
      }

    Buckets are aligned to UTC date boundaries. Empty days are included so the
    heatmap component can render a contiguous calendar.
    """
    days = max(1, min(days, 365))
    today = date.today()
    start_date = today - timedelta(days=days - 1)
    cutoff = datetime.combine(start_date, datetime.min.time())

    day_expr = func.date(ReviewEvent.created_at)
    rows = (
        db.query(
            day_expr.label("day"),
            ReviewEvent.quality,
            func.count(ReviewEvent.id),
        )
        .filter(ReviewEvent.user_id == user_id, ReviewEvent.created_at >= cutoff)
        .group_by(day_expr, ReviewEvent.quality)
        .all()
    )

    bucket_map: Dict[str, Dict[str, int]] = {}
    for raw_day, quality, count in rows:
        # SQLite returns date() as ISO string; Postgres as date object — normalise.
        key = raw_day if isinstance(raw_day, str) else raw_day.isoformat()
        slot = bucket_map.setdefault(key, {"total": 0, "remembered": 0, "fuzzy": 0, "forgot": 0})
        slot["total"] += int(count or 0)
        if quality in slot:
            slot[quality] += int(count or 0)

    buckets: List[Dict[str, Any]] = []
    for offset_days in range(days):
        d = start_date + timedelta(days=offset_days)
        key = d.isoformat()
        info = bucket_map.get(key) or {"total": 0, "remembered": 0, "fuzzy": 0, "forgot": 0}
        buckets.append({"date": key, **info})

    return {
        "days": days,
        "start": start_date.isoformat(),
        "end": today.isoformat(),
        "buckets": buckets,
    }


def review_streak(db: Session, *, user_id: int) -> Dict[str, Any]:
    """Daily-review streak (consecutive UTC days with ≥1 review event).

    Returns current_streak (counting today/yesterday-anchored runs), longest_streak,
    last_review_date, and reviewed_today flag.
    """
    rows = (
        db.query(func.date(ReviewEvent.created_at))
        .filter(ReviewEvent.user_id == user_id)
        .group_by(func.date(ReviewEvent.created_at))
        .all()
    )
    raw_days: List[date] = []
    for (raw,) in rows:
        if raw is None:
            continue
        if isinstance(raw, str):
            try:
                raw_days.append(date.fromisoformat(raw))
            except ValueError:
                continue
        elif isinstance(raw, datetime):
            raw_days.append(raw.date())
        else:
            raw_days.append(raw)  # type: ignore[arg-type]
    raw_days.sort()

    if not raw_days:
        return {
            "current_streak": 0, "longest_streak": 0,
            "last_review_date": None, "reviewed_today": False,
        }

    longest = run = 1
    for i in range(1, len(raw_days)):
        if (raw_days[i] - raw_days[i - 1]).days == 1:
            run += 1
            longest = max(longest, run)
        elif raw_days[i] != raw_days[i - 1]:
            run = 1

    today = date.today()
    reviewed_today = raw_days[-1] == today
    # Current streak counts only if the last review was today or yesterday —
    # otherwise it's already broken.
    current = 0
    if (today - raw_days[-1]).days <= 1:
        current = 1
        for i in range(len(raw_days) - 1, 0, -1):
            if (raw_days[i] - raw_days[i - 1]).days == 1:
                current += 1
            else:
                break
    return {
        "current_streak": current,
        "longest_streak": longest,
        "last_review_date": raw_days[-1].isoformat(),
        "reviewed_today": reviewed_today,
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
        "mastered": bool(row.mastered),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
