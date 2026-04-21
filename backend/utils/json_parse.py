"""Defensive JSON parsing for LLM responses.

LLMs sometimes wrap JSON in ```json fences, add prose before/after, or emit
single quotes. These helpers recover valid JSON from those variants instead of
blowing up on the first stray backtick.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _strip_fence(text: str) -> str:
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text.strip()


def parse_json(raw: str, *, prefer: str = "auto") -> Optional[Any]:
    """
    Parse a JSON blob from an LLM response, tolerating code fences and prose.

    prefer: 'array' | 'object' | 'auto' — when trying heuristic fallbacks,
            which shape to look for first.

    Returns parsed value or None if nothing could be parsed.
    """
    if not raw:
        return None

    candidates = [
        raw.strip(),
        _strip_fence(raw),
    ]

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    # Heuristic: find outermost [...] or {...}
    patterns = (_ARRAY_RE, _OBJECT_RE) if prefer != "object" else (_OBJECT_RE, _ARRAY_RE)
    for pattern in patterns:
        m = pattern.search(raw)
        if not m:
            continue
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            continue

    logger.debug("parse_json: all attempts failed for input (len=%d)", len(raw))
    return None


def parse_json_array(raw: str) -> list:
    """Parse a JSON array; returns empty list on failure."""
    data = parse_json(raw, prefer="array")
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "words" in data and isinstance(data["words"], list):
        return data["words"]
    return []


def parse_json_object(raw: str) -> dict:
    """Parse a JSON object; returns empty dict on failure."""
    data = parse_json(raw, prefer="object")
    return data if isinstance(data, dict) else {}
