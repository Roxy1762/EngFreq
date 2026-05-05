"""
Free Dictionary API provider — https://dictionaryapi.dev/
No API key required.  Covers most common English words.

Built on `HttpDictProviderBase`, which gives us bounded concurrency, a single
shared httpx client, and on-disk lookup caching for free.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from backend.providers._http_base import HttpDictProviderBase
from backend.services.dict_cache import CachedDefinition

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.dictionaryapi.dev/api/v2/entries/en/{word}"


class FreeDictProvider(HttpDictProviderBase):
    name = "free_dict"
    concurrency = 5
    timeout_seconds = 10.0

    async def _lookup_one(
        self,
        client: httpx.AsyncClient,
        word: str,
    ) -> Optional[CachedDefinition]:
        url = _BASE_URL.format(word=word)
        resp = await client.get(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return _parse(word, data)


def _parse(word: str, data: list) -> Optional[CachedDefinition]:
    pos = ""
    en_def = ""
    example = ""
    phonetic = ""
    try:
        first = data[0]
        phonetic = (first.get("phonetic") or "").strip()
        meanings = first.get("meanings", [])
        if meanings:
            m = meanings[0]
            pos = m.get("partOfSpeech", "") or ""
            defs = m.get("definitions", [])
            if defs:
                en_def = defs[0].get("definition", "") or ""
                example = defs[0].get("example", "") or ""
    except (IndexError, KeyError, TypeError):
        return None

    if not (en_def or example):
        return None
    return CachedDefinition(
        headword=word,
        pos=pos,
        english_definition=en_def,
        example_sentence=example,
        notes=phonetic if phonetic else "",
    )
