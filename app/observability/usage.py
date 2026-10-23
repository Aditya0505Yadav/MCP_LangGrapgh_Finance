"""Token-usage normalization and versioned USD cost estimation.

Uses the *actual* usage metadata returned by Anthropic, retaining the raw
representation. Anthropic's token categories are disjoint — ``input_tokens``
(uncached input), ``cache_read_input_tokens``, ``cache_creation_input_tokens``
(cache writes, split into 5-minute / 1-hour TTL under ``cache_creation``), and
``output_tokens`` — so summing them does not double-count cached tokens.

Cost is estimated from a versioned pricing row read from ``app.pricing`` (never a
hard-coded rate). An unknown rate or missing usage yields ``None`` (unknown),
never zero. Each actual call is recorded once (1:1 with its llm span).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.db.repositories import Repositories


def _get(obj: Any, name: str) -> Any:
    return getattr(obj, name, None)


def normalize_usage(usage: Any) -> dict[str, Any]:
    """Normalize an Anthropic ``usage`` object into disjoint token categories."""
    input_tokens = _get(usage, "input_tokens")
    output_tokens = _get(usage, "output_tokens")
    cache_read = _get(usage, "cache_read_input_tokens")
    cache_creation_total = _get(usage, "cache_creation_input_tokens")

    short_ttl: int | None = None
    long_ttl: int | None = None
    cc = _get(usage, "cache_creation")
    if cc is not None:
        short_ttl = _get(cc, "ephemeral_5m_input_tokens")
        long_ttl = _get(cc, "ephemeral_1h_input_tokens")
    # Fall back to the aggregate cache-creation count (older shape) — attribute
    # it to the 5-minute TTL (the default) when the split is unavailable.
    if short_ttl is None and long_ttl is None and cache_creation_total:
        short_ttl = cache_creation_total
        long_ttl = 0

    if input_tokens is not None and output_tokens is not None:
        status = "recorded"
    elif input_tokens is not None or output_tokens is not None:
        status = "partial"
    else:
        status = "unknown"

    if hasattr(usage, "model_dump"):
        try:
            raw = usage.model_dump(mode="json")
        except Exception:
            raw = {"repr": str(usage)}
    elif isinstance(usage, dict):
        raw = usage
    else:
        raw = {"repr": str(usage)}

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read or 0,
        "cache_write_tokens_short_ttl": short_ttl or 0,
        "cache_write_tokens_long_ttl": long_ttl or 0,
        "usage_status": status,
        "usage_raw": raw,
    }


def estimate_cost(normalized: dict[str, Any], pricing_row: dict | None) -> Decimal | None:
    """Return the estimated USD cost, or ``None`` if it cannot be known.

    None is returned when there is no pricing row, when usage is not recorded,
    or when a *non-zero* token category has no matching rate.
    """
    if pricing_row is None:
        return None
    if normalized["usage_status"] == "unknown":
        return None

    rates = pricing_row.get("rates_per_million") or {}

    def rate(key: str) -> Decimal | None:
        v = rates.get(key)
        return Decimal(str(v)) if v is not None else None

    components = [
        (normalized["input_tokens"], rate("input")),
        (normalized["output_tokens"], rate("output")),
        (normalized["cache_read_tokens"], rate("cache_read")),
        (normalized["cache_write_tokens_short_ttl"], rate("cache_write_5m")),
        (normalized["cache_write_tokens_long_ttl"], rate("cache_write_1h")),
    ]
    total = Decimal(0)
    for tokens, r in components:
        if not tokens:
            continue
        if r is None:
            # A billable category with no known rate -> cost is unknown.
            return None
        total += (Decimal(int(tokens)) * r) / Decimal(1_000_000)
    return total.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


class UsageRecorder:
    """Persists one LLM call's usage + estimated cost (1:1 with its span)."""

    def __init__(self, repos: Repositories) -> None:
        self._repos = repos

    async def record(
        self,
        *,
        llm_call_id: str,
        span_id: str,
        provider: str,
        requested_model: str,
        returned_model: str | None,
        prompt_version: str,
        prompt_hash: str,
        generation_parameters: dict,
        usage: Any,
        stop_reason: str | None,
        provider_request_id: str | None,
        ttft_ms: int | None = None,
        call_time: datetime | None = None,
    ) -> dict[str, Any]:
        call_time = call_time or datetime.now(timezone.utc)
        normalized = normalize_usage(usage)
        model_for_pricing = returned_model or requested_model
        pricing = await self._repos.get_active_pricing(provider, model_for_pricing, call_time)
        cost = estimate_cost(normalized, pricing)

        await self._repos.insert_llm_call(
            {
                "llm_call_id": llm_call_id,
                "span_id": span_id,
                "created_at": call_time,
                "provider": provider,
                "requested_model": requested_model,
                "returned_model": returned_model,
                "prompt_version": prompt_version,
                "prompt_hash": prompt_hash,
                "generation_parameters": generation_parameters,
                "usage_raw": normalized["usage_raw"],
                "input_tokens": normalized["input_tokens"],
                "output_tokens": normalized["output_tokens"],
                "cache_read_tokens": normalized["cache_read_tokens"],
                "cache_write_tokens_short_ttl": normalized["cache_write_tokens_short_ttl"],
                "cache_write_tokens_long_ttl": normalized["cache_write_tokens_long_ttl"],
                "usage_status": normalized["usage_status"],
                "estimated_cost_usd": cost,
                "pricing_id": pricing["pricing_id"] if pricing else None,
                "stop_reason": stop_reason,
                "provider_request_id": provider_request_id,
                "ttft_ms": ttft_ms,
            }
        )
        return {"normalized": normalized, "estimated_cost_usd": cost,
                "pricing_id": pricing["pricing_id"] if pricing else None}
