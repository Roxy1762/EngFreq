"""
Model registry — capabilities, batch sizes, and fallback chains for LLM models.

The registry captures what we know about each model (context window, preferred
batch size, whether prompt caching is worth doing, a fallback hop). Callers
should look up a model by name and fall back to `DEFAULT` if unknown — we
don't refuse to run on unrecognized names.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider: str                     # claude | deepseek | openai
    context_window: int = 200_000
    max_output_tokens: int = 4096
    batch_size: int = 20              # words per enrichment batch
    supports_prompt_cache: bool = False
    supports_json_mode: bool = False  # OpenAI-compatible `response_format`
    tier: str = "standard"            # lite | standard | premium
    fallback: Optional[str] = None    # name of next model to try on failure
    notes: str = ""


# ── Known models (keep list conservative; unknown names fall back to DEFAULT) ──

_MODELS: dict[str, ModelProfile] = {
    # Anthropic Claude (4.x family — Opus 4.7 is current flagship)
    "claude-opus-4-7": ModelProfile(
        name="claude-opus-4-7", provider="claude",
        context_window=200_000, max_output_tokens=16_384,
        batch_size=30, supports_prompt_cache=True,
        tier="premium", fallback="claude-sonnet-4-6",
        notes="Latest Anthropic flagship, best reasoning.",
    ),
    "claude-opus-4-6": ModelProfile(
        name="claude-opus-4-6", provider="claude",
        context_window=200_000, max_output_tokens=8_192,
        batch_size=25, supports_prompt_cache=True,
        tier="premium", fallback="claude-sonnet-4-6",
    ),
    "claude-sonnet-4-6": ModelProfile(
        name="claude-sonnet-4-6", provider="claude",
        context_window=200_000, max_output_tokens=8_192,
        batch_size=30, supports_prompt_cache=True,
        tier="standard", fallback="claude-haiku-4-5",
    ),
    "claude-haiku-4-5": ModelProfile(
        name="claude-haiku-4-5", provider="claude",
        context_window=200_000, max_output_tokens=4_096,
        batch_size=40, supports_prompt_cache=True,
        tier="lite", fallback=None,
    ),

    # DeepSeek
    "deepseek-chat": ModelProfile(
        name="deepseek-chat", provider="deepseek",
        context_window=64_000, max_output_tokens=8_192,
        batch_size=30, supports_json_mode=True,
        tier="standard",
    ),
    "deepseek-reasoner": ModelProfile(
        name="deepseek-reasoner", provider="deepseek",
        context_window=64_000, max_output_tokens=8_192,
        batch_size=20, tier="premium", supports_json_mode=True,
    ),

    # OpenAI-compatible
    "gpt-4o": ModelProfile(
        name="gpt-4o", provider="openai",
        context_window=128_000, max_output_tokens=16_384,
        batch_size=30, supports_json_mode=True, tier="premium",
        fallback="gpt-4o-mini",
    ),
    "gpt-4o-mini": ModelProfile(
        name="gpt-4o-mini", provider="openai",
        context_window=128_000, max_output_tokens=16_384,
        batch_size=30, supports_json_mode=True, tier="standard",
    ),
    "gpt-4.1-mini": ModelProfile(
        name="gpt-4.1-mini", provider="openai",
        context_window=1_000_000, max_output_tokens=32_768,
        batch_size=40, supports_json_mode=True, tier="standard",
    ),
}


DEFAULT = ModelProfile(
    name="unknown", provider="unknown",
    context_window=32_000, max_output_tokens=4_096,
    batch_size=20, supports_prompt_cache=False, tier="standard",
)


def get_profile(name: Optional[str]) -> ModelProfile:
    """Look up a model profile by name. Unknown names return DEFAULT."""
    if not name:
        return DEFAULT
    return _MODELS.get(name.strip(), DEFAULT)


def list_profiles(provider: Optional[str] = None) -> list[ModelProfile]:
    profiles = list(_MODELS.values())
    if provider:
        profiles = [p for p in profiles if p.provider == provider]
    return sorted(profiles, key=lambda p: (p.provider, p.tier, p.name))


def recommended_batch_size(model: Optional[str], requested: int) -> int:
    """Clamp the user-requested batch size to the model's safe range (1-50)."""
    profile = get_profile(model)
    ceiling = max(5, min(50, profile.batch_size))
    return max(1, min(requested, ceiling))
