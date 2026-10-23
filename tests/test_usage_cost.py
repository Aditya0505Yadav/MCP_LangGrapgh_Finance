"""Token normalization and versioned cost estimation."""

from decimal import Decimal
from types import SimpleNamespace

from app.observability.usage import estimate_cost, normalize_usage

PRICING = {
    "pricing_id": "p1",
    "rates_per_million": {
        "input": 4.0, "output": 20.0, "cache_read": 0.2,
        "cache_write_5m": 5.0, "cache_write_1h": 8.0,
    },
}


def test_normalize_basic():
    usage = SimpleNamespace(
        input_tokens=1000, output_tokens=500,
        cache_read_input_tokens=0, cache_creation_input_tokens=0, cache_creation=None,
    )
    n = normalize_usage(usage)
    assert n["input_tokens"] == 1000
    assert n["output_tokens"] == 500
    assert n["usage_status"] == "recorded"


def test_normalize_cache_split():
    cc = SimpleNamespace(ephemeral_5m_input_tokens=200, ephemeral_1h_input_tokens=50)
    usage = SimpleNamespace(
        input_tokens=100, output_tokens=10, cache_read_input_tokens=20,
        cache_creation_input_tokens=250, cache_creation=cc,
    )
    n = normalize_usage(usage)
    assert n["cache_read_tokens"] == 20
    assert n["cache_write_tokens_short_ttl"] == 200
    assert n["cache_write_tokens_long_ttl"] == 50


def test_normalize_unknown_when_missing():
    usage = SimpleNamespace(input_tokens=None, output_tokens=None,
                            cache_read_input_tokens=None,
                            cache_creation_input_tokens=None, cache_creation=None)
    assert normalize_usage(usage)["usage_status"] == "unknown"


def test_cost_sum_no_double_count():
    # Disjoint categories summed once each.
    n = {"input_tokens": 1000, "output_tokens": 500, "cache_read_tokens": 0,
         "cache_write_tokens_short_ttl": 0, "cache_write_tokens_long_ttl": 0,
         "usage_status": "recorded"}
    cost = estimate_cost(n, PRICING)
    # (1000*4 + 500*20)/1e6 = 0.014
    assert cost == Decimal("0.01400000")


def test_cost_includes_cache_categories():
    n = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 1_000_000,
         "cache_write_tokens_short_ttl": 0, "cache_write_tokens_long_ttl": 0,
         "usage_status": "recorded"}
    assert estimate_cost(n, PRICING) == Decimal("0.20000000")


def test_cost_unknown_without_pricing():
    n = {"input_tokens": 10, "output_tokens": 10, "cache_read_tokens": 0,
         "cache_write_tokens_short_ttl": 0, "cache_write_tokens_long_ttl": 0,
         "usage_status": "recorded"}
    assert estimate_cost(n, None) is None


def test_cost_unknown_when_usage_unknown():
    n = {"input_tokens": None, "output_tokens": None, "cache_read_tokens": 0,
         "cache_write_tokens_short_ttl": 0, "cache_write_tokens_long_ttl": 0,
         "usage_status": "unknown"}
    assert estimate_cost(n, PRICING) is None


def test_cost_none_when_rate_missing_for_present_category():
    pricing = {"pricing_id": "p", "rates_per_million": {"input": 4.0, "output": 20.0}}
    n = {"input_tokens": 10, "output_tokens": 10, "cache_read_tokens": 100,
         "cache_write_tokens_short_ttl": 0, "cache_write_tokens_long_ttl": 0,
         "usage_status": "recorded"}
    # cache_read has tokens but no rate -> unknown, not silently dropped
    assert estimate_cost(n, pricing) is None
