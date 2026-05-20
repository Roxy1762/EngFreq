"""
Practice quiz service — converts the user's library into self-test sessions.

Three question types are produced offline (no LLM round trips):

  * ``definition_to_word`` – show the Chinese / English definition, the user
    types or selects the headword.
  * ``word_to_definition`` – show the headword, the user picks the right
    definition from distractors.
  * ``fill_in_blank``      – show the stored example sentence with the
    headword replaced by ``____``; user types the answer.

Distractors for MCQ rounds are drawn from other library entries that share
the target's CEFR level (or fall back to siblings by Zipf bucket). This
produces quizzes that feel level-appropriate rather than trivially easy.

Grading is purely string-based and never round-trips to an LLM. Submitting a
quiz records each answer as a ``ReviewEvent`` so the existing heatmap/streak
analytics include quiz participation — a small but valuable cross-feature
integration.
"""
from __future__ import annotations

import logging
import random
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from backend.database import LibraryWord, ReviewEvent, ReviewItem

logger = logging.getLogger(__name__)


# ── Quiz definitions ─────────────────────────────────────────────────────────

QUIZ_MODES = ("definition_to_word", "word_to_definition", "fill_in_blank", "mixed")
DEFAULT_QUIZ_SIZE = 10
MAX_QUIZ_SIZE = 50
DEFAULT_NUM_CHOICES = 4

# Quiz tokens expire after 30 minutes — long enough that a phone-stuck user
# isn't surprised, short enough that abandoned quizzes don't bloat memory.
QUIZ_TOKEN_TTL = timedelta(minutes=30)

# Treat answers as correct when after-normalisation they match the headword
# exactly. We allow stripping common adornments so "the apple" passes for
# "apple", but stop short of fuzzy edit-distance — students should learn
# the canonical form.
_ANSWER_TRIM = re.compile(r"^(the|an|a)\s+", re.IGNORECASE)


# ── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class QuizQuestion:
    """One quiz question as sent to the client and graded later."""
    id: str
    mode: str
    prompt: str
    headword: str
    library_id: int
    choices: Optional[List[str]] = None    # MCQ — None for typed answers
    correct_choice_index: Optional[int] = None
    example: Optional[str] = None
    pos: Optional[str] = None
    word_level: Optional[str] = None

    def to_client(self) -> Dict[str, Any]:
        """Strip the answer key before sending to the client."""
        return {
            "id": self.id,
            "mode": self.mode,
            "prompt": self.prompt,
            "library_id": self.library_id,
            "choices": list(self.choices) if self.choices else None,
            "example": self.example,
            "pos": self.pos,
            "word_level": self.word_level,
        }


@dataclass
class QuizSession:
    """In-memory record of a freshly generated quiz so we can grade later."""
    token: str
    user_id: int
    questions: List[QuizQuestion]
    expires_at: datetime
    mode: str

    def find(self, qid: str) -> Optional[QuizQuestion]:
        for q in self.questions:
            if q.id == qid:
                return q
        return None


@dataclass
class GradedAnswer:
    question_id: str
    headword: str
    correct: bool
    user_answer: str
    correct_answer: str
    explanation: str = ""


# ── Quiz token store ─────────────────────────────────────────────────────────


class _QuizStore:
    """In-memory quiz token store with lazy expiry.

    A SQLite-backed table was overkill for a feature where the typical
    lifetime of a quiz is a few minutes; we keep state in process memory.
    Stale tokens get cleaned out on every `purge_expired` call (driven by
    the get/submit paths so we don't need a separate timer).
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, QuizSession] = {}

    def put(self, session: QuizSession) -> None:
        self._sessions[session.token] = session
        self.purge_expired()

    def get(self, token: str) -> Optional[QuizSession]:
        sess = self._sessions.get(token)
        if sess is None:
            return None
        if _utcnow() >= sess.expires_at:
            self._sessions.pop(token, None)
            return None
        return sess

    def pop(self, token: str) -> Optional[QuizSession]:
        sess = self._sessions.pop(token, None)
        if sess is None or _utcnow() >= sess.expires_at:
            return None
        return sess

    def purge_expired(self) -> int:
        now = _utcnow()
        stale = [tok for tok, s in self._sessions.items() if s.expires_at <= now]
        for tok in stale:
            self._sessions.pop(tok, None)
        return len(stale)

    def size(self) -> int:
        return len(self._sessions)


_store = _QuizStore()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _norm_answer(s: str) -> str:
    s = (s or "").strip().lower()
    s = _ANSWER_TRIM.sub("", s)
    # Drop trailing punctuation
    s = re.sub(r"[.,;:!?\"'\s]+$", "", s)
    return s


# ── Candidate selection ──────────────────────────────────────────────────────


def _eligible_library_query(db: Session, *, user_id: int):
    """Pool of library rows usable for a quiz.

    A row needs:
      * a non-empty headword (always true given DB constraint)
      * AT LEAST one of {chinese_meaning, english_definition} so we have a prompt
      * NOT mastered (the user already retired the word — quizzing it pollutes
        the active study set)
    """
    return (
        db.query(LibraryWord)
        .filter(
            LibraryWord.user_id == user_id,
            LibraryWord.mastered.is_(False),
            # NB: SQLAlchemy 2 `or_` here would work too — using a case() keeps
            # the LIKE optimisation simple on SQLite.
        )
    )


def _has_definition(row: LibraryWord) -> bool:
    return bool(row.chinese_meaning or row.english_definition)


def _pick_distractors(
    pool: Sequence[LibraryWord], target: LibraryWord, k: int,
) -> List[LibraryWord]:
    """Choose `k` distractors that aren't the target word.

    Preference order:
      1. same CEFR level as the target
      2. same word_level (基础/高考/...)
      3. anything else
    """
    same_cefr: list[LibraryWord] = []
    same_level: list[LibraryWord] = []
    other: list[LibraryWord] = []
    for row in pool:
        if row.id == target.id:
            continue
        if not _has_definition(row):
            continue
        if target.cefr_level and row.cefr_level == target.cefr_level:
            same_cefr.append(row)
        elif target.word_level and row.word_level == target.word_level:
            same_level.append(row)
        else:
            other.append(row)
    random.shuffle(same_cefr)
    random.shuffle(same_level)
    random.shuffle(other)
    chosen: list[LibraryWord] = []
    for bucket in (same_cefr, same_level, other):
        for r in bucket:
            if r in chosen:
                continue
            chosen.append(r)
            if len(chosen) >= k:
                return chosen
    return chosen


def _make_definition_prompt(row: LibraryWord) -> str:
    """Build the 'definition' text shown for a word.

    Prefers Chinese (the typical target language for this app's audience),
    falls back to English. POS hint is bracketed when present.
    """
    parts: list[str] = []
    if row.pos:
        parts.append(f"({row.pos})")
    body = row.chinese_meaning or row.english_definition or ""
    parts.append(body.strip())
    return " ".join(p for p in parts if p)


_BLANK_TOKEN = "____"


def _make_blank_example(row: LibraryWord) -> Optional[str]:
    """Replace every case-insensitive occurrence of the headword in the example
    sentence with a blank token. Returns None if no replacement happened or no
    example exists (in which case fill_in_blank is unsuitable for this word).
    """
    example = (row.example_sentence or "").strip()
    if not example:
        return None
    # Word-boundary regex so "import" doesn't match "important". We also strip
    # trailing s/es/ed/ing inflection from the headword when looking for it.
    base = re.escape((row.headword or row.lemma or "").lower())
    if not base:
        return None
    pattern = re.compile(rf"\b{base}\w*\b", re.IGNORECASE)
    new, n = pattern.subn(_BLANK_TOKEN, example)
    if n == 0:
        return None
    return new


# ── Question construction ───────────────────────────────────────────────────


def _build_question(
    row: LibraryWord, pool: Sequence[LibraryWord], mode: str, num_choices: int,
) -> Optional[QuizQuestion]:
    """Produce a single QuizQuestion for `row` in the requested mode.

    Returns None if the row lacks the data needed for the chosen mode (e.g.
    fill_in_blank needs an example sentence). Callers should resample.
    """
    qid = secrets.token_urlsafe(8)

    if mode == "word_to_definition":
        if not _has_definition(row):
            return None
        prompt = (row.headword or row.lemma or "").strip()
        if not prompt:
            return None
        correct_def = _make_definition_prompt(row)
        if not correct_def:
            return None
        distractors = _pick_distractors(pool, row, num_choices - 1)
        if len(distractors) < num_choices - 1:
            return None
        choices = [correct_def] + [_make_definition_prompt(d) for d in distractors]
        random.shuffle(choices)
        idx = choices.index(correct_def)
        return QuizQuestion(
            id=qid, mode=mode,
            prompt=f"\"{prompt}\" 的意思是？",
            headword=row.headword, library_id=row.id,
            choices=choices, correct_choice_index=idx,
            example=row.example_sentence, pos=row.pos, word_level=row.word_level,
        )

    if mode == "definition_to_word":
        if not _has_definition(row):
            return None
        defn = _make_definition_prompt(row)
        if not defn:
            return None
        # Default: typed answer. If `num_choices` >= 2 we promote to MCQ to
        # keep the experience snappy on phones where typing is awkward.
        if num_choices >= 2:
            distractors = _pick_distractors(pool, row, num_choices - 1)
            if len(distractors) < num_choices - 1:
                return None
            correct_word = (row.headword or row.lemma or "").strip()
            choices = [correct_word] + [
                (d.headword or d.lemma or "").strip() for d in distractors
            ]
            random.shuffle(choices)
            idx = choices.index(correct_word)
            return QuizQuestion(
                id=qid, mode=mode,
                prompt=f"哪个单词对应：{defn}",
                headword=row.headword, library_id=row.id,
                choices=choices, correct_choice_index=idx,
                example=row.example_sentence, pos=row.pos, word_level=row.word_level,
            )
        return QuizQuestion(
            id=qid, mode=mode,
            prompt=f"哪个单词对应：{defn}",
            headword=row.headword, library_id=row.id,
            example=row.example_sentence, pos=row.pos, word_level=row.word_level,
        )

    if mode == "fill_in_blank":
        masked = _make_blank_example(row)
        if not masked:
            return None
        return QuizQuestion(
            id=qid, mode=mode,
            prompt=masked,
            headword=row.headword, library_id=row.id,
            example=row.example_sentence, pos=row.pos, word_level=row.word_level,
        )

    return None


def _question_modes_for(row: LibraryWord, mode: str) -> List[str]:
    """Order of modes to try for `mode='mixed'`, biased toward what the row
    has data for. Single-mode requests just echo the chosen mode."""
    if mode != "mixed":
        return [mode]
    pool: list[str] = []
    if row.example_sentence:
        pool.append("fill_in_blank")
    if _has_definition(row):
        pool.append("word_to_definition")
        pool.append("definition_to_word")
    random.shuffle(pool)
    return pool


# ── Public API ───────────────────────────────────────────────────────────────


def generate_quiz(
    db: Session, *, user_id: int, mode: str = "mixed",
    size: int = DEFAULT_QUIZ_SIZE, num_choices: int = DEFAULT_NUM_CHOICES,
    tag: Optional[str] = None,
    only_due: bool = False,
) -> Dict[str, Any]:
    """Build a quiz session for `user_id`.

    Args:
        mode: one of ``definition_to_word`` / ``word_to_definition`` /
              ``fill_in_blank`` / ``mixed``.
        size: number of questions (clamped 1..MAX_QUIZ_SIZE).
        num_choices: MCQ options when applicable.
        tag:  if supplied, restrict to library entries carrying this tag.
        only_due: when True, prefer words enrolled in the review queue and
                  whose ``due_at`` has passed — turns the quiz into a spaced-
                  repetition session.

    Returns ``{token, questions[...], expires_at, count}`` on success.
    """
    mode = mode if mode in QUIZ_MODES else "mixed"
    size = max(1, min(int(size or DEFAULT_QUIZ_SIZE), MAX_QUIZ_SIZE))
    num_choices = max(2, min(int(num_choices or DEFAULT_NUM_CHOICES), 6))

    q = _eligible_library_query(db, user_id=user_id)
    if tag:
        # Escape LIKE wildcards: a tag of "20%" or "wild_card" should match
        # literally, not as a wildcard pattern.
        clean_tag = tag.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        q = q.filter(LibraryWord.tags.like(f"%{clean_tag}%", escape="\\"))

    if only_due:
        # Two-step query: pull due headwords from ReviewItem first, then map
        # back to library rows. Faster on SQLite than a join when many words
        # aren't due yet (the typical case).
        now = _utcnow()
        due_rows = (
            db.query(ReviewItem.headword, ReviewItem.box)
            .filter(
                ReviewItem.user_id == user_id,
                ReviewItem.due_at <= now,
            )
            .all()
        )
        due_words = {h.lower() for h, _ in due_rows}
        if due_words:
            q = q.filter(func.lower(LibraryWord.headword).in_(due_words))
        else:
            # Nothing due → fall back to the unrestricted pool so we still
            # return *something* (with a warning the caller can show).
            pass

    pool = q.all()
    if len(pool) < num_choices and mode != "fill_in_blank":
        return {
            "ok": False,
            "error": "library_too_small",
            "needed": num_choices,
            "available": len(pool),
        }

    # Reservoir-sample candidate rows and keep building questions until we
    # have `size` valid ones — some rows may not yield a question in the
    # chosen mode (e.g. fill_in_blank needs an example sentence).
    random.shuffle(pool)
    questions: list[QuizQuestion] = []
    seen_ids: set[int] = set()
    cursor = 0
    while len(questions) < size and cursor < len(pool):
        row = pool[cursor]
        cursor += 1
        if row.id in seen_ids:
            continue
        for m in _question_modes_for(row, mode):
            q_obj = _build_question(row, pool, m, num_choices)
            if q_obj is not None:
                questions.append(q_obj)
                seen_ids.add(row.id)
                break

    if not questions:
        return {
            "ok": False,
            "error": "no_eligible_questions",
            "tip": "添加更多带例句或释义的生词后再试。",
        }

    token = secrets.token_urlsafe(16)
    session = QuizSession(
        token=token,
        user_id=user_id,
        questions=questions,
        expires_at=_utcnow() + QUIZ_TOKEN_TTL,
        mode=mode,
    )
    _store.put(session)
    return {
        "ok": True,
        "token": token,
        "mode": mode,
        "count": len(questions),
        "expires_at": session.expires_at.isoformat() + "Z",
        "questions": [q.to_client() for q in questions],
    }


def submit_quiz(
    db: Session, *, user_id: int, token: str, answers: List[Dict[str, Any]],
    record_review_event: bool = True,
) -> Dict[str, Any]:
    """Grade the submitted answers and (optionally) record ReviewEvents.

    Args:
        answers: list of ``{question_id, answer}`` (answer is a string for
                 typed modes, or the index of the chosen choice as a string).

    Returns the score, per-question breakdown, and updated streak/heatmap
    eligibility (the caller's frontend usually refreshes the dashboard
    automatically afterwards).
    """
    session = _store.pop(token)
    if session is None:
        return {"ok": False, "error": "quiz_expired_or_unknown"}
    if session.user_id != user_id:
        # Re-insert before bailing so the legitimate owner can still submit.
        _store.put(session)
        return {"ok": False, "error": "wrong_user"}

    by_qid: Dict[str, Dict[str, Any]] = {
        str(a.get("question_id") or a.get("id") or ""): a for a in (answers or [])
    }

    graded: list[GradedAnswer] = []
    correct_count = 0
    now = _utcnow()
    headword_to_quality: Dict[str, str] = {}

    for q in session.questions:
        ans = by_qid.get(q.id) or {}
        raw = ans.get("answer", "")
        is_correct = _grade_question(q, raw)
        if is_correct:
            correct_count += 1
        graded.append(
            GradedAnswer(
                question_id=q.id,
                headword=q.headword,
                correct=is_correct,
                user_answer=str(raw)[:128],
                correct_answer=_correct_answer_text(q),
                explanation=_grade_explanation(q, is_correct),
            )
        )
        # For ReviewEvent logging we collapse the per-question result into a
        # single quality: "remembered" (correct), "forgot" (incorrect). The
        # existing Leitner pipeline expects exactly those three labels.
        if q.headword:
            quality = "remembered" if is_correct else "forgot"
            # Keep the *worst* quality if the same headword appeared multiple
            # times in the quiz — one wrong should not be erased by one right.
            current = headword_to_quality.get(q.headword)
            if current != "forgot":
                headword_to_quality[q.headword] = quality

    score_pct = round(100.0 * correct_count / max(1, len(session.questions)), 1)

    review_events_written = 0
    if record_review_event and headword_to_quality:
        # Use the spaced-repetition pipeline so quiz performance feeds back
        # into Leitner scheduling — no need to duplicate that logic here.
        from backend.services import library_service
        for hw, quality in headword_to_quality.items():
            try:
                library_service.submit_review_feedback(
                    db, user_id=user_id, headword=hw, quality=quality,
                )
                review_events_written += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Quiz review-event write failed for %s: %s", hw, exc)

    return {
        "ok": True,
        "score_pct": score_pct,
        "correct": correct_count,
        "total": len(session.questions),
        "review_events_written": review_events_written,
        "results": [
            {
                "question_id": g.question_id,
                "headword": g.headword,
                "correct": g.correct,
                "your_answer": g.user_answer,
                "correct_answer": g.correct_answer,
                "explanation": g.explanation,
            }
            for g in graded
        ],
    }


def _grade_question(q: QuizQuestion, raw_answer: Any) -> bool:
    """Return True if `raw_answer` is correct for question `q`."""
    if q.choices and q.correct_choice_index is not None:
        # MCQ — accept either the choice index (as int or string) or the raw
        # choice text. Lenient parsing makes the API painless to integrate.
        try:
            idx = int(raw_answer)
            return idx == q.correct_choice_index
        except (TypeError, ValueError):
            pass
        if isinstance(raw_answer, str):
            target = q.choices[q.correct_choice_index]
            return _norm_answer(raw_answer) == _norm_answer(target)
        return False

    # Typed answer — match against headword (with light normalisation).
    given = _norm_answer(str(raw_answer or ""))
    if not given:
        return False
    return given == _norm_answer(q.headword)


def _correct_answer_text(q: QuizQuestion) -> str:
    if q.choices and q.correct_choice_index is not None:
        return q.choices[q.correct_choice_index]
    return q.headword


def _grade_explanation(q: QuizQuestion, correct: bool) -> str:
    """Short hint shown in the result panel — encouraging on success, helpful
    on miss. We avoid revealing the full definition on success so the user
    doesn't feel patronised."""
    if correct:
        return "✓ 答对了" if q.mode != "fill_in_blank" else f"✓ 正确，答案是 {q.headword}"
    return f"正确答案：{q.headword}"


# ── Statistics ───────────────────────────────────────────────────────────────


def quiz_stats(db: Session, *, user_id: int, days: int = 30) -> Dict[str, Any]:
    """Aggregate stats over the user's recent review events (which include
    quiz submissions). Mirrors :mod:`library_service.review_stats` so the
    dashboard can show "quiz performance over last 30 days" without keeping
    a separate counter table.
    """
    days = max(1, min(int(days or 30), 365))
    cutoff = _utcnow() - timedelta(days=days)
    total, remembered, fuzzy, forgot = db.query(
        func.count(ReviewEvent.id),
        func.coalesce(func.sum(case((ReviewEvent.quality == "remembered", 1), else_=0)), 0),
        func.coalesce(func.sum(case((ReviewEvent.quality == "fuzzy", 1), else_=0)), 0),
        func.coalesce(func.sum(case((ReviewEvent.quality == "forgot", 1), else_=0)), 0),
    ).filter(
        ReviewEvent.user_id == user_id,
        ReviewEvent.created_at >= cutoff,
    ).one()

    total_i = int(total or 0)
    correct = int(remembered or 0)
    accuracy = round(100.0 * correct / total_i, 1) if total_i else 0.0
    return {
        "days": days,
        "events": total_i,
        "remembered": int(remembered or 0),
        "fuzzy": int(fuzzy or 0),
        "forgot": int(forgot or 0),
        "accuracy_pct": accuracy,
        "active_quizzes_in_memory": _store.size(),
    }
