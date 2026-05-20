"""
iCIBA (爱词霸/金山词霸) — public dictionary provider, no API key required.

Uses the long-stable iCIBA "dictionary mini" JSON endpoint. The endpoint is
public-facing and does not require authentication, but does ship its payload
inside an HTML wrapper for some words; we handle both shapes.

Returns Chinese 释义, part-of-speech, phonetic, and (when present) example
sentences — making it ideal for Chinese English learners.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from backend.providers._http_base import HttpDictProviderBase
from backend.services.dict_cache import CachedDefinition

logger = logging.getLogger(__name__)

_ICIBA_API = "https://dict-mobile.iciba.com/interface/index.php"
# Public web endpoint that returns JSON with rich part-of-speech information.
_ICIBA_DICT_API = "https://dict.iciba.com/dictionary/word/query/web"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.iciba.com/",
}


class ICIBAProvider(HttpDictProviderBase):
    name = "iciba"
    concurrency = 5
    timeout_seconds = 8.0

    async def _lookup_one(
        self,
        client: httpx.AsyncClient,
        word: str,
    ) -> Optional[CachedDefinition]:
        # Try the rich dict.iciba.com endpoint first (better POS + Chinese).
        try:
            params = {
                "client": "6", "word": word, "uid": "0", "type": "json",
                "key": "1000006",
            }
            resp = await client.get(
                _ICIBA_DICT_API,
                params=params,
                headers=_HEADERS,
                timeout=self.timeout_seconds,
            )
            if resp.status_code == 200:
                payload = _safe_json(resp, word=word, endpoint="dict")
                if payload is not None:
                    cached = self._parse_dict_response(word, payload)
                    if cached:
                        return cached
        except Exception as exc:   # noqa: BLE001
            logger.debug("iciba dict endpoint failed for '%s': %s", word, exc)

        # Fall back to the long-stable mobile interface.
        try:
            params = {
                "c": "word", "m": "getsuggest", "nums": "1",
                "client": "6", "is_need_mean": "1", "word": word,
            }
            resp = await client.get(
                _ICIBA_API,
                params=params,
                headers=_HEADERS,
                timeout=self.timeout_seconds,
            )
            resp.raise_for_status()
            payload = _safe_json(resp, word=word, endpoint="suggest")
            if payload is None:
                return None
            return self._parse_suggest_response(word, payload)
        except Exception as exc:   # noqa: BLE001
            logger.debug("iciba suggest failed for '%s': %s", word, exc)
            return None

    # ── Parsers ─────────────────────────────────────────────────────────────
    def _parse_dict_response(self, word: str, data: dict) -> Optional[CachedDefinition]:
        if not isinstance(data, dict):
            return None
        message = data.get("message") or {}
        # The dict.iciba.com JSON usually nests data under message.{word, baesInfo, …}
        word_data = message.get("word") if isinstance(message, dict) else None
        if not isinstance(word_data, dict):
            return None

        symbols = (word_data.get("baesInfo") or {}).get("symbols") or []
        # Symbols is a list of {parts:[{part,means}], ph_en, ph_am, …}
        pos_parts: list[str] = []
        chinese_parts: list[str] = []
        phonetic = ""
        for sym in symbols:
            ph_en = sym.get("ph_en") or sym.get("ph_am") or ""
            if ph_en and not phonetic:
                phonetic = f"/{ph_en}/"
            for part in sym.get("parts") or []:
                p = (part.get("part") or "").strip()
                means = part.get("means") or []
                clean_means = [self._clean_text(m) for m in means if m]
                if not clean_means:
                    continue
                if p:
                    chinese_parts.append(f"{p} {'，'.join(clean_means[:3])}")
                    pos_parts.append(p)
                else:
                    chinese_parts.append('，'.join(clean_means[:3]))

        if not chinese_parts:
            return None

        return CachedDefinition(
            headword=word,
            pos=_normalize_pos(pos_parts[0]) if pos_parts else "",
            chinese_meaning='；'.join(chinese_parts[:4]),
            english_definition="",
            example_sentence="",
            notes=phonetic,
        )

    def _parse_suggest_response(self, word: str, data: dict) -> Optional[CachedDefinition]:
        if not isinstance(data, dict):
            return None
        msg = data.get("message")
        if not isinstance(msg, list) or not msg:
            return None
        first = msg[0] if isinstance(msg[0], dict) else {}
        means = first.get("means") or first.get("paraphrase") or ""
        if not means:
            return None
        return CachedDefinition(
            headword=word,
            pos="",
            chinese_meaning=self._clean_text(str(means))[:300],
            english_definition="",
            example_sentence="",
            notes="",
        )

    @staticmethod
    def _clean_text(value: str) -> str:
        # Strip HTML tags + collapse whitespace.
        text = re.sub(r"<[^>]+>", "", value or "")
        return re.sub(r"\s+", " ", text).strip()


def _safe_json(resp: httpx.Response, *, word: str, endpoint: str) -> Optional[dict]:
    """Decode ``resp.json()`` without crashing the provider on bad payloads.

    iCIBA occasionally serves an HTML interstitial (rate-limit page, captcha)
    that bypasses Content-Type and breaks ``resp.json()`` with a JSONDecodeError.
    Surfacing that as an exception leaves the asyncio semaphore acquired and
    blocks subsequent words; converting to ``None`` keeps the pipeline moving.
    """
    try:
        return resp.json()
    except ValueError as exc:
        logger.debug(
            "iciba %s endpoint returned non-JSON for '%s' (%d bytes): %s",
            endpoint, word, len(resp.content or b""), exc,
        )
        return None


def _normalize_pos(raw: str) -> str:
    raw = (raw or "").strip().rstrip(".").lower()
    table = {
        "n": "noun", "v": "verb", "vt": "verb", "vi": "verb",
        "adj": "adj", "a": "adj", "ad": "adv", "adv": "adv",
        "prep": "other", "conj": "other", "pron": "other",
        "num": "other", "int": "other",
    }
    return table.get(raw, raw or "other")
