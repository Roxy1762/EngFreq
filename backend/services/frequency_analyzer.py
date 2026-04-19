"""
Frequency analyzer — aggregates token counts from structured text sections
and computes surface / lemma / family tables with configurable scoring.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, Tuple

from backend.models.schemas import (
    FamilyEntry,
    FilterConfig,
    LemmaEntry,
    StructureStats,
    WeightConfig,
    WordEntry,
)
from backend.services.structure_recognizer import LineType, StructuredText
from backend.services.word_family import get_family_id
from backend.services.word_processor import TokenInfo, filter_tokens, tokenise

logger = logging.getLogger(__name__)


# ── Internal accumulator ──────────────────────────────────────────────────────

class _Counter:
    """Per-word running counter for body / stem / option slots."""

    __slots__ = ("body", "stem", "option", "lemma", "pos", "family_id", "surfaces")

    def __init__(self):
        self.body = 0
        self.stem = 0
        self.option = 0
        self.lemma = ""
        self.pos = ""
        self.family_id: str | None = None
        self.surfaces: set[str] = set()


# ── Main analysis function ────────────────────────────────────────────────────

def analyse(
    structured: StructuredText,
    filters: FilterConfig,
    weights: WeightConfig,
) -> Tuple[List[WordEntry], List[LemmaEntry], List[FamilyEntry], StructureStats]:
    """
    Run full frequency analysis on a StructuredText.

    Returns (word_table, lemma_table, family_table, stats).
    """
    # --- section → slot mapping ---
    section_map = {
        LineType.BODY:        "body",
        LineType.TITLE:       "body",       # treat titles as body-weight
        LineType.INSTRUCTION: "body",       # treat instructions as body-weight
        LineType.STEM:        "stem",
        LineType.OPTION:      "option",
    }

    # --- per-surface counters ---
    surface_data: Dict[str, _Counter] = defaultdict(_Counter)

    line_stats = {lt: 0 for lt in LineType}
    token_stats = {"body": 0, "stem": 0, "option": 0}

    for ann_line in structured.annotated_lines:
        if ann_line.line_type == LineType.BLANK:
            continue

        line_stats[ann_line.line_type] += 1
        slot = section_map.get(ann_line.line_type)
        if slot is None:
            continue

        tokens: List[TokenInfo] = tokenise(ann_line.text)
        tokens = filter_tokens(
            tokens,
            min_length=filters.min_word_length,
            filter_stopwords=filters.filter_stopwords,
            keep_proper_nouns=filters.keep_proper_nouns,
            filter_numbers=filters.filter_numbers,
            filter_basic_words=filters.filter_basic_words,
            basic_words_threshold=filters.basic_words_threshold,
        )

        token_stats[slot] += len(tokens)

        for tok in tokens:
            c = surface_data[tok.surface]
            setattr(c, slot, getattr(c, slot) + 1)
            # First assignment wins for lemma/pos/family_id
            if not c.lemma:
                c.lemma = tok.lemma
                c.pos = tok.pos
                c.family_id = get_family_id(tok.lemma)
            c.surfaces.add(tok.surface)

    # --- build structure stats ---
    stats = StructureStats(
        total_lines=len(structured.annotated_lines),
        body_lines=line_stats[LineType.BODY],
        stem_lines=line_stats[LineType.STEM],
        option_lines=line_stats[LineType.OPTION],
        title_lines=line_stats[LineType.TITLE],
        body_tokens=token_stats["body"],
        stem_tokens=token_stats["stem"],
        option_tokens=token_stats["option"],
    )

    # ── Word table ────────────────────────────────────────────────────────────
    word_table: List[WordEntry] = []
    for surface, c in surface_data.items():
        total = c.body + c.stem + c.option
        score = weights.score(c.body, c.stem, c.option)
        word_table.append(
            WordEntry(
                surface=surface,
                lemma=c.lemma,
                pos=c.pos,
                family_id=c.family_id,
                body_count=c.body,
                stem_count=c.stem,
                option_count=c.option,
                total_count=total,
                score=round(score, 2),
            )
        )
    word_table.sort(key=lambda e: (-e.score, -e.total_count))

    # ── Lemma table ───────────────────────────────────────────────────────────
    lemma_acc: Dict[str, _Counter] = defaultdict(_Counter)
    for surf, c in surface_data.items():
        lc = lemma_acc[c.lemma]
        lc.body += c.body
        lc.stem += c.stem
        lc.option += c.option
        if not lc.lemma:
            lc.lemma = c.lemma
            lc.pos = c.pos
            lc.family_id = c.family_id
        lc.surfaces.add(surf)

    lemma_table: List[LemmaEntry] = []
    for lemma, lc in lemma_acc.items():
        total = lc.body + lc.stem + lc.option
        score = weights.score(lc.body, lc.stem, lc.option)
        lemma_table.append(
            LemmaEntry(
                lemma=lemma,
                pos=lc.pos,
                family_id=lc.family_id,
                surface_forms=sorted(lc.surfaces),
                body_count=lc.body,
                stem_count=lc.stem,
                option_count=lc.option,
                total_count=total,
                score=round(score, 2),
            )
        )
    lemma_table.sort(key=lambda e: (-e.score, -e.total_count))

    # ── Family table ──────────────────────────────────────────────────────────
    family_acc: Dict[str, _Counter] = defaultdict(_Counter)
    for lemma, lc in lemma_acc.items():
        fid = lc.family_id or lemma
        fc = family_acc[fid]
        fc.body += lc.body
        fc.stem += lc.stem
        fc.option += lc.option
        fc.surfaces.add(lemma)   # surfaces here = member lemmas

    family_table: List[FamilyEntry] = []
    for fid, fc in family_acc.items():
        total = fc.body + fc.stem + fc.option
        score = weights.score(fc.body, fc.stem, fc.option)
        family_table.append(
            FamilyEntry(
                family_id=fid,
                members=sorted(fc.surfaces),
                body_count=fc.body,
                stem_count=fc.stem,
                option_count=fc.option,
                total_count=total,
                score=round(score, 2),
            )
        )
    family_table.sort(key=lambda e: (-e.score, -e.total_count))

    return word_table, lemma_table, family_table, stats
