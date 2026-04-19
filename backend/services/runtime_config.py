"""Runtime configuration persisted in SQLite and editable from the admin UI."""
from __future__ import annotations

import importlib.util
import json
import logging
import os
from shutil import which
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import AppSetting, SessionLocal

logger = logging.getLogger(__name__)

_RUNTIME_CONFIG_KEY = "runtime_config"


class AnalysisDefaults(BaseModel):
    min_word_length: int = Field(settings.default_min_word_length, ge=1, le=10)
    filter_stopwords: bool = settings.default_filter_stopwords
    keep_proper_nouns: bool = settings.default_keep_proper_nouns
    filter_numbers: bool = settings.default_filter_numbers
    filter_basic_words: bool = False
    basic_words_threshold: float = Field(5.7, ge=3.0, le=8.0)
    top_n: int = Field(50, ge=5, le=300)
    weight_body: float = Field(settings.weight_body, ge=0, le=20)
    weight_stem: float = Field(settings.weight_stem, ge=0, le=20)
    weight_option: float = Field(settings.weight_option, ge=0, le=20)


class OcrRuntimeConfig(BaseModel):
    engine: str = "auto"  # auto | tesseract | rapidocr
    language: str = settings.ocr_language
    pdf_dpi: int = Field(300, ge=150, le=600)
    page_segmentation_mode: int = Field(6, ge=3, le=13)
    preprocess: bool = True
    binary_threshold: int = Field(180, ge=0, le=255)
    upscale_factor: float = Field(2.0, ge=1.0, le=4.0)
    sharpen: bool = True
    auto_rotate: bool = False
    fallback_to_tesseract: bool = True
    pdf_ocr_threshold: int = Field(50, ge=0, le=2000)  # chars below which PDF is treated as scanned


class MinerURuntimeConfig(BaseModel):
    enabled: bool = False
    api_base: str = "https://mineru.net/api/v1/agent"
    language: str = "en"
    enable_table: bool = True
    enable_formula: bool = True
    is_ocr: bool = False
    page_range: Optional[str] = None
    poll_timeout_sec: int = Field(300, ge=30, le=1800)
    poll_interval_sec: int = Field(3, ge=1, le=30)
    fallback_to_local: bool = True


class TextCleanerConfig(BaseModel):
    backend: str = settings.text_cleaner_backend  # none | claude | deepseek | openai
    enabled: bool = False
    context_hint: str = "Chinese high school English exam, multiple choice"


class LLMProviderConfig(BaseModel):
    """Runtime overrides for LLM provider endpoints (supplements .env)."""
    anthropic_api_key: Optional[str] = settings.anthropic_api_key
    deepseek_api_key: Optional[str] = settings.deepseek_api_key
    deepseek_base_url: str = settings.deepseek_base_url
    deepseek_model: str = settings.deepseek_model
    openai_api_key: Optional[str] = settings.openai_api_key
    openai_base_url: Optional[str] = settings.openai_base_url
    openai_model: str = settings.openai_model
    ai_model: str = settings.ai_model


class RuntimeConfig(BaseModel):
    analysis: AnalysisDefaults = Field(default_factory=AnalysisDefaults)
    ocr: OcrRuntimeConfig = Field(default_factory=OcrRuntimeConfig)
    parse_backend: str = "local"  # local | mineru
    save_raw_parse_result: bool = False
    mineru: MinerURuntimeConfig = Field(default_factory=MinerURuntimeConfig)
    vocab_provider: str = settings.vocab_provider
    ai_model: str = settings.ai_model
    ai_batch_size: int = Field(settings.ai_batch_size, ge=1, le=50)
    registration_enabled: bool = True
    ocr_cache_enabled: bool = True
    text_cleaner: TextCleanerConfig = Field(default_factory=TextCleanerConfig)
    llm: LLMProviderConfig = Field(default_factory=LLMProviderConfig)


def _default_config() -> RuntimeConfig:
    return RuntimeConfig()


def detect_ocr_capabilities() -> Dict[str, Any]:
    tesseract_path = settings.tesseract_cmd or which("tesseract")
    return {
        "pytesseract": importlib.util.find_spec("pytesseract") is not None,
        "rapidocr": importlib.util.find_spec("rapidocr") is not None,
        "tesseract_cmd": tesseract_path or "",
        "tesseract_configured": bool((tesseract_path and os.path.exists(tesseract_path)) or which("tesseract")),
    }


def get_runtime_config(db: Optional[Session] = None) -> RuntimeConfig:
    owned_db = db is None
    if owned_db:
        db = SessionLocal()

    try:
        record = db.query(AppSetting).filter_by(key=_RUNTIME_CONFIG_KEY).first()
        if not record:
            return _default_config()
        payload = json.loads(record.value_json or "{}")
        defaults = _default_config().model_dump()
        merged = _deep_merge(defaults, payload)
        return RuntimeConfig.model_validate(merged)
    except Exception as exc:
        logger.warning("Failed to load runtime config, using defaults: %s", exc)
        return _default_config()
    finally:
        if owned_db and db is not None:
            db.close()


def save_runtime_config(payload: Dict[str, Any], db: Optional[Session] = None) -> RuntimeConfig:
    owned_db = db is None
    if owned_db:
        db = SessionLocal()

    try:
        current = get_runtime_config(db).model_dump()
        merged = _deep_merge(current, payload)
        model = RuntimeConfig.model_validate(merged)
        record = db.query(AppSetting).filter_by(key=_RUNTIME_CONFIG_KEY).first()
        if not record:
            record = AppSetting(key=_RUNTIME_CONFIG_KEY, value_json="{}")
            db.add(record)
        record.value_json = model.model_dump_json()
        db.commit()
        return model
    finally:
        if owned_db and db is not None:
            db.close()


def frontend_config_payload() -> Dict[str, Any]:
    config = get_runtime_config()
    from backend.services.ocr_cache import cache_stats
    wordlist_count = 0
    try:
        from backend.services.wordlist_service import gaokao_word_count
        wordlist_count = gaokao_word_count()
    except Exception:
        pass
    return {
        "defaults": config.model_dump(),
        "ocr_capabilities": detect_ocr_capabilities(),
        "parse_backends": ["local", "mineru"],
        "ocr_cache_stats": cache_stats(),
        "text_cleaner_backends": ["none", "claude", "deepseek", "openai"],
        "gaokao_word_count": wordlist_count,
    }


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
