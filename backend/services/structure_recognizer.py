"""
Exam structure recognizer.

Classifies each line of extracted text into:
  - TITLE       : section headers, part labels, exam titles
  - INSTRUCTION : directions / requirements text
  - STEM        : numbered question stems
  - OPTION      : A/B/C/D answer choices
  - BODY        : reading passages and other running prose
  - BLANK       : empty lines

The classification is heuristic-based (regex + context rules).
It handles compound option lines (A. x  B. y  C. z  D. w) by splitting them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict


class LineType(Enum):
    TITLE = "title"
    INSTRUCTION = "instruction"
    STEM = "stem"
    OPTION = "option"
    BODY = "body"
    BLANK = "blank"


@dataclass
class AnnotatedLine:
    text: str
    line_type: LineType
    option_label: str = ""   # "A", "B", "C", or "D" for OPTION lines


@dataclass
class StructuredText:
    annotated_lines: List[AnnotatedLine] = field(default_factory=list)

    # ── Convenience accessors ────────────────────────────────────────────────

    def get_text(self, *line_types: LineType) -> str:
        types = set(line_types)
        return " ".join(
            l.text for l in self.annotated_lines if l.line_type in types
        )

    @property
    def body_text(self) -> str:
        return self.get_text(LineType.BODY)

    @property
    def stem_text(self) -> str:
        return self.get_text(LineType.STEM)

    @property
    def option_text(self) -> str:
        return self.get_text(LineType.OPTION)

    @property
    def stats(self) -> Dict[str, int]:
        from collections import Counter
        c = Counter(l.line_type for l in self.annotated_lines)
        return {lt.value: c.get(lt, 0) for lt in LineType}


# ── Regex patterns ────────────────────────────────────────────────────────────

# Single option at line start: "A. text" / "A) text" / "(A) text" / "A、text" / "A．text"
_OPTION_RE = re.compile(
    r"^([A-Da-d])\s*[.、．\)）]\s+\S",
    re.UNICODE,
)

# Compact option: "A.text" or "A．text" without trailing space (some OCR output)
_OPTION_COMPACT_RE = re.compile(
    r"^([A-Da-d])[.．]\S",
    re.UNICODE,
)

# Four options on one line: "A. x   B. y   C. z   D. w" (common in compact exams)
# Captures A through D with their labels
_INLINE_OPTIONS_RE = re.compile(
    r"(?:^|\s+)([A-Da-d])\s*[.、．\)）]\s+(\S[^A-Da-d]*?)(?=\s+[A-Da-d]\s*[.、．\)）]|$)",
    re.UNICODE,
)

# Detect if a line likely contains 2-4 options inline
_MULTI_OPTION_TRIGGER = re.compile(
    r"[A-Da-d]\s*[.、．\)）].{1,60}[B-Db-d]\s*[.、．\)）]",
    re.UNICODE,
)

# 1. text / 1) text / （1）text / [1] text
_STEM_RE = re.compile(
    r"^\(?(\d{1,3})\)?\s*[.、．]\s+\S",
    re.UNICODE,
)
_STEM_RE2 = re.compile(r"^(\d{1,3})\.\s+\S")
_STEM_RE3 = re.compile(r"^\[(\d{1,3})\]\s+\S")   # [1] format
_STEM_RE4 = re.compile(r"^（(\d{1,3})）\s*\S")    # full-width brackets （1）

# Section / part headers
_SECTION_RE = re.compile(
    r"^(Part\s+[IVXivx\d]+|Section\s+[A-Za-z\d]+|Reading\s+Comprehension"
    r"|Cloze\s+Test|Grammar\s+Fill|Listening\s+Comprehension"
    r"|Translation|Composition|Writing|Essay|Passage\s+\d"
    r"|Text\s+[A-E\d]|Dialogue\s+Completion|Error\s+Correction"
    r"|Blank\s+Filling|Word\s+Formation|Sentence\s+Rewriting)",
    re.IGNORECASE,
)

# Short Roman-numeral sections: "I. " "II. "
_ROMAN_RE = re.compile(r"^(I{1,3}V?|IV|VI{0,3}|IX|XI?)[.\s]\s", re.UNICODE)

# Lines that are instruction-like keywords
_INSTRUCTION_LEAD_RE = re.compile(
    r"^(Directions?|Note[:\s]|Instructions?|Choose|Select|Fill\s+in"
    r"|Complete|Answer\s+the|Read\s+the|Listen|Translate|Write|Based\s+on"
    r"|According\s+to|For\s+each\s+of|Decide|Pick|Indicate|Mark|Underline"
    r"|Match|Circle|Identify|Look\s+at|Refer\s+to|Use\s+the)",
    re.IGNORECASE,
)

_INSTRUCTION_WORDS = frozenset(
    "choose select fill complete answer write read listen translate match "
    "circle underline note choose pick decide indicate identify mark refer".split()
)

# Cloze blank markers: "(   )" / "__" / "____" / "（   ）"
_BLANK_MARKER_RE = re.compile(r"_{2,}|\(\s{2,}\)|（\s*）")


# ── Pre-processor: split compound option lines ────────────────────────────────

def _split_inline_options(line: str) -> List[str] | None:
    """
    If *line* contains 2-4 options on one line (e.g. "A. foo  B. bar  C. baz  D. qux"),
    split them into individual option lines. Returns None if not a compound line.
    """
    stripped = line.strip()
    if not _MULTI_OPTION_TRIGGER.search(stripped):
        return None

    # Split on option-label boundaries: look ahead for next [A-D][.)
    parts = re.split(r"(?<!\w)\s+(?=[A-Da-d]\s*[.、．\)）]\s)", stripped)
    if len(parts) < 2:
        return None

    result = []
    for part in parts:
        part = part.strip()
        if part:
            result.append(part)

    # Validate: each part should start with an option label
    valid = all(
        re.match(r"^[A-Da-d]\s*[.、．\)）]", p) for p in result
    )
    return result if valid and len(result) >= 2 else None


# ── Per-line classifier ───────────────────────────────────────────────────────

def _classify_line(line: str) -> AnnotatedLine:
    stripped = line.strip()

    if not stripped:
        return AnnotatedLine(stripped, LineType.BLANK)

    # Option check (highest priority before stem, because "A." could be stem-like)
    m = _OPTION_RE.match(stripped)
    if m:
        return AnnotatedLine(stripped, LineType.OPTION, option_label=m.group(1).upper())

    # Compact option without space (OCR artefact)
    m = _OPTION_COMPACT_RE.match(stripped)
    if m and len(stripped) > 2:
        return AnnotatedLine(stripped, LineType.OPTION, option_label=m.group(1).upper())

    # Stem check — multiple formats
    if (
        _STEM_RE.match(stripped)
        or _STEM_RE2.match(stripped)
        or _STEM_RE3.match(stripped)
        or _STEM_RE4.match(stripped)
    ):
        return AnnotatedLine(stripped, LineType.STEM)

    # Section header
    if _SECTION_RE.match(stripped) or _ROMAN_RE.match(stripped):
        return AnnotatedLine(stripped, LineType.TITLE)

    # All-uppercase short line → title
    alpha_only = "".join(ch for ch in stripped if ch.isalpha() or ch == " ")
    if (
        len(stripped) <= 80
        and len(stripped) >= 4
        and alpha_only.strip() == alpha_only.strip().upper()
        and len(alpha_only.strip()) > 0
    ):
        return AnnotatedLine(stripped, LineType.TITLE)

    # Instruction directive
    if _INSTRUCTION_LEAD_RE.match(stripped):
        return AnnotatedLine(stripped, LineType.INSTRUCTION)

    # Short line (≤12 words) containing instruction verbs but no period → instruction
    words = stripped.lower().split()
    if len(words) <= 12 and any(w in _INSTRUCTION_WORDS for w in words) and not stripped.endswith("."):
        return AnnotatedLine(stripped, LineType.INSTRUCTION)

    # Line with cloze blanks → likely body/stem
    if _BLANK_MARKER_RE.search(stripped):
        # If short (≤25 words) and has a blank, probably a stem
        if len(words) <= 25:
            return AnnotatedLine(stripped, LineType.STEM)

    return AnnotatedLine(stripped, LineType.BODY)


# ── Pre-processing: expand compound option lines ──────────────────────────────

def _preprocess_lines(raw_lines: List[str]) -> List[str]:
    """Expand lines containing multiple inline options into separate lines."""
    result: List[str] = []
    for line in raw_lines:
        expanded = _split_inline_options(line)
        if expanded:
            result.extend(expanded)
        else:
            result.append(line)
    return result


# ── Context smoothing ─────────────────────────────────────────────────────────

def _smooth(lines: List[AnnotatedLine]) -> List[AnnotatedLine]:
    """
    Multi-pass context-aware reclassification.
    Rules applied sequentially; adjust here to tune behaviour.
    """
    result = list(lines)
    n = len(result)

    for i in range(1, n - 1):
        prev = result[i - 1]
        curr = result[i]
        nxt = result[i + 1]

        # Long TITLE sandwiched between BODY lines → BODY
        if (
            curr.line_type == LineType.TITLE
            and len(curr.text) > 80
            and prev.line_type == LineType.BODY
            and nxt.line_type == LineType.BODY
        ):
            result[i] = AnnotatedLine(curr.text, LineType.BODY)
            continue

        # Short BODY line (≤6 words) immediately before an OPTION → STEM
        if (
            curr.line_type == LineType.BODY
            and len(curr.text.split()) <= 6
            and nxt.line_type == LineType.OPTION
        ):
            result[i] = AnnotatedLine(curr.text, LineType.STEM)
            continue

        # INSTRUCTION > 40 words → probably BODY
        if curr.line_type == LineType.INSTRUCTION and len(curr.text.split()) > 40:
            result[i] = AnnotatedLine(curr.text, LineType.BODY)
            continue

        # BODY line ≤ 12 words sandwiched between STEM and OPTION → STEM continuation
        if (
            curr.line_type == LineType.BODY
            and len(curr.text.split()) <= 12
            and prev.line_type == LineType.STEM
            and nxt.line_type == LineType.OPTION
        ):
            result[i] = AnnotatedLine(curr.text, LineType.STEM)
            continue

        # Lone TITLE line between OPTIONS → probably a stray stem label, classify as STEM
        if (
            curr.line_type == LineType.TITLE
            and prev.line_type == LineType.OPTION
            and nxt.line_type == LineType.OPTION
            and len(curr.text.split()) <= 8
        ):
            result[i] = AnnotatedLine(curr.text, LineType.STEM)
            continue

    # Second pass: OPTION lines surrounded by BODY on both sides with no nearby STEM
    # → reclassify as BODY (false positive — e.g. "A. Einstein" in reading passage)
    for i in range(1, n - 1):
        curr = result[i]
        if curr.line_type != LineType.OPTION:
            continue
        prev = result[i - 1]
        nxt = result[i + 1]
        # Check no STEM or OPTION nearby (within 3 lines)
        context_start = max(0, i - 3)
        context_end = min(n, i + 4)
        has_nearby_structure = any(
            result[j].line_type in (LineType.STEM, LineType.OPTION)
            for j in range(context_start, context_end)
            if j != i
        )
        if (
            prev.line_type == LineType.BODY
            and nxt.line_type == LineType.BODY
            and not has_nearby_structure
        ):
            result[i] = AnnotatedLine(curr.text, LineType.BODY)

    return result


# ── Public API ────────────────────────────────────────────────────────────────

def recognize_structure(text: str) -> StructuredText:
    """
    Parse *text* into a StructuredText with per-line type annotations.

    Call .body_text / .stem_text / .option_text for segmented content,
    or iterate .annotated_lines for fine-grained access.

    Handles compound option lines (all 4 options on one line) by splitting
    them before classification.
    """
    raw_lines = text.split("\n")
    expanded = _preprocess_lines(raw_lines)
    annotated = [_classify_line(ln) for ln in expanded]
    annotated = _smooth(annotated)
    return StructuredText(annotated_lines=annotated)
