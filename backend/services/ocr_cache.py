"""
OCR result cache — stores extracted text keyed by SHA-256(file bytes + OCR settings).

This allows re-analysis with different NLP/filter parameters without re-running OCR,
which is the most time-consuming step for scanned PDFs and images.

Cache files are stored as JSON under data/ocr_cache/.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE_DIR: Optional[Path] = None


def _get_cache_dir() -> Path:
    global _CACHE_DIR
    if _CACHE_DIR is None:
        from backend.config import settings
        _CACHE_DIR = Path(settings.ocr_cache_dir)
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR


def _file_hash(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _settings_hash(ocr_engine: str, language: str, backend: str) -> str:
    key = f"{ocr_engine}:{language}:{backend}"
    return hashlib.md5(key.encode()).hexdigest()[:8]


def cache_key(file_path: Path, ocr_engine: str = "auto", language: str = "eng", backend: str = "local") -> str:
    fh = _file_hash(file_path)
    sh = _settings_hash(ocr_engine, language, backend)
    return f"{fh[:16]}_{sh}"


def get_cached(file_path: Path, ocr_engine: str = "auto", language: str = "eng", backend: str = "local") -> Optional[dict]:
    """Return cached OCR result dict or None if not cached."""
    key = cache_key(file_path, ocr_engine, language, backend)
    cache_file = _get_cache_dir() / f"{key}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        logger.info("OCR cache hit for %s (key=%s)", file_path.name, key)
        return data
    except Exception as exc:
        logger.warning("Failed to read OCR cache for %s: %s", file_path.name, exc)
        return None


def save_cache(
    file_path: Path,
    result: dict,
    ocr_engine: str = "auto",
    language: str = "eng",
    backend: str = "local",
) -> None:
    """Persist OCR result to cache."""
    key = cache_key(file_path, ocr_engine, language, backend)
    cache_file = _get_cache_dir() / f"{key}.json"
    try:
        payload = {**result, "_cached_at": time.time(), "_source_file": file_path.name}
        cache_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("OCR result cached for %s (key=%s)", file_path.name, key)
    except Exception as exc:
        logger.warning("Failed to write OCR cache for %s: %s", file_path.name, exc)


def invalidate(file_path: Path) -> int:
    """Delete all cache entries for a file (any OCR settings). Returns number deleted."""
    fh = _file_hash(file_path)
    prefix = fh[:16]
    deleted = 0
    for f in _get_cache_dir().glob(f"{prefix}_*.json"):
        try:
            f.unlink()
            deleted += 1
        except Exception:
            pass
    return deleted


def clear_all() -> int:
    """Delete the entire OCR cache. Returns number of files deleted."""
    deleted = 0
    for f in _get_cache_dir().glob("*.json"):
        try:
            f.unlink()
            deleted += 1
        except Exception:
            pass
    return deleted


def cache_stats() -> dict:
    """Return stats about the current cache."""
    cache_dir = _get_cache_dir()
    files = list(cache_dir.glob("*.json"))
    total_size = sum(f.stat().st_size for f in files if f.exists())
    return {
        "count": len(files),
        "total_size_bytes": total_size,
        "total_size_mb": round(total_size / 1024 / 1024, 2),
        "cache_dir": str(cache_dir),
    }
