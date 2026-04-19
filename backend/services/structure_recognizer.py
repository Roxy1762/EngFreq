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
It is NOT guaranteed to be perfect — but it produces useful signal for
weighted word-frequency analysis and can be improved incrementally.
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

# A. text  /  A) text  /  (A) text  /  A、text
_OPTION_RE = re.compile(
    r"^([A-Da-d])\s*[.、．\)）]\s+\S",
    re.UNICODE,
)

# 1. text  /  1) text  /  （1）text
_STEM_RE = re.compile(
    r"^\(?(\d{1,3})\)?\s*[.、．]\s+\S",
    re.UNICODE,
)
# Also catch plain "1. " at the start
_STEM_RE2 = re.compile(r"^(\d{1,3})\.\s+\S")

# Section / part headers
_SECTION_RE = re.compile(
    r"^(Part\s+[IVXivx\d]+|Section\s+[A-Za-z\d]+|Reading\s+Comprehension"
    r"|Cloze\s+Test|Grammar\s+Fill|Listening\s+Comprehension"
    r"|Translation|Composition|Writing|Essay|Passage\s+\d)",
    re.IGNORECASE,
)

# Short Roman-numeral sections: "I. " "II. "
_ROMAN_RE = re.compile(r"^(I{1,3}V?|IV|VI{0,3}|IX|XI?)[.\s]\s", re.UNICODE)

# Lines that are instruction-like keywords
_INSTRUCTION_LEAD_RE = re.compile(
    r"^(Directions?|Note[:\s]|Instructions?|Choose|Select|Fill\s+in"
    r"|Complete|Answer\s+the|Read\s+the|Listen|Translate|Write|Based\s+on"
    r"|According\s+to|For\s+each\s+of)",
    re.IGNORECASE,
)

_INSTRUCTION_WORDS = frozenset(
    "choose select fill complete answer write read listen translate match "
    "circle underline note choose pick decide indicate".split()
)


# ── Per-line classifier ───────────────────────────────────────────────────────

def _classify_line(line: str) -> AnnotatedLine:
    stripped = line.strip()

    if not stripped:
        return AnnotatedLine(stripped, LineType.BLANK)

    # Option check (highest priority before stem, because "A." could be stem-like)
    m = _OPTION_RE.match(stripped)
    if m:
        return AnnotatedLine(stripped, LineType.OPTION, option_label=m.group(1).upper())

    # Stem check
    if _STEM_RE.match(stripped) or _STEM_RE2.match(stripped):
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

    return AnnotatedLine(stripped, LineType.BODY)


# ── Context smoothing ─────────────────────────────────────────────────────────

def _smooth(lines: List[AnnotatedLine]) -> List[AnnotatedLine]:
    """
    Single-pass context-aware reclassification.
    Rules applied sequentially; adjust here to tune behaviour.
    """
    result = list(lines)
    n = len(result)

    for i in range(1, n - 1):
        prev = result[i - 1]
        curr = result[i]
        nxt = result[i + 1]

        # A TITLE line that is long and sandwiched between BODY lines → BODY
        if (
            curr.line_type == LineType.TITLE
            and len(curr.text) > 80
            and prev.line_type == LineType.BODY
            and nxt.line_type == LineType.BODY
        ):
            result[i] = AnnotatedLine(curr.text, LineType.BODY)
            continue

        # A BODY line that is very short (≤5 words) and immediately followed
        # by an OPTION line → likely the tail of a stem or a blank label;
        # reclassify as STEM for better weighting
        if (
            curr.line_type == LineType.BODY
            and len(curr.text.split()) <= 6
            and nxt.line_type == LineType.OPTION
        ):
            result[i] = AnnotatedLine(curr.text, LineType.STEM)
            continue

        # A line classified as INSTRUCTION that is > 40 words → probably BODY
        if curr.line_type == LineType.INSTRUCTION and len(curr.text.split()) > 40:
            result[i] = AnnotatedLine(curr.text, LineType.BODY)

    return result


# ── Public API ────────────────────────────────────────────────────────────────

def recognize_structure(text: str) -> StructuredText:
    """
    Parse *text* into a StructuredText with per-line type annotations.

    Call .body_text / .stem_text / .option_text for segmented content,
    or iterate .annotated_lines for fine-grained access.
    """
    raw_lines = text.split("\n")
    annotated = [_classify_line(ln) for ln in raw_lines]
    annotated = _smooth(annotated)
    return StructuredText(annotated_lines=annotated)
