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


def _strip_fence(text: str) -> str:
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text.strip()


def _balanced_slice(text: str, open_ch: str, close_ch: str) -> Optional[str]:
    """Extract the first balanced ``open_ch``…``close_ch`` slice.

    A greedy ``\\[.*\\]`` regex (the previous approach) captured everything
    between the first ``[`` and the last ``]``, swallowing intervening prose
    and confusing the JSON parser when the LLM wrote multiple blocks. This
    helper walks the string once, respecting nested brackets and quoted
    strings — so an LLM response with both a code-fenced array and a stray
    bracket later in the prose still parses cleanly.
    """
    start = text.find(open_ch)
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


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

    # Heuristic: pull the first balanced bracket pair. Try the preferred shape
    # first, then the other. Walking once respects nested structures, so an
    # array of objects parses correctly.
    pairs = [("[", "]"), ("{", "}")] if prefer != "object" else [("{", "}"), ("[", "]")]
    for open_ch, close_ch in pairs:
        slice_ = _balanced_slice(raw, open_ch, close_ch)
        if slice_ is None:
            continue
        try:
            return json.loads(slice_)
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
