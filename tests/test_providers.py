"""Fixture provider behavior and fault injection (no network)."""

import pytest

from app.config import Settings
from mcp_server.providers import ProviderAdapter, _fault_counters


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="t", data_mode="fixture")


async def test_stock_ok(settings):
    p = ProviderAdapter(settings)
    r = await p.get_stock_data("AAPL", "2023-01-01", "2023-12-31")
    assert r.status == "ok"
    assert len(r.observations) > 0
    assert r.data_mode == "fixture"
    assert r.adjustment_basis in ("close", "adjusted_close")


async def test_invalid_symbol(settings):
    p = ProviderAdapter(settings)
    r = await p.get_stock_data("ZZZZ", "2023-01-01", "2023-12-31")
    assert r.status == "invalid_symbol"
    assert r.error and r.error.retryable is False


async def test_no_data_out_of_range(settings):
    p = ProviderAdapter(settings)
    r = await p.get_stock_data("AAPL", "2019-01-01", "2019-02-01")
    assert r.status == "no_data"


async def test_invalid_date_range(settings):
    p = ProviderAdapter(settings)
    r = await p.get_stock_data("AAPL", "2023-12-31", "2023-01-01")
    assert r.status == "invalid_date_range"


async def test_financials_missing_debt_is_null(settings):
    p = ProviderAdapter(settings)
    r = await p.get_company_financials("MSFT", "FY2023", "annual")
    assert r.status == "ok"
    assert r.total_debt is None  # missing debt != zero debt
    assert r.revenue is not None


async def test_unsupported_period(settings):
    p = ProviderAdapter(settings)
    r = await p.get_company_financials("AAPL", "FY2023", "weekly")
    assert r.status == "unsupported_period"
    assert r.error.retryable is False


async def test_transient_fault_then_success():
    _fault_counters.clear()
    s = Settings(anthropic_api_key="t", data_mode="fixture",
                 mcp_fault_transient_symbol="AAPL", mcp_fault_transient_times=2)
    p = ProviderAdapter(s)
    r1 = await p.get_stock_data("AAPL", "2023-01-01", "2023-12-31")
    r2 = await p.get_stock_data("AAPL", "2023-01-01", "2023-12-31")
    r3 = await p.get_stock_data("AAPL", "2023-01-01", "2023-12-31")
    assert r1.status == "provider_timeout" and r1.error.retryable is True
    assert r2.status == "provider_timeout"
    assert r3.status == "ok"  # recovers after the configured number of faults
