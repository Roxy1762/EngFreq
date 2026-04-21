"""
Versioned prompt registry.

Prompts are first-class data, not code. Keeping them in their own module makes
it easy to:
  - A/B test variants without touching provider logic
  - Parametrize by domain (gaokao, IELTS, CET, TOEFL)
  - Track prompt versions for reproducibility

Use `get_prompt(name, domain=..., version=...)` to fetch a prompt string.
"""
from __future__ import annotations

from backend.prompts.rerank import RERANK_PROMPTS
from backend.prompts.text_cleaner import TEXT_CLEANER_PROMPTS
from backend.prompts.vocab import VOCAB_ENRICH_PROMPTS
from backend.prompts.vocab_select import VOCAB_SELECT_PROMPTS

_REGISTRY: dict[str, dict[str, dict[str, str]]] = {
    "vocab_enrich":   VOCAB_ENRICH_PROMPTS,
    "text_cleaner":   TEXT_CLEANER_PROMPTS,
    "rerank":         RERANK_PROMPTS,
    "vocab_select":   VOCAB_SELECT_PROMPTS,
}

DEFAULT_DOMAIN = "gaokao"
DEFAULT_VERSION = "v2"


def get_prompt(
    name: str,
    domain: str = DEFAULT_DOMAIN,
    version: str = DEFAULT_VERSION,
) -> str:
    """
    Fetch a prompt by (name, domain, version).

    Falls back to: (name, domain, 'v1') → (name, 'gaokao', 'v2') → (name, 'gaokao', 'v1').
    Raises KeyError if none match.
    """
    prompts = _REGISTRY.get(name)
    if prompts is None:
        raise KeyError(f"Unknown prompt family: {name!r}")

    for d, v in ((domain, version), (domain, "v1"), ("gaokao", "v2"), ("gaokao", "v1")):
        variant = prompts.get(d, {}).get(v)
        if variant:
            return variant.strip()

    raise KeyError(f"No prompt found: {name!r} (domain={domain!r}, version={version!r})")


def available_prompts() -> dict[str, dict[str, list[str]]]:
    """Introspection: list all prompt name → domain → [versions]."""
    return {
        name: {domain: sorted(v.keys()) for domain, v in domains.items()}
        for name, domains in _REGISTRY.items()
    }
