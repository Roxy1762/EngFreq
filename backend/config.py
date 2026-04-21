"""Central configuration driven by environment variables or a local .env file."""

import os
from typing import Optional

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*args, **kwargs):
        return False

from pydantic_settings import BaseSettings


load_dotenv(override=False)


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 20120

    secret_key: Optional[str] = None
    admin_username: str = "admin"
    admin_password: str = "admin123"
    db_path: str = "app.db"

    # ── Anthropic Claude ──────────────────────────────────────────────────────
    anthropic_api_key: Optional[str] = None
    # Default to Opus 4.7 (current Anthropic flagship as of 2026)
    ai_model: str = "claude-opus-4-7"
    ai_batch_size: int = 25
    ai_prompt_caching: bool = True   # cache the long system prompt across calls

    # ── DeepSeek ──────────────────────────────────────────────────────────────
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"

    # ── Generic OpenAI-compatible ─────────────────────────────────────────────
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None   # None = use default OpenAI endpoint
    openai_model: str = "gpt-4o-mini"

    # ── Dictionary providers ──────────────────────────────────────────────────
    merriam_webster_key: Optional[str] = None
    oxford_app_id: Optional[str] = None
    oxford_app_key: Optional[str] = None
    youdao_app_key: Optional[str] = None
    youdao_app_secret: Optional[str] = None
    ecdict_path: Optional[str] = None       # Path to ecdict.csv or ecdict.db

    vocab_provider: str = "claude"

    # ── Scoring weights ───────────────────────────────────────────────────────
    weight_body: float = 1.0
    weight_stem: float = 1.5
    weight_option: float = 3.0

    # ── Analysis defaults ─────────────────────────────────────────────────────
    default_min_word_length: int = 2
    default_filter_stopwords: bool = False
    default_keep_proper_nouns: bool = True
    default_filter_numbers: bool = True

    # ── OCR ───────────────────────────────────────────────────────────────────
    tesseract_cmd: Optional[str] = None
    ocr_language: str = "eng"

    # ── Storage ───────────────────────────────────────────────────────────────
    upload_dir: str = "data/uploads"
    results_dir: str = "data/exports"
    ocr_cache_dir: str = "data/ocr_cache"
    file_store_dir: str = "data/files"      # Persistent copies of uploaded files

    # ── Text cleaner ──────────────────────────────────────────────────────────
    text_cleaner_backend: str = "none"      # none | claude | deepseek | openai

    # ── LLM retry policy ──────────────────────────────────────────────────────
    llm_retry_attempts: int = 3
    llm_retry_initial_delay: float = 1.5    # seconds
    llm_retry_max_delay: float = 20.0

    # ── Prompt domain (vocab enrichment) ──────────────────────────────────────
    # gaokao | ielts | cet — selects which prompt family is used by default.
    prompt_domain: str = "gaokao"
    prompt_version: str = "v2"

    # ── Upload limits ─────────────────────────────────────────────────────────
    max_upload_mb: int = 50                 # reject uploads larger than this

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()

os.makedirs(settings.upload_dir, exist_ok=True)
os.makedirs(settings.results_dir, exist_ok=True)
os.makedirs(settings.ocr_cache_dir, exist_ok=True)
os.makedirs(settings.file_store_dir, exist_ok=True)
