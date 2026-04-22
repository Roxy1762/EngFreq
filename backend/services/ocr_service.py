"""OCR service with preprocessing and optional RapidOCR fallback."""
from __future__ import annotations

import io
import logging
import tempfile
from pathlib import Path
from statistics import mean
from typing import Iterable, List, Tuple

from PIL import Image, ImageFilter, ImageOps

from backend.config import settings
from backend.services.runtime_config import get_runtime_config

logger = logging.getLogger(__name__)

_RAPIDOCR_ENGINE = None


def _get_ocr_config():
    try:
        return get_runtime_config().ocr
    except Exception as exc:
        logger.debug("Falling back to default OCR config: %s", exc)
        class _Fallback:
            engine = "auto"
            language = settings.ocr_language
            pdf_dpi = 300
            page_segmentation_mode = 6
            preprocess = True
            binary_threshold = 180
            upscale_factor = 2.0
            sharpen = True
            auto_rotate = False
            fallback_to_tesseract = True

        return _Fallback()


def _get_tesseract():
    try:
        import pytesseract
    except ImportError as exc:
        raise RuntimeError("pytesseract not installed. Run: pip install pytesseract") from exc

    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd

    return pytesseract


def _get_rapidocr():
    global _RAPIDOCR_ENGINE
    if _RAPIDOCR_ENGINE is not None:
        return _RAPIDOCR_ENGINE

    try:
        from rapidocr import RapidOCR

        _RAPIDOCR_ENGINE = RapidOCR()
        return _RAPIDOCR_ENGINE
    except Exception as exc:
        logger.debug("RapidOCR unavailable: %s", exc)
        _RAPIDOCR_ENGINE = False
        return _RAPIDOCR_ENGINE


def _prepare_image_variants(img: Image.Image) -> List[Tuple[str, Image.Image]]:
    cfg = _get_ocr_config()
    base = ImageOps.exif_transpose(img).convert("RGB")
    if cfg.upscale_factor and cfg.upscale_factor > 1:
        width = max(1, int(base.width * cfg.upscale_factor))
        height = max(1, int(base.height * cfg.upscale_factor))
        base = base.resize((width, height), Image.Resampling.LANCZOS)

    gray = ImageOps.autocontrast(base.convert("L"))
    if cfg.sharpen:
        gray = gray.filter(ImageFilter.SHARPEN)
    denoised = gray.filter(ImageFilter.MedianFilter(size=3))

    variants: List[Tuple[str, Image.Image]] = [("gray", gray)]
    if cfg.preprocess:
        thresholded = denoised.point(lambda p: 255 if p >= cfg.binary_threshold else 0)
        variants.append(("binary", thresholded))
        boosted = ImageOps.autocontrast(denoised)
        variants.append(("enhanced", boosted))
    variants.append(("original", base))
    return variants


def _normalise_ocr_text(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def _ocr_tesseract_image(img: Image.Image) -> str:
    pytesseract = _get_tesseract()
    cfg = _get_ocr_config()
    lang = cfg.language or settings.ocr_language

    best_text = ""
    best_score = float("-inf")
    page_modes = [cfg.page_segmentation_mode, 6, 11]

    for variant_name, variant in _prepare_image_variants(img):
        for psm in dict.fromkeys(page_modes):
            config = f"--oem 3 --psm {psm}"
            try:
                data = pytesseract.image_to_data(
                    variant,
                    lang=lang,
                    config=config,
                    output_type=pytesseract.Output.DICT,
                )
            except Exception as exc:
                logger.debug("Tesseract OCR failed for %s/psm=%s: %s", variant_name, psm, exc)
                continue

            tokens = []
            confidences = []
            for raw_text, raw_conf in zip(data.get("text", []), data.get("conf", [])):
                text = (raw_text or "").strip()
                if not text:
                    continue
                tokens.append(text)
                try:
                    conf = float(raw_conf)
                except (TypeError, ValueError):
                    conf = -1
                if conf >= 0:
                    confidences.append(conf)

            text = _normalise_ocr_text("\n".join(tokens))
            if not text:
                continue

            confidence = mean(confidences) if confidences else 0.0
            score = confidence + min(len(text), 500) / 25.0
            if score > best_score:
                best_score = score
                best_text = text

    if not best_text:
        try:
            best_text = _normalise_ocr_text(
                pytesseract.image_to_string(img, lang=lang, config=f"--oem 3 --psm {cfg.page_segmentation_mode}")
            )
        except Exception as exc:
            raise RuntimeError(f"Tesseract OCR failed: {exc}") from exc

    return best_text


def _extract_rapidocr_text(result, _depth: int = 0) -> str:
    if result is None:
        return ""
    if not result and not isinstance(result, (list, tuple)):
        return ""

    # Named attribute API (newer RapidOCR versions)
    if hasattr(result, "txts"):
        texts = getattr(result, "txts")
        if isinstance(texts, (list, tuple)):
            return _normalise_ocr_text("\n".join(str(item) for item in texts if str(item).strip()))

    if hasattr(result, "txt"):
        text = getattr(result, "txt")
        if isinstance(text, str):
            return _normalise_ocr_text(text)

    # Prevent infinite recursion
    if _depth > 3:
        return ""

    if isinstance(result, tuple):
        # RapidOCR typically returns (data, elapsed_time) — try each element
        for item in result:
            if item is None:
                continue
            # Skip numeric values (elapsed time, confidence scores)
            if isinstance(item, (int, float)):
                continue
            text = _extract_rapidocr_text(item, _depth + 1)
            if text:
                return text
        return ""

    if isinstance(result, list):
        chunks = []
        for item in result:
            if item is None:
                continue
            if isinstance(item, (list, tuple)):
                # Each detection: [box_coords, text_str, confidence_float]
                # box_coords is a list/array, text is a str, confidence is a float
                str_parts = [part for part in item if isinstance(part, str) and part.strip()]
                if str_parts:
                    # Take the first string part (the OCR text, not metadata)
                    chunks.append(str_parts[0])
                    continue
            if isinstance(item, dict):
                text = item.get("text") or item.get("txt") or item.get("rec_txt")
                if text:
                    chunks.append(str(text))
                    continue
            if isinstance(item, str) and item.strip():
                chunks.append(item)
        if chunks:
            return _normalise_ocr_text("\n".join(chunks))
        return ""

    return ""


def _ocr_with_rapidocr(path: Path) -> str:
    engine = _get_rapidocr()
    if not engine:
        raise RuntimeError("RapidOCR is not available")

    try:
        result = engine(str(path))
    except Exception as exc:
        raise RuntimeError(f"RapidOCR failed: {exc}") from exc

    logger.debug("RapidOCR raw result type: %s", type(result).__name__)
    text = _extract_rapidocr_text(result)
    if not text:
        logger.warning("RapidOCR returned no text for %s (result type: %s)", path.name, type(result).__name__)
        raise RuntimeError("RapidOCR returned no text")
    logger.info("RapidOCR extracted %d chars from %s", len(text), path.name)
    return text


def _ocr_image_core(path: Path, img: Image.Image) -> str:
    cfg = _get_ocr_config()
    engine = (cfg.engine or "auto").lower()

    if engine in {"auto", "rapidocr"}:
        try:
            return _ocr_with_rapidocr(path)
        except Exception as exc:
            logger.info("RapidOCR unavailable or failed for %s: %s", path.name, exc)
            if engine == "rapidocr" and not cfg.fallback_to_tesseract:
                raise

    return _ocr_tesseract_image(img)


def ocr_image(path: Path) -> str:
    """Run OCR on a single image file."""
    with Image.open(str(path)) as raw:
        img = raw.copy()
    result = _ocr_image_core(path, img)
    if not result.strip():
        raise RuntimeError(
            f"OCR extracted 0 characters from {path.name}. "
            "The image may be blank or the OCR engine failed to read it."
        )
    return result


def _pdf_page_images(path: Path) -> Iterable[Image.Image]:
    cfg = _get_ocr_config()

    try:
        from pdf2image import convert_from_path

        for img in convert_from_path(str(path), dpi=cfg.pdf_dpi):
            yield img
        return
    except ImportError:
        logger.warning("pdf2image not available, trying PyMuPDF")

    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(path))
        try:
            for page in doc:
                pix = page.get_pixmap(dpi=cfg.pdf_dpi)
                yield Image.open(io.BytesIO(pix.tobytes("png")))
        finally:
            doc.close()
        return
    except ImportError as exc:
        raise RuntimeError(
            "Cannot OCR PDF: install either pdf2image+poppler or PyMuPDF.\n"
            "  pip install pdf2image PyMuPDF"
        ) from exc


def ocr_pdf(path: Path) -> str:
    """Convert each PDF page to an image and run OCR."""
    texts: List[str] = []

    for index, img in enumerate(_pdf_page_images(path), start=1):
        page_text = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=f"_page_{index}.png", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            img.save(tmp_path)
            try:
                page_text = _ocr_image_core(tmp_path, img)
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("OCR failed for PDF page %s: %s", index, exc)
            try:
                page_text = _ocr_tesseract_image(img)
            except Exception as inner_exc:
                logger.error("Tesseract fallback failed for PDF page %s: %s", index, inner_exc)
                page_text = ""

        if page_text:
            texts.append(page_text)

    result = "\n\n".join(texts)
    if not result.strip():
        raise RuntimeError(
            "OCR extracted 0 characters from the PDF. "
            "The file may be blank, corrupted, or require a different OCR engine. "
            "Try enabling MinerU or switching the OCR engine in admin settings."
        )
    return result
