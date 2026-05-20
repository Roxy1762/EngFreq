"""
In-process provider observability — latency, success/failure counts, token
spend per provider.

Counters live in module-level memory and are reset on process restart. This
matches the deployment shape of the rest of the app (single-process by
default) and avoids pulling in Prometheus or any other dependency.

Reads are lock-free, writes hold a tiny RLock. The data is intended for an
admin diagnostics endpoint — not for high-throughput dashboards — so
serialisation overhead is negligible compared to the API calls being
measured.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

_lock = threading.RLock()


# ── Per-provider counters ────────────────────────────────────────────────────

@dataclass
class _ProviderCounters:
    name: str
    total_calls: int = 0
    success_count: int = 0
    failure_count: int = 0
    total_latency_ms: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    last_latency_ms: int = 0
    last_error: Optional[str] = None
    last_error_at: Optional[float] = None
    last_success_at: Optional[float] = None
    # Rolling window of recent latencies (capped) for percentile estimation
    recent_latencies: Deque[int] = field(default_factory=lambda: deque(maxlen=128))


_providers: Dict[str, _ProviderCounters] = {}


def _get(name: str) -> _ProviderCounters:
    pc = _providers.get(name)
    if pc is None:
        pc = _ProviderCounters(name=name)
        _providers[name] = pc
    return pc


# ── Public recording API ─────────────────────────────────────────────────────

def record_provider_call(
    provider: str,
    *,
    ok: bool,
    latency_ms: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
    error: Optional[str] = None,
) -> None:
    """Record a single provider call's outcome.

    Called by the LLM client and HTTP dictionary base after every request.
    Cheap enough to be on every hot path — no I/O, no allocation beyond the
    deque append.
    """
    if not provider:
        return
    now = time.time()
    with _lock:
        pc = _get(provider.strip().lower())
        pc.total_calls += 1
        pc.total_latency_ms += max(0, int(latency_ms))
        pc.last_latency_ms = max(0, int(latency_ms))
        pc.recent_latencies.append(pc.last_latency_ms)
        if input_tokens:
            pc.total_input_tokens += int(input_tokens)
        if output_tokens:
            pc.total_output_tokens += int(output_tokens)
        if ok:
            pc.success_count += 1
            pc.last_success_at = now
        else:
            pc.failure_count += 1
            pc.last_error = (error or "").strip() or "unknown error"
            pc.last_error_at = now


def reset_provider(name: Optional[str] = None) -> int:
    """Reset counters for one provider or all. Returns the number reset."""
    with _lock:
        if name is None:
            count = len(_providers)
            _providers.clear()
            return count
        key = name.strip().lower()
        if key in _providers:
            del _providers[key]
            return 1
        return 0


# ── Snapshot for the admin endpoint ──────────────────────────────────────────

def _percentile(values: List[int], pct: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[k]


def snapshot() -> Dict[str, Any]:
    """Return a JSON-friendly view of every provider's metrics."""
    with _lock:
        items: List[Dict[str, Any]] = []
        totals = defaultdict(int)
        for pc in sorted(_providers.values(), key=lambda p: p.name):
            recent = list(pc.recent_latencies)
            avg_ms = int(pc.total_latency_ms / pc.total_calls) if pc.total_calls else 0
            success_rate = (
                round(100.0 * pc.success_count / pc.total_calls, 1)
                if pc.total_calls else 0.0
            )
            items.append({
                "provider": pc.name,
                "total_calls": pc.total_calls,
                "success_count": pc.success_count,
                "failure_count": pc.failure_count,
                "success_rate_pct": success_rate,
                "avg_latency_ms": avg_ms,
                "p50_latency_ms": _percentile(recent, 50),
                "p95_latency_ms": _percentile(recent, 95),
                "p99_latency_ms": _percentile(recent, 99),
                "last_latency_ms": pc.last_latency_ms,
                "total_input_tokens": pc.total_input_tokens,
                "total_output_tokens": pc.total_output_tokens,
                "last_error": pc.last_error,
                "last_error_at": pc.last_error_at,
                "last_success_at": pc.last_success_at,
            })
            totals["calls"] += pc.total_calls
            totals["success"] += pc.success_count
            totals["failures"] += pc.failure_count
            totals["input_tokens"] += pc.total_input_tokens
            totals["output_tokens"] += pc.total_output_tokens

        return {
            "providers": items,
            "totals": {
                "calls": totals["calls"],
                "success": totals["success"],
                "failures": totals["failures"],
                "success_rate_pct": (
                    round(100.0 * totals["success"] / totals["calls"], 1)
                    if totals["calls"] else 0.0
                ),
                "input_tokens": totals["input_tokens"],
                "output_tokens": totals["output_tokens"],
            },
        }
