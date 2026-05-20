"""
Merriam-Webster Collegiate Dictionary API provider.

Requires a free API key from https://dictionaryapi.com/
Set MERRIAM_WEBSTER_KEY in .env, or configure via the admin panel.

Refactored to use `HttpDictProviderBase` for shared concurrency + on-disk cache.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from backend.config import settings
from backend.providers._http_base import HttpDictProviderBase
from backend.services.dict_cache import CachedDefinition

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.dictionaryapi.com/api/v3/references/collegiate/json/{word}"


def _get_mw_key() -> str:
    try:
        from backend.services.runtime_config import get_runtime_config
        key = get_runtime_config().dict_providers.merriam_webster_key
        if key:
            return key
    except Exception:
        pass
    return settings.merriam_webster_key or ""


class MerriamWebsterProvider(HttpDictProviderBase):
    name = "merriam_webster"
    concurrency = 5
    timeout_seconds = 10.0

    def __init__(self):
        # Resolve the API key once at construction. Previously every word
        # lookup re-fetched the runtime config (a SQLite read per word) just
        # to pull the key — that was the slowest part of a 50-word batch.
        self._api_key = _get_mw_key()
        if not self._api_key:
            raise RuntimeError(
                "Merriam-Webster API key not configured. "
                "Set it in the admin panel under '词典工具密钥' or via MERRIAM_WEBSTER_KEY in .env"
            )

    async def _lookup_one(
        self,
        client: httpx.AsyncClient,
        word: str,
    ) -> Optional[CachedDefinition]:
        url = _BASE_URL.format(word=word)
        # Pass timeout explicitly: httpx's per-call timeout overrides the
        # client default. Some MW responses for rare words have been observed
        # to take 30+ seconds — without an explicit per-call timeout the
        # request inherits whatever the client default is at call time, which
        # changed between httpx versions.
        resp = await client.get(
            url,
            params={"key": self._api_key},
            timeout=self.timeout_seconds,
        )
        resp.raise_for_status()
        # Some MW errors return HTML for invalid keys; tolerate that.
        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning("Merriam-Webster returned non-JSON for '%s': %s", word, exc)
            return None
        return _parse(word, data)


def _parse(word: str, data: list) -> Optional[CachedDefinition]:
    if not data or isinstance(data[0], str):
        return None

    first = data[0]
    pos = first.get("fl", "")
    shortdefs = first.get("shortdef", [])
    en_def = shortdefs[0] if shortdefs else ""

    example = ""
    try:
        for section in first.get("def", []):
            for sseq in section.get("sseq", []):
                for sense in sseq:
                    if not isinstance(sense, list) or len(sense) < 2:
                        continue
                    for dt_item in sense[1].get("dt", []):
                        if isinstance(dt_item, list) and dt_item[0] == "vis":
                            for vis in dt_item[1]:
                                t = vis.get("t", "")
                                if t:
                                    example = re.sub(r"\{[^}]+\}", "", t).strip()
                                    break
                        if example:
                            break
                    if example:
                        break
                if example:
                    break
            if example:
                break
    except (IndexError, KeyError, TypeError):
        pass

    if not en_def and not example:
        return None
    return CachedDefinition(
        headword=word,
        pos=pos,
        english_definition=en_def,
        example_sentence=example,
    )
