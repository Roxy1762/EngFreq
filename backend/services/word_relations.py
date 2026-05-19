"""
Word relations & smart suggestions service.

Given a single word (typically from the user's library), surface a set of
*related* words the user is likely to find useful:

  * **family**   – same derivational family (study → student, studious)
  * **peers**    – similar difficulty (CEFR level + close Zipf score) drawn
                   from the global gaokao/CET wordlists
  * **library**  – sibling entries already in the user's library that share a
                   tag, source exam, or family root
  * **collocations** – chunks the provider has previously stored under the
                   :class:`VocabEntry.collocations` field (semi-colon list)

The service is purely read-only and does not invoke any external API — it
piggy-backs on existing local data (word_family, wordlist_service, basic_vocab)
so it's safe to call frequently without rate-limit concerns.

A second helper (`suggest_gaps_for_user`) crosses the library against the
gaokao 3500 list to surface unlearned high-priority words — used by the
dashboard to nudge users toward syllabus coverage.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from backend.database import LibraryWord
from backend.services.basic_vocab import zipf_score
from backend.services.word_family import get_family_id
from backend.services.wordlist_service import (
    get_cefr_level,
    get_gaokao_words,
    get_word_level,
)

logger = logging.getLogger(__name__)


# Suffix templates we attach to a stem to fish out plausible family members
# without hitting any external API. Order is descending in usefulness so we
# can stop early when enough candidates are found.
_DERIVATION_TEMPLATES: tuple[str, ...] = (
    "{stem}",
    "{stem}e",
    "{stem}er",
    "{stem}ers",
    "{stem}or",
    "{stem}ors",
    "{stem}ed",
    "{stem}ing",
    "{stem}ings",
    "{stem}es",
    "{stem}s",
    "{stem}ly",
    "{stem}ily",
    "{stem}y",
    "{stem}ily",
    "{stem}ful",
    "{stem}fully",
    "{stem}less",
    "{stem}lessly",
    "{stem}able",
    "{stem}ably",
    "{stem}ible",
    "{stem}ibly",
    "{stem}ive",
    "{stem}ively",
    "{stem}ic",
    "{stem}ical",
    "{stem}ically",
    "{stem}ous",
    "{stem}ously",
    "{stem}al",
    "{stem}ally",
    "{stem}ity",
    "{stem}ities",
    "{stem}ness",
    "{stem}ment",
    "{stem}ments",
    "{stem}tion",
    "{stem}tions",
    "{stem}sion",
    "{stem}sions",
    "{stem}ation",
    "{stem}ations",
    "{stem}ize",
    "{stem}izer",
    "{stem}ist",
    "{stem}ists",
    "{stem}ism",
)


# Maximum candidates returned in any single response — keeps payload bounded
# and avoids surprising users with hundreds of marginal matches.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


@dataclass
class RelatedWord:
    """A single related-word suggestion.

    `relation` is a categorical label the frontend can colour-code:
    "family" / "library_family" / "peer" / "collocation" / "tag" / "exam".
    `score` is a heuristic 0–1 ranking used to order suggestions by usefulness.
    """
    word: str
    relation: str
    score: float = 0.0
    word_level: Optional[str] = None
    cefr_level: Optional[str] = None
    zipf_score: Optional[float] = None
    in_library: bool = False
    notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "word": self.word,
            "relation": self.relation,
            "score": round(float(self.score), 3),
            "word_level": self.word_level,
            "cefr_level": self.cefr_level,
            "zipf_score": (
                round(self.zipf_score, 2) if self.zipf_score is not None else None
            ),
            "in_library": bool(self.in_library),
            "notes": self.notes,
        }


# ── Internal helpers ─────────────────────────────────────────────────────────


def _strip_short_suffix(word: str) -> str:
    """Crude stem: lop one trailing ``e`` to expand the derivation surface area."""
    if len(word) > 3 and word.endswith("e"):
        return word[:-1]
    return word


def _generate_family_candidates(word: str, *, vocabulary: Iterable[str]) -> List[str]:
    """Score plausible family members by checking suffix templates against a
    vocabulary set (typically the gaokao 3500 list).

    Returns words that (a) exist in the supplied vocabulary, and (b) share the
    same family root via word_family.get_family_id.
    """
    if not word:
        return []
    word = word.lower().strip()
    vocab = set(vocabulary)
    target_family = get_family_id(word)
    if not target_family:
        return []

    stem = _strip_short_suffix(word)
    seen: set[str] = set()
    out: list[str] = []
    for tmpl in _DERIVATION_TEMPLATES:
        candidate = tmpl.format(stem=stem).lower()
        if not candidate or candidate == word or candidate in seen:
            continue
        seen.add(candidate)
        if candidate not in vocab:
            continue
        # Confirm by family — avoids false positives like "study" → "students" but
        # "study" → "studio" (different family root).
        if get_family_id(candidate) == target_family:
            out.append(candidate)
    return out


def _peer_candidates_from_list(
    word: str, *, vocabulary: Iterable[str], zipf_window: float = 0.5,
    cefr_match: bool = True, limit: int = 12,
) -> List[str]:
    """Pick words from `vocabulary` whose Zipf score is within ±`zipf_window`
    of the target word and (optionally) match CEFR level. Sorted by closeness
    of Zipf score so the most comparable peers come first.
    """
    word = word.lower().strip()
    if not word:
        return []
    target_zipf = zipf_score(word)
    if target_zipf == 0.0:
        # Unknown words don't yield meaningful peers — bail out cheaply rather
        # than ranking the whole list against an arbitrary zero.
        return []
    target_cefr = get_cefr_level(word) if cefr_match else None
    target_family = get_family_id(word)

    scored: list[tuple[float, str]] = []
    for w in vocabulary:
        if w == word or get_family_id(w) == target_family:
            continue  # skip same-family — handled by family suggestions
        z = zipf_score(w)
        if z == 0.0:
            continue
        if abs(z - target_zipf) > zipf_window:
            continue
        if target_cefr and get_cefr_level(w) != target_cefr:
            continue
        # Closer Zipf = better peer. Tie-break by length similarity.
        score = abs(z - target_zipf) + abs(len(w) - len(word)) * 0.01
        scored.append((score, w))
    scored.sort(key=lambda kv: kv[0])
    return [w for _, w in scored[:limit]]


def _user_library_words(db: Session, user_id: int) -> set[str]:
    """Cheap lookup of every headword the user has already saved."""
    rows = (
        db.query(LibraryWord.headword)
        .filter(LibraryWord.user_id == user_id)
        .all()
    )
    return {(h or "").lower() for (h,) in rows}


def _library_family_siblings(
    db: Session, *, user_id: int, family_id: Optional[str], exclude: str,
) -> List[LibraryWord]:
    if not family_id:
        return []
    rows = (
        db.query(LibraryWord)
        .filter(LibraryWord.user_id == user_id)
        .all()
    )
    out: list[LibraryWord] = []
    for r in rows:
        if (r.headword or "").lower() == exclude.lower():
            continue
        if get_family_id((r.headword or r.lemma or "").lower()) == family_id:
            out.append(r)
    return out


def _library_tag_or_exam_siblings(
    db: Session, *, user_id: int, source: LibraryWord, exclude: str, limit: int = 5,
) -> List[LibraryWord]:
    """Library entries that share *either* a tag or the same source exam code."""
    if source.tags is None and not source.source_exam_code:
        return []
    tag_set = {t.strip() for t in (source.tags or "").split(",") if t.strip()}
    same_exam = source.source_exam_code

    candidates = (
        db.query(LibraryWord)
        .filter(
            LibraryWord.user_id == user_id,
            LibraryWord.id != source.id,
        )
        .order_by(LibraryWord.created_at.desc())
        .limit(500)
        .all()
    )
    out: list[LibraryWord] = []
    seen: set[int] = set()
    for r in candidates:
        if r.id in seen or (r.headword or "").lower() == exclude.lower():
            continue
        r_tags = {t.strip() for t in (r.tags or "").split(",") if t.strip()}
        if tag_set and (tag_set & r_tags):
            out.append(r)
            seen.add(r.id)
            continue
        if same_exam and r.source_exam_code == same_exam:
            out.append(r)
            seen.add(r.id)
        if len(out) >= limit:
            break
    return out


# ── Public API ───────────────────────────────────────────────────────────────


def related_for_word(
    db: Session, *, user_id: int, word: str,
    include_peers: bool = True,
    include_family: bool = True,
    include_library_siblings: bool = True,
    limit: int = DEFAULT_LIMIT,
) -> Dict[str, Any]:
    """Return a structured set of related words for `word`.

    Always cheap and offline — driven by the gaokao wordlist and word_family
    heuristics. Caller controls which categories are returned.
    """
    word = (word or "").strip().lower()
    if not word:
        return {"word": word, "groups": [], "total": 0}

    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    in_library = _user_library_words(db, user_id)
    family_id = get_family_id(word)

    groups: list[dict[str, Any]] = []
    total = 0

    if include_family:
        family_words = _generate_family_candidates(word, vocabulary=get_gaokao_words())
        family_items = [
            RelatedWord(
                word=w,
                relation="family",
                score=1.0,                  # family is most-relevant
                word_level=get_word_level(w),
                cefr_level=get_cefr_level(w),
                zipf_score=zipf_score(w),
                in_library=w in in_library,
            ).to_dict()
            for w in family_words[:limit]
        ]
        if family_items:
            groups.append({"relation": "family", "label": "同根词", "items": family_items})
            total += len(family_items)

    if include_library_siblings:
        family_siblings = _library_family_siblings(
            db, user_id=user_id, family_id=family_id, exclude=word,
        )
        sib_items = [
            RelatedWord(
                word=r.headword.lower(),
                relation="library_family",
                score=0.95,
                word_level=r.word_level,
                cefr_level=r.cefr_level,
                zipf_score=float(r.zipf_score) if r.zipf_score else None,
                in_library=True,
                notes=r.chinese_meaning or r.english_definition,
            ).to_dict()
            for r in family_siblings[:limit]
        ]
        if sib_items:
            groups.append({"relation": "library_family", "label": "生词本同根", "items": sib_items})
            total += len(sib_items)

    if include_peers:
        peer_words = _peer_candidates_from_list(
            word, vocabulary=get_gaokao_words(), limit=limit,
        )
        peer_items = [
            RelatedWord(
                word=w,
                relation="peer",
                score=0.6,
                word_level=get_word_level(w),
                cefr_level=get_cefr_level(w),
                zipf_score=zipf_score(w),
                in_library=w in in_library,
            ).to_dict()
            for w in peer_words
        ]
        if peer_items:
            groups.append({"relation": "peer", "label": "同难度", "items": peer_items})
            total += len(peer_items)

    return {
        "word": word,
        "family_id": family_id,
        "groups": groups,
        "total": total,
    }


def related_for_library_entry(
    db: Session, *, user_id: int, word_id: int, limit: int = DEFAULT_LIMIT,
) -> Dict[str, Any]:
    """Same as :func:`related_for_word` but anchored on an existing LibraryWord row.

    Adds an extra "tag_sibling" group with library entries that share a tag or
    came from the same exam — useful for browsing a study cluster.
    """
    row = (
        db.query(LibraryWord)
        .filter(LibraryWord.id == word_id, LibraryWord.user_id == user_id)
        .first()
    )
    if row is None:
        return {"word": None, "groups": [], "total": 0, "error": "not_found"}

    base = related_for_word(
        db, user_id=user_id, word=row.headword or row.lemma or "", limit=limit,
    )
    base["library_id"] = row.id
    base["headword"] = row.headword

    siblings = _library_tag_or_exam_siblings(
        db, user_id=user_id, source=row, exclude=row.headword or "", limit=limit,
    )
    sib_items = [
        RelatedWord(
            word=(r.headword or "").lower(),
            relation="tag_or_exam_sibling",
            score=0.7,
            word_level=r.word_level,
            cefr_level=r.cefr_level,
            zipf_score=float(r.zipf_score) if r.zipf_score else None,
            in_library=True,
            notes=r.chinese_meaning or r.english_definition,
        ).to_dict()
        for r in siblings
    ]
    if sib_items:
        base.setdefault("groups", []).append(
            {"relation": "tag_or_exam_sibling", "label": "标签/试卷同组", "items": sib_items}
        )
        base["total"] = int(base.get("total", 0)) + len(sib_items)
    return base


def suggest_gaps_for_user(
    db: Session, *, user_id: int, limit: int = 20,
) -> Dict[str, Any]:
    """Surface high-value gaokao words the user has **not** added to the library.

    Ranks by exposure across the user's saved exams when available, otherwise
    by Zipf score (more useful words first). Caps at `limit` to keep payload
    snappy — the dashboard renders this as a "推荐补充" widget.
    """
    limit = max(1, min(int(limit or 20), MAX_LIMIT))
    in_library = _user_library_words(db, user_id)
    gaokao = get_gaokao_words()

    # Per-user "exposure": how often a gaokao word shows up across the user's
    # uploaded exams. We derive this from existing Exam.result_json without
    # touching analysis pipelines.
    from backend.database import Exam   # local import avoids a cycle
    exam_rows = (
        db.query(Exam.result_json)
        .filter(Exam.user_id == user_id)
        .all()
    )
    exposure: dict[str, float] = {}
    import json
    for (raw,) in exam_rows:
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:   # noqa: BLE001
            continue
        for row in data.get("lemma_table", [])[:200]:   # only top words/exam
            lemma = (row.get("lemma") or "").lower()
            if lemma in gaokao and lemma not in in_library:
                exposure[lemma] = exposure.get(lemma, 0.0) + float(row.get("score") or 0)

    candidates: list[tuple[float, str]] = []
    if exposure:
        # Rank by exposure first (highest seen on user's own exams)
        for lemma, sc in exposure.items():
            candidates.append((sc + zipf_score(lemma) * 0.1, lemma))
    else:
        # No exam history yet → just rank by frequency (most useful first)
        for w in gaokao:
            if w in in_library:
                continue
            z = zipf_score(w)
            if z == 0.0:
                continue
            candidates.append((z, w))

    candidates.sort(key=lambda kv: kv[0], reverse=True)
    items = [
        RelatedWord(
            word=w,
            relation="gap",
            score=round(float(sc), 3),
            word_level=get_word_level(w),
            cefr_level=get_cefr_level(w),
            zipf_score=zipf_score(w),
            in_library=False,
        ).to_dict()
        for sc, w in candidates[:limit]
    ]
    return {
        "source": "exam_exposure" if exposure else "frequency",
        "total": len(items),
        "items": items,
    }
