"""Pydantic data models shared across the application."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ── Request / config models ──────────────────────────────────────────────────

class FilterConfig(BaseModel):
    min_word_length: int = Field(2, ge=1, le=10)
    filter_stopwords: bool = False
    keep_proper_nouns: bool = True
    filter_numbers: bool = True
    filter_basic_words: bool = False
    basic_words_threshold: float = Field(5.7, ge=3.0, le=8.0)


class WeightConfig(BaseModel):
    weight_body: float = Field(1.0, ge=0)
    weight_stem: float = Field(1.5, ge=0)
    weight_option: float = Field(3.0, ge=0)

    def score(self, body: int, stem: int, option: int) -> float:
        return body * self.weight_body + stem * self.weight_stem + option * self.weight_option


class AnalyzeRequest(BaseModel):
    filters: FilterConfig = Field(default_factory=FilterConfig)
    weights: WeightConfig = Field(default_factory=WeightConfig)
    top_n: int = Field(50, ge=1, le=500)
    generate_vocab: bool = False   # trigger AI/dict vocab in same call


class MultiExamRequest(BaseModel):
    """Combine multiple existing exams for cross-exam frequency analysis."""
    exam_codes: List[str] = Field(..., min_length=1, max_length=20)
    top_n: int = Field(50, ge=5, le=300)
    provider: Optional[str] = None
    generate_vocab: bool = True


class VocabSelectionRequest(BaseModel):
    """Update which words are selected in a vocab list."""
    selections: Dict[str, bool]  # headword → selected


class AISelectRequest(BaseModel):
    """Ask AI to suggest word selection for a study goal."""
    task_id: str
    goal: str = "高考备考"
    max_words: int = Field(40, ge=5, le=200)
    provider: Optional[str] = None


class UserProfileUpdate(BaseModel):
    email: Optional[str] = None
    display_name: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=6)


# ── Result models ────────────────────────────────────────────────────────────

class WordEntry(BaseModel):
    """One surface-form word."""
    surface: str
    lemma: str
    pos: str = ""
    family_id: Optional[str] = None
    body_count: int = 0
    stem_count: int = 0
    option_count: int = 0
    total_count: int = 0
    score: float = 0.0


class LemmaEntry(BaseModel):
    """All surface forms collapsed to one lemma."""
    lemma: str
    pos: str = ""
    family_id: Optional[str] = None
    surface_forms: List[str] = Field(default_factory=list)
    body_count: int = 0
    stem_count: int = 0
    option_count: int = 0
    total_count: int = 0
    score: float = 0.0


class FamilyEntry(BaseModel):
    """All lemmas in the same derivational word family."""
    family_id: str
    members: List[str] = Field(default_factory=list)   # lemma strings
    body_count: int = 0
    stem_count: int = 0
    option_count: int = 0
    total_count: int = 0
    score: float = 0.0


class VocabEntry(BaseModel):
    """Rich vocabulary entry for one headword."""
    headword: str
    lemma: str
    family: Optional[str] = None
    pos: Optional[str] = None
    chinese_meaning: Optional[str] = None
    english_definition: Optional[str] = None
    example_sentence: Optional[str] = None
    notes: Optional[str] = None
    body_count: int = 0
    stem_count: int = 0
    option_count: int = 0
    total_count: int = 0
    score: float = 0.0
    source: str = ""     # "claude" | "free_dict" | "merriam_webster" | "oxford"
    word_level: Optional[str] = None  # "基础" | "高考" | "四六级" | "超纲"
    selected: bool = True             # manual/AI selection flag
    exam_sources: List[str] = Field(default_factory=list)  # exam codes contributing to this word


class StructureStats(BaseModel):
    total_lines: int = 0
    body_lines: int = 0
    stem_lines: int = 0
    option_lines: int = 0
    title_lines: int = 0
    body_tokens: int = 0
    stem_tokens: int = 0
    option_tokens: int = 0


class AnalysisResult(BaseModel):
    task_id: str
    filename: str
    parse_backend: str = "local"
    raw_parse_stored: bool = False
    structure_stats: StructureStats = Field(default_factory=StructureStats)
    word_table: List[WordEntry] = Field(default_factory=list)
    lemma_table: List[LemmaEntry] = Field(default_factory=list)
    family_table: List[FamilyEntry] = Field(default_factory=list)
    vocab_table: List[VocabEntry] = Field(default_factory=list)
    is_combined: bool = False          # True for multi-exam results
    source_exam_codes: List[str] = Field(default_factory=list)  # exam codes combined


class TaskStatus(BaseModel):
    task_id: str
    status: str           # pending | processing | done | error
    progress: int = 0     # 0-100
    message: str = ""
    result: Optional[AnalysisResult] = None
    error: Optional[str] = None
    exam_code: Optional[str] = None   # set after result saved to DB
    dict_code: Optional[str] = None   # set after vocab saved to DB
