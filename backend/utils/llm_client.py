"""
Unified async LLM client — single entrypoint for Claude / DeepSeek / OpenAI.

Goals:
- One interface for chat completions across providers, so services (vocab
  generation, text cleaning, AI rerank) don't duplicate SDK boilerplate.
- Built-in retry with exponential backoff (see `utils.retry`).
- Optional Anthropic prompt caching — long system prompts are cache hints,
  so repeated calls during one analysis session reuse cached tokens.
- Observability: logs token usage (when exposed by SDK) and latency.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.utils.model_registry import get_profile
from backend.utils.retry import RetryPolicy, call_with_retry

logger = logging.getLogger(__name__)


# ── Response envelope ─────────────────────────────────────────────────────────

@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    latency_ms: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    finish_reason: Optional[str] = None
    raw: Any = field(default=None, repr=False)

    @property
    def usage_summary(self) -> str:
        parts = []
        if self.input_tokens is not None:
            parts.append(f"in={self.input_tokens}")
        if self.output_tokens is not None:
            parts.append(f"out={self.output_tokens}")
        if self.cache_read_tokens:
            parts.append(f"cache_r={self.cache_read_tokens}")
        if self.cache_write_tokens:
            parts.append(f"cache_w={self.cache_write_tokens}")
        parts.append(f"{self.latency_ms}ms")
        return " ".join(parts)


# ── Public calls ──────────────────────────────────────────────────────────────

_DEFAULT_POLICY = RetryPolicy(max_attempts=3, initial_delay=1.5, max_delay=20.0)


async def chat(
    *,
    provider: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 4096,
    temperature: float = 0.3,
    use_prompt_cache: bool = False,
    json_mode: bool = False,
    retry_policy: Optional[RetryPolicy] = None,
    label: Optional[str] = None,
) -> LLMResponse:
    """
    Single turn chat completion.

    provider: 'claude' | 'deepseek' | 'openai'
    use_prompt_cache: for Anthropic — marks the system message as cacheable.
                     No-op for other providers (safely ignored).
    json_mode: for OpenAI-compatible providers — requests JSON response format.
              Ignored for providers that don't support it.
    """
    provider = provider.strip().lower()
    policy = retry_policy or _DEFAULT_POLICY
    log_label = label or f"{provider}:{model}"

    if provider == "claude":
        fn = _call_claude
    elif provider == "deepseek":
        fn = _call_openai_compatible  # DeepSeek is OpenAI-compatible
    elif provider == "openai":
        fn = _call_openai_compatible
    else:
        raise ValueError(f"Unknown LLM provider: {provider!r}")

    t0 = time.monotonic()
    response = await call_with_retry(
        fn,
        model=model,
        system=system,
        user=user,
        max_tokens=max_tokens,
        temperature=temperature,
        use_prompt_cache=use_prompt_cache,
        json_mode=json_mode,
        provider=provider,
        policy=policy,
        label=log_label,
    )
    response.latency_ms = int((time.monotonic() - t0) * 1000)
    logger.info("%s ok (%s)", log_label, response.usage_summary)
    return response


# ── Anthropic Claude ──────────────────────────────────────────────────────────

async def _call_claude(
    *, model: str, system: str, user: str,
    max_tokens: int, temperature: float,
    use_prompt_cache: bool, json_mode: bool, provider: str,
) -> LLMResponse:
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError("anthropic package not installed: pip install anthropic") from exc

    from backend.services.runtime_config import get_runtime_config
    llm = get_runtime_config().llm
    if not llm.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    client = anthropic.AsyncAnthropic(api_key=llm.anthropic_api_key)

    # Build system param: plain string or cacheable content block
    profile = get_profile(model)
    system_param: Any = system
    if use_prompt_cache and profile.supports_prompt_cache and len(system) > 500:
        system_param = [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]

    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_param,
        messages=[{"role": "user", "content": user}],
    )

    text = resp.content[0].text if resp.content else ""
    usage = getattr(resp, "usage", None)
    return LLMResponse(
        text=text.strip(),
        model=model,
        provider=provider,
        input_tokens=getattr(usage, "input_tokens", None) if usage else None,
        output_tokens=getattr(usage, "output_tokens", None) if usage else None,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", None) if usage else None,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None) if usage else None,
        finish_reason=getattr(resp, "stop_reason", None),
        raw=resp,
    )


# ── OpenAI-compatible (OpenAI, DeepSeek, Ollama, LM Studio, …) ────────────────

async def _call_openai_compatible(
    *, model: str, system: str, user: str,
    max_tokens: int, temperature: float,
    use_prompt_cache: bool, json_mode: bool, provider: str,
) -> LLMResponse:
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise RuntimeError("openai package not installed: pip install openai") from exc

    from backend.services.runtime_config import get_runtime_config
    llm = get_runtime_config().llm

    if provider == "deepseek":
        api_key = llm.deepseek_api_key
        base_url = llm.deepseek_base_url
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY not configured")
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    else:
        api_key = llm.openai_api_key
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not configured")
        kwargs: dict = {"api_key": api_key}
        if llm.openai_base_url:
            kwargs["base_url"] = llm.openai_base_url
        client = AsyncOpenAI(**kwargs)

    call_kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode and get_profile(model).supports_json_mode:
        call_kwargs["response_format"] = {"type": "json_object"}

    resp = await client.chat.completions.create(**call_kwargs)

    choice = resp.choices[0] if resp.choices else None
    text = choice.message.content if choice and choice.message else ""
    usage = getattr(resp, "usage", None)
    return LLMResponse(
        text=(text or "").strip(),
        model=model,
        provider=provider,
        input_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
        output_tokens=getattr(usage, "completion_tokens", None) if usage else None,
        finish_reason=getattr(choice, "finish_reason", None) if choice else None,
        raw=resp,
    )


# ── Resolver: figure out which provider/model to use ──────────────────────────

def resolve_active_llm(provider_name: Optional[str] = None) -> tuple[str, str]:
    """
    Resolve (provider, model) from runtime config.

    If provider_name is None, reads `vocab_provider` from runtime config.
    Returns (provider, model) ready to pass to `chat()`.

    Raises ValueError if the chosen provider is not an LLM (e.g. free_dict).
    """
    from backend.services.runtime_config import get_runtime_config

    runtime = get_runtime_config()
    pname = (provider_name or runtime.vocab_provider or "").strip().lower()

    if pname == "claude":
        return "claude", runtime.ai_model
    if pname == "deepseek":
        return "deepseek", runtime.llm.deepseek_model
    if pname == "openai":
        return "openai", runtime.llm.openai_model

    raise ValueError(f"Provider '{pname}' is not an LLM provider")


def is_llm_provider(name: Optional[str]) -> bool:
    return (name or "").strip().lower() in {"claude", "deepseek", "openai"}
