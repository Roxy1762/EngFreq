"""
File handler — detects file type and routes to the correct extractor.

Supported types:
  - .txt                  → direct text read
  - .pdf                  → pdfplumber (text layer) or OCR fallback
  - .docx                 → python-docx
  - .png/.jpg/.jpeg/.bmp  → OCR
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ExtractedText:
    text: str
    used_ocr: bool
    backend: str = "local"
    raw_result: dict | None = None

# Lazy imports so missing libraries don't crash startup
def _extract_txt(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_text(errors="replace")


def _extract_pdf_pymupdf(path: Path) -> Optional[Tuple[str, int]]:
    """Primary PDF extractor. Returns (text, total_chars) or None if unavailable."""
    try:
        import fitz   # PyMuPDF
    except ImportError:
        return None
    try:
        pages_text: list[str] = []
        total_chars = 0
        with fitz.open(str(path)) as doc:
            for page in doc:
                t = page.get_text("text") or ""
                pages_text.append(t)
                total_chars += len(t.strip())
        return "\n".join(pages_text), total_chars
    except Exception as exc:   # noqa: BLE001
        logger.warning("PyMuPDF extraction failed for %s: %s — falling back to pdfplumber", path.name, exc)
        return None


def _extract_pdf_pdfplumber(path: Path) -> Tuple[str, int]:
    """Fallback PDF extractor — more accurate for tables, slower overall."""
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError("No PDF extractor installed. Run: pip install PyMuPDF pdfplumber") from exc

    pages_text: list[str] = []
    total_chars = 0
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            pages_text.append(t)
            total_chars += len(t.strip())
    return "\n".join(pages_text), total_chars


def _extract_pdf(path: Path) -> Tuple[str, bool]:
    """
    Extract text from a PDF.

    Pipeline:
      1. PyMuPDF (fastest, handles most modern PDFs)
      2. pdfplumber (slower but reads some malformed PDFs PyMuPDF skips,
         AND tried whenever PyMuPDF returns empty text — some PDFs encode
         their content in a way fitz can't decode but pdfplumber can)
      3. OCR (only if both text-layer extractors return less than the
         configured threshold of characters)

    Returns (text, used_ocr).
    """
    # Use configurable threshold to decide if PDF is scanned
    try:
        from backend.services.runtime_config import get_runtime_config
        threshold = get_runtime_config().ocr.pdf_ocr_threshold
    except Exception as exc:
        logger.debug("Runtime config unavailable, using default OCR threshold: %s", exc)
        threshold = 50

    full_text = ""
    total_chars = 0

    pymupdf_result = _extract_pdf_pymupdf(path)
    if pymupdf_result is not None:
        full_text, total_chars = pymupdf_result

    # If PyMuPDF was unavailable OR returned suspiciously little text, retry
    # with pdfplumber before falling back to expensive OCR.
    if pymupdf_result is None or total_chars < threshold:
        try:
            plumber_text, plumber_chars = _extract_pdf_pdfplumber(path)
        except RuntimeError:
            # Both extractors unavailable — propagate the original error so
            # the user sees a clear "install PyMuPDF or pdfplumber" message.
            if pymupdf_result is None:
                raise
            plumber_text, plumber_chars = "", 0
        except Exception as exc:   # noqa: BLE001
            logger.warning("pdfplumber extraction failed for %s: %s", path.name, exc)
            plumber_text, plumber_chars = "", 0

        # Use whichever extractor pulled more text — sometimes pdfplumber
        # finds content where PyMuPDF returns garbage (or vice versa).
        if plumber_chars > total_chars:
            full_text, total_chars = plumber_text, plumber_chars

    if total_chars < threshold:
        logger.info(
            "PDF appears to be scanned (chars=%d < threshold=%d) — falling back to OCR",
            total_chars, threshold,
        )
        from backend.services.ocr_service import ocr_pdf
        return ocr_pdf(path), True

    return full_text, False


def _extract_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError:
        raise RuntimeError("python-docx not installed. Run: pip install python-docx")

    doc = Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs)


def _extract_image(path: Path) -> str:
    from backend.services.ocr_service import ocr_image
    return ocr_image(path)


def _extract_text_local(path: Path) -> ExtractedText:
    suffix = path.suffix.lower()

    if suffix == ".txt":
        text = _extract_txt(path)
        return ExtractedText(
            text=text,
            used_ocr=False,
            backend="local",
            raw_result={"service": "local", "source_type": "txt", "text": text},
        )

    if suffix == ".pdf":
        text, used_ocr = _extract_pdf(path)
        return ExtractedText(
            text=text,
            used_ocr=used_ocr,
            backend="local",
            raw_result={
                "service": "local",
                "source_type": "pdf",
                "mode": "ocr" if used_ocr else "text-layer",
                "text": text,
            },
        )

    if suffix == ".docx":
        text = _extract_docx(path)
        return ExtractedText(
            text=text,
            used_ocr=False,
            backend="local",
            raw_result={"service": "local", "source_type": "docx", "text": text},
        )

    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}:
        text = _extract_image(path)
        return ExtractedText(
            text=text,
            used_ocr=True,
            backend="local",
            raw_result={"service": "local", "source_type": "image", "text": text},
        )

    raise ValueError(f"Unsupported file type: {suffix}")


def extract_text(path: Path, use_cache: bool = True) -> ExtractedText:
    """
    Extract raw text from a file, with OCR result caching.

    When use_cache=True (default), the OCR result is loaded from disk cache if
    available — so re-analysis with different NLP/filter settings does NOT repeat
    the expensive OCR step.

    Returns:
        ExtractedText containing text, OCR flag, backend, and optional raw result.
    """
    from backend.services.runtime_config import get_runtime_config

    runtime = get_runtime_config()

    # ── MinerU backend ────────────────────────────────────────────────────────
    if runtime.parse_backend == "mineru" and runtime.mineru.enabled:
        if use_cache:
            try:
                from backend.services.ocr_cache import get_cached
                cached = get_cached(path, ocr_engine="mineru", language=runtime.ocr.language, backend="mineru")
                if cached and cached.get("text"):
                    return ExtractedText(
                        text=cached["text"],
                        used_ocr=cached.get("used_ocr", True),
                        backend="mineru",
                        raw_result=cached.get("raw_result"),
                    )
            except Exception as exc:   # noqa: BLE001
                logger.debug("MinerU cache lookup failed for %s: %s", path.name, exc)

        try:
            from backend.services.mineru_service import parse_file

            parsed = parse_file(path)
            result = ExtractedText(
                text=parsed["text"],
                used_ocr=parsed["used_ocr"],
                backend=parsed.get("backend", "mineru"),
                raw_result=parsed.get("raw_result"),
            )
            if use_cache and result.text:
                try:
                    from backend.services.ocr_cache import save_cache
                    save_cache(
                        path,
                        {"text": result.text, "used_ocr": result.used_ocr, "raw_result": result.raw_result},
                        ocr_engine="mineru",
                        language=runtime.ocr.language,
                        backend="mineru",
                    )
                except Exception as exc:   # noqa: BLE001
                    logger.debug("MinerU cache write failed for %s: %s", path.name, exc)
            return result
        except Exception as exc:
            if runtime.mineru.fallback_to_local:
                logger.warning("MinerU parsing failed for %s, falling back to local extraction: %s", path.name, exc)
            else:
                raise

    # ── Local backend with cache ──────────────────────────────────────────────
    suffix = path.suffix.lower()
    is_ocr_needed = suffix in {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"} or suffix == ".pdf"

    if use_cache and is_ocr_needed and runtime.ocr_cache_enabled:
        try:
            from backend.services.ocr_cache import get_cached
            cached = get_cached(
                path,
                ocr_engine=runtime.ocr.engine,
                language=runtime.ocr.language,
                backend="local",
            )
            # Only honour the cache hit when there is real text — otherwise a
            # previously-failed OCR run could permanently shadow a working
            # retry after the user tweaks settings.
            if cached and cached.get("text"):
                return ExtractedText(
                    text=cached["text"],
                    used_ocr=cached.get("used_ocr", True),
                    backend="local",
                    raw_result=cached.get("raw_result"),
                )
        except Exception as exc:   # noqa: BLE001
            logger.debug("OCR cache lookup failed for %s: %s", path.name, exc)

    result = _extract_text_local(path)

    if use_cache and result.used_ocr and result.text and runtime.ocr_cache_enabled:
        try:
            from backend.services.ocr_cache import save_cache
            save_cache(
                path,
                {"text": result.text, "used_ocr": result.used_ocr, "raw_result": result.raw_result},
                ocr_engine=runtime.ocr.engine,
                language=runtime.ocr.language,
                backend="local",
            )
        except Exception as exc:   # noqa: BLE001
            logger.debug("OCR cache write failed for %s: %s", path.name, exc)

    return result
