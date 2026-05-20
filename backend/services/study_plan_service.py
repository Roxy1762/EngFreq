"""
Adaptive daily study plan.

Generates a personalised study queue for the day based on the user's:
  * due-for-review words (spaced-repetition queue)
  * gap suggestions (high-value gaokao words not yet in library)
  * recent accuracy and momentum (powering the difficulty mix)

Plans are immutable once generated: re-opening the page returns the same
list rather than reshuffling. This matters for users who switch devices
mid-session — they don't lose progress on items they've already started.

A new plan is generated on first access per UTC day, or on an explicit
``refresh`` request from the user.

The service is entirely offline — no LLM calls. Recommendations come from
the existing review queue + the cheap word_relations gap algorithm. This
keeps the daily plan instant to load and avoids tying study habits to
LLM availability.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from backend.database import (
    LibraryWord,
    ReviewEvent,
    ReviewItem,
    StudyPlan,
    StudyPlanItem,
)
from backend.services import word_relations
from backend.services.library_service import review_streak

logger = logging.getLogger(__name__)


# ── Tunables ────────────────────────────────────────────────────────────────

DEFAULT_REVIEW_TARGET = 12   # due words to review
DEFAULT_LEARN_TARGET = 5     # new gap words to introduce
DEFAULT_QUIZ_TARGET = 8      # questions in the focused quiz section

# Accuracy thresholds that drive the difficulty mix.
# Below ``LOW_ACCURACY`` we lean on review-heavy plans (consolidate weak
# areas); above ``HIGH_ACCURACY`` we increase the new-word intake.
LOW_ACCURACY = 60.0
HIGH_ACCURACY = 85.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _today() -> date:
    return _utcnow().date()


# ── Accuracy + momentum ─────────────────────────────────────────────────────

@dataclass
class _Snapshot:
    accuracy_pct: float
    events_last_7d: int
    forgot_streak_words: List[str]
    weak_levels: List[str]


def _build_snapshot(db: Session, *, user_id: int) -> _Snapshot:
    """Aggregate the recent activity that drives plan difficulty."""
    cutoff = _utcnow() - timedelta(days=7)
    total, remembered, forgot = db.query(
        func.count(ReviewEvent.id),
        func.coalesce(func.sum(case((ReviewEvent.quality == "remembered", 1), else_=0)), 0),
        func.coalesce(func.sum(case((ReviewEvent.quality == "forgot", 1), else_=0)), 0),
    ).filter(
        ReviewEvent.user_id == user_id,
        ReviewEvent.created_at >= cutoff,
    ).one()
    total = int(total or 0)
    accuracy = round(100.0 * int(remembered or 0) / total, 1) if total else 0.0

    # Words the user got wrong twice or more in the last week — strong
    # candidates for tomorrow's review queue.
    forgot_rows = (
        db.query(ReviewEvent.headword)
        .filter(
            ReviewEvent.user_id == user_id,
            ReviewEvent.created_at >= cutoff,
            ReviewEvent.quality == "forgot",
        )
        .all()
    )
    counter = Counter(hw.lower() for (hw,) in forgot_rows if hw)
    forgot_streak = [w for w, n in counter.most_common(20) if n >= 2]

    # Weakest CEFR levels in the library, ranked by miss rate.
    weak_levels = _weak_cefr_levels(db, user_id=user_id, cutoff=cutoff)

    return _Snapshot(
        accuracy_pct=accuracy,
        events_last_7d=total,
        forgot_streak_words=forgot_streak,
        weak_levels=weak_levels,
    )


def _weak_cefr_levels(db: Session, *, user_id: int, cutoff: datetime) -> List[str]:
    """Identify CEFR buckets with sub-50% recall in the last week."""
    rows = (
        db.query(
            LibraryWord.cefr_level,
            ReviewEvent.quality,
            func.count(ReviewEvent.id),
        )
        .join(
            ReviewEvent,
            (ReviewEvent.user_id == LibraryWord.user_id)
            & (func.lower(ReviewEvent.headword) == func.lower(LibraryWord.headword)),
        )
        .filter(
            LibraryWord.user_id == user_id,
            ReviewEvent.created_at >= cutoff,
            LibraryWord.cefr_level.isnot(None),
        )
        .group_by(LibraryWord.cefr_level, ReviewEvent.quality)
        .all()
    )
    by_level: Dict[str, Dict[str, int]] = {}
    for cefr, quality, count in rows:
        slot = by_level.setdefault(cefr, {"remembered": 0, "fuzzy": 0, "forgot": 0})
        if quality in slot:
            slot[quality] += int(count or 0)
    weak: List[tuple[str, float]] = []
    for cefr, slots in by_level.items():
        total = sum(slots.values())
        if total < 3:
            continue
        miss_rate = (slots["forgot"] + slots["fuzzy"] * 0.5) / total
        if miss_rate >= 0.5:
            weak.append((cefr, miss_rate))
    weak.sort(key=lambda kv: kv[1], reverse=True)
    return [c for c, _ in weak]


# ── Item selection ──────────────────────────────────────────────────────────

def _select_review_items(
    db: Session, *, user_id: int, target: int, forgot_streak: List[str],
) -> List[Dict[str, Any]]:
    """Pull up to ``target`` due review items.

    Prioritises words the user has missed twice or more in the last week,
    then falls back to the regular due queue (oldest first).
    """
    if target <= 0:
        return []
    now = _utcnow()
    rows = (
        db.query(ReviewItem, LibraryWord)
        .outerjoin(LibraryWord, ReviewItem.library_word_id == LibraryWord.id)
        .filter(
            ReviewItem.user_id == user_id,
            ReviewItem.due_at <= now,
        )
        .order_by(ReviewItem.due_at.asc())
        .limit(target * 3)   # fetch extras so we can re-rank by forgot_streak
        .all()
    )
    seen: set[str] = set()
    boosted: List[Dict[str, Any]] = []
    rest: List[Dict[str, Any]] = []
    for item, lib in rows:
        hw_l = (item.headword or "").lower()
        if hw_l in seen:
            continue
        seen.add(hw_l)
        payload = _review_item_payload(item, lib)
        if hw_l in forgot_streak:
            boosted.append(payload)
        else:
            rest.append(payload)
    combined = boosted + rest
    return combined[:target]


def _select_learn_items(
    db: Session, *, user_id: int, target: int, weak_levels: List[str],
) -> List[Dict[str, Any]]:
    """Suggest ``target`` new gap words anchored on the user's weak CEFR levels."""
    if target <= 0:
        return []
    payload = word_relations.suggest_gaps_for_user(db, user_id=user_id, limit=target * 4)
    items = payload.get("items") or []
    if not items:
        return []
    # Bias toward weak CEFR levels first when available, but never below the
    # ``target`` count — fall back to the original ranking when filtering
    # leaves too few candidates.
    if weak_levels:
        weak_set = set(weak_levels)
        weak_items = [it for it in items if it.get("cefr_level") in weak_set]
        if len(weak_items) >= target:
            items = weak_items
    out: List[Dict[str, Any]] = []
    for it in items[:target]:
        out.append({
            "headword": it.get("word"),
            "lemma": it.get("word"),
            "kind": "learn",
            "source": "gap",
            "word_level": it.get("word_level"),
            "cefr_level": it.get("cefr_level"),
            "chinese_meaning": None,
            "english_definition": None,
            "example_sentence": None,
        })
    return out


def _select_quiz_items(
    db: Session, *, user_id: int, target: int, forgot_streak: List[str],
) -> List[Dict[str, Any]]:
    """Curated set of headwords the focused quiz can be built from."""
    if target <= 0:
        return []
    # Active library words (not mastered) with a definition the quiz can
    # use as a prompt.
    q = (
        db.query(LibraryWord)
        .filter(
            LibraryWord.user_id == user_id,
            LibraryWord.mastered.is_(False),
            (
                LibraryWord.chinese_meaning.isnot(None)
                | LibraryWord.english_definition.isnot(None)
            ),
        )
    )
    pool = q.order_by(LibraryWord.updated_at.desc()).limit(target * 5).all()
    forgot_set = set(forgot_streak)
    boosted = [r for r in pool if (r.headword or "").lower() in forgot_set]
    rest = [r for r in pool if (r.headword or "").lower() not in forgot_set]
    combined = boosted + rest
    out: List[Dict[str, Any]] = []
    for row in combined[:target]:
        out.append({
            "headword": row.headword,
            "lemma": row.lemma,
            "kind": "quiz",
            "source": "library",
            "word_level": row.word_level,
            "cefr_level": row.cefr_level,
            "chinese_meaning": row.chinese_meaning,
            "english_definition": row.english_definition,
            "example_sentence": row.example_sentence,
        })
    return out


def _review_item_payload(item: ReviewItem, lib: Optional[LibraryWord]) -> Dict[str, Any]:
    return {
        "headword": item.headword,
        "lemma": (lib.lemma if lib else item.headword),
        "kind": "review",
        "source": "review",
        "word_level": getattr(lib, "word_level", None) if lib else None,
        "cefr_level": getattr(lib, "cefr_level", None) if lib else None,
        "chinese_meaning": getattr(lib, "chinese_meaning", None) if lib else None,
        "english_definition": getattr(lib, "english_definition", None) if lib else None,
        "example_sentence": getattr(lib, "example_sentence", None) if lib else None,
    }


# ── Targets calculation ─────────────────────────────────────────────────────

def _adapt_targets(
    snapshot: _Snapshot,
    *,
    review_target: Optional[int],
    learn_target: Optional[int],
    quiz_target: Optional[int],
    include_quiz: bool,
) -> tuple[int, int, int]:
    """Apply the adaptive scaling on top of any user-supplied overrides."""
    rt = review_target if review_target is not None else DEFAULT_REVIEW_TARGET
    lt = learn_target if learn_target is not None else DEFAULT_LEARN_TARGET
    qt = quiz_target if quiz_target is not None else (DEFAULT_QUIZ_TARGET if include_quiz else 0)

    if review_target is None and learn_target is None:
        # Only adapt when the user accepts the default mix.
        if snapshot.accuracy_pct and snapshot.events_last_7d >= 10:
            if snapshot.accuracy_pct < LOW_ACCURACY:
                rt = int(DEFAULT_REVIEW_TARGET * 1.5)
                lt = max(2, int(DEFAULT_LEARN_TARGET * 0.5))
            elif snapshot.accuracy_pct > HIGH_ACCURACY:
                rt = max(4, int(DEFAULT_REVIEW_TARGET * 0.8))
                lt = int(DEFAULT_LEARN_TARGET * 1.6)
    if not include_quiz:
        qt = 0
    return rt, lt, qt


# ── Public API ──────────────────────────────────────────────────────────────

def get_or_create_today(
    db: Session, *, user_id: int,
    review_target: Optional[int] = None,
    learn_target: Optional[int] = None,
    quiz_target: Optional[int] = None,
    include_quiz: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    """Return today's plan, generating it on first call (or when ``force``)."""
    today_key = _today().isoformat()
    plan = (
        db.query(StudyPlan)
        .filter(StudyPlan.user_id == user_id, StudyPlan.plan_date == today_key)
        .first()
    )
    if plan is not None and not force:
        return _plan_to_payload(plan)
    if plan is not None and force:
        # Drop items + plan; cascade does the rest.
        db.query(StudyPlanItem).filter(StudyPlanItem.plan_id == plan.id).delete(synchronize_session=False)
        db.delete(plan)
        db.commit()

    snapshot = _build_snapshot(db, user_id=user_id)
    streak = review_streak(db, user_id=user_id)
    rt, lt, qt = _adapt_targets(
        snapshot,
        review_target=review_target,
        learn_target=learn_target,
        quiz_target=quiz_target,
        include_quiz=include_quiz,
    )

    reviews = _select_review_items(
        db, user_id=user_id, target=rt, forgot_streak=snapshot.forgot_streak_words,
    )
    learns = _select_learn_items(
        db, user_id=user_id, target=lt, weak_levels=snapshot.weak_levels,
    )
    quizzes = _select_quiz_items(
        db, user_id=user_id, target=qt, forgot_streak=snapshot.forgot_streak_words,
    )

    plan = StudyPlan(
        user_id=user_id,
        plan_date=today_key,
        review_target=len(reviews),
        learn_target=len(learns),
        quiz_target=len(quizzes),
        accuracy_pct=f"{snapshot.accuracy_pct:.1f}" if snapshot.accuracy_pct else None,
        streak_at_creation=int(streak.get("current_streak", 0) or 0),
        insights_json=json.dumps({
            "accuracy_pct": snapshot.accuracy_pct,
            "events_last_7d": snapshot.events_last_7d,
            "weak_levels": snapshot.weak_levels,
            "forgot_streak_words": snapshot.forgot_streak_words[:10],
            "streak": streak,
        }, ensure_ascii=False),
    )
    db.add(plan)
    db.flush()    # need plan.id before items

    position = 0
    for payload in (*reviews, *learns, *quizzes):
        db.add(StudyPlanItem(
            plan_id=plan.id,
            headword=payload["headword"],
            lemma=payload.get("lemma"),
            kind=payload["kind"],
            source=payload.get("source"),
            word_level=payload.get("word_level"),
            cefr_level=payload.get("cefr_level"),
            chinese_meaning=payload.get("chinese_meaning"),
            english_definition=payload.get("english_definition"),
            example_sentence=payload.get("example_sentence"),
            position=position,
        ))
        position += 1

    db.commit()
    db.refresh(plan)
    return _plan_to_payload(plan)


def mark_item_complete(
    db: Session, *, user_id: int, item_id: int,
) -> Optional[Dict[str, Any]]:
    """Toggle a plan item's completed flag. Returns updated plan payload or None."""
    item = (
        db.query(StudyPlanItem)
        .join(StudyPlan, StudyPlan.id == StudyPlanItem.plan_id)
        .filter(StudyPlanItem.id == item_id, StudyPlan.user_id == user_id)
        .first()
    )
    if item is None:
        return None
    plan = db.query(StudyPlan).filter(StudyPlan.id == item.plan_id).first()
    if plan is None:
        return None
    was_done = bool(item.completed)
    item.completed = True
    item.completed_at = _utcnow()
    # Update running counter on the plan, but only when transitioning.
    if not was_done:
        if item.kind == "review":
            plan.completed_review = (plan.completed_review or 0) + 1
        elif item.kind == "learn":
            plan.completed_learn = (plan.completed_learn or 0) + 1
        elif item.kind == "quiz":
            plan.completed_quiz = (plan.completed_quiz or 0) + 1
    db.commit()
    db.refresh(plan)
    return _plan_to_payload(plan)


def history(
    db: Session, *, user_id: int, days: int = 14,
) -> Dict[str, Any]:
    """Past plans (most recent first), capped at ``days``."""
    days = max(1, min(int(days or 14), 90))
    cutoff_key = (_today() - timedelta(days=days)).isoformat()
    plans = (
        db.query(StudyPlan)
        .filter(StudyPlan.user_id == user_id, StudyPlan.plan_date >= cutoff_key)
        .order_by(StudyPlan.plan_date.desc())
        .all()
    )
    return {
        "days": days,
        "items": [_plan_summary(p) for p in plans],
    }


def insights(db: Session, *, user_id: int, days: int = 30) -> Dict[str, Any]:
    """Longer-horizon summary across the user's recent plans."""
    days = max(1, min(int(days or 30), 90))
    cutoff_key = (_today() - timedelta(days=days)).isoformat()
    plans = (
        db.query(StudyPlan)
        .filter(StudyPlan.user_id == user_id, StudyPlan.plan_date >= cutoff_key)
        .all()
    )
    total_review = sum(int(p.completed_review or 0) for p in plans)
    target_review = sum(int(p.review_target or 0) for p in plans)
    total_learn = sum(int(p.completed_learn or 0) for p in plans)
    target_learn = sum(int(p.learn_target or 0) for p in plans)
    total_quiz = sum(int(p.completed_quiz or 0) for p in plans)
    target_quiz = sum(int(p.quiz_target or 0) for p in plans)
    completion = 0.0
    full_target = target_review + target_learn + target_quiz
    if full_target:
        completion = round(
            100.0 * (total_review + total_learn + total_quiz) / full_target, 1,
        )
    streak = review_streak(db, user_id=user_id)
    return {
        "days": days,
        "plans_generated": len(plans),
        "completed_review": total_review,
        "completed_learn": total_learn,
        "completed_quiz": total_quiz,
        "target_review": target_review,
        "target_learn": target_learn,
        "target_quiz": target_quiz,
        "completion_pct": completion,
        "streak": streak,
    }


# ── Serialisation ───────────────────────────────────────────────────────────

def _plan_to_payload(plan: StudyPlan) -> Dict[str, Any]:
    try:
        ins = json.loads(plan.insights_json) if plan.insights_json else {}
    except Exception:
        ins = {}
    items = sorted(plan.items or [], key=lambda it: it.position)
    return {
        "id": plan.id,
        "plan_date": plan.plan_date,
        "review_target": int(plan.review_target or 0),
        "learn_target": int(plan.learn_target or 0),
        "quiz_target": int(plan.quiz_target or 0),
        "completed_review": int(plan.completed_review or 0),
        "completed_learn": int(plan.completed_learn or 0),
        "completed_quiz": int(plan.completed_quiz or 0),
        "accuracy_pct": float(plan.accuracy_pct) if plan.accuracy_pct else None,
        "streak_at_creation": int(plan.streak_at_creation or 0),
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
        "insights": ins,
        "items": [_item_to_payload(it) for it in items],
    }


def _plan_summary(plan: StudyPlan) -> Dict[str, Any]:
    total_done = int(plan.completed_review or 0) + int(plan.completed_learn or 0) + int(plan.completed_quiz or 0)
    total_target = int(plan.review_target or 0) + int(plan.learn_target or 0) + int(plan.quiz_target or 0)
    return {
        "id": plan.id,
        "plan_date": plan.plan_date,
        "review_target": int(plan.review_target or 0),
        "learn_target": int(plan.learn_target or 0),
        "quiz_target": int(plan.quiz_target or 0),
        "completed_review": int(plan.completed_review or 0),
        "completed_learn": int(plan.completed_learn or 0),
        "completed_quiz": int(plan.completed_quiz or 0),
        "completion_pct": round(100.0 * total_done / total_target, 1) if total_target else 0.0,
    }


def _item_to_payload(item: StudyPlanItem) -> Dict[str, Any]:
    return {
        "id": item.id,
        "headword": item.headword,
        "lemma": item.lemma,
        "kind": item.kind,
        "source": item.source,
        "word_level": item.word_level,
        "cefr_level": item.cefr_level,
        "chinese_meaning": item.chinese_meaning,
        "english_definition": item.english_definition,
        "example_sentence": item.example_sentence,
        "position": int(item.position or 0),
        "completed": bool(item.completed),
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
    }
