"""Lightweight security helpers — header middleware and input sanitising."""
from __future__ import annotations

import re
from typing import Callable, Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


# Defaults intentionally permit inline scripts/styles because the existing
# frontend ships inline handlers in its HTML. A stricter policy can be opted
# into by passing a custom ``csp`` value.
_DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds a modest set of hardening headers to every response.

    The defaults are deliberately conservative so the existing frontend keeps
    working while closing a few easy-to-exploit holes (clickjacking, MIME
    sniffing, open-ended referrer leaks).
    """

    def __init__(
        self,
        app,
        *,
        csp: str | None = _DEFAULT_CSP,
        enable_hsts: bool = False,
        extra: dict[str, str] | None = None,
    ) -> None:
        super().__init__(app)
        self._csp = csp
        self._enable_hsts = enable_hsts
        self._extra = dict(extra or {})

    async def dispatch(self, request: Request, call_next: Callable):
        response: Response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=(), payment=()",
        )
        if self._csp:
            headers.setdefault("Content-Security-Policy", self._csp)
        if self._enable_hsts:
            headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        for key, value in self._extra.items():
            headers[key] = value
        return response


# ── Upload helpers ───────────────────────────────────────────────────────────

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._\- ]+")


def sanitize_filename(raw: str | None, fallback: str = "upload") -> str:
    """Return a safe on-disk filename derived from a user-provided name.

    Strips path separators, control characters, and non-ASCII bytes while
    retaining the original extension (lower-cased). Always returns a
    non-empty string.
    """
    if not raw:
        return fallback
    # Drop any path components supplied by the client.
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not raw:
        return fallback
    stem, dot, ext = raw.rpartition(".")
    if not dot:
        stem, ext = raw, ""
    cleaned_stem = _SAFE_FILENAME.sub("_", stem)[:80] or fallback
    cleaned_ext = _SAFE_FILENAME.sub("", ext)[:16].lower()
    return f"{cleaned_stem}.{cleaned_ext}" if cleaned_ext else cleaned_stem


def client_identifier(request, trusted_proxies: Iterable[str] = ()) -> str:
    """Best-effort client identifier for rate limiting / logging.

    Prefers the direct peer address to avoid spoofing via X-Forwarded-For; if
    the server is configured to trust a proxy chain the caller should apply
    its own forwarded-header parsing before calling here.
    """
    client = getattr(request, "client", None)
    if client and client.host:
        return client.host
    return "unknown"
