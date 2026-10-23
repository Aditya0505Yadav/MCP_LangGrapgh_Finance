"""Tool contracts: validated input + structured output schemas, and the status
model that distinguishes valid data, valid-empty data, and tool errors.

Three outcome categories are preserved end to end:

* **Valid data** — ``status == "ok"`` (nullable fields such as missing debt stay
  ``null`` with an explanation; missing debt is NOT zero debt).
* **Valid empty data** — ``status == "no_data"`` (a legitimate "nothing to
  report" response; becomes a branch-failure status at validation, not a retry).
* **Tool error** — ``status`` is an error code; ``retryable`` says whether a
  transient class (``rate_limited`` / ``provider_timeout`` /
  ``provider_unavailable``) should be retried. Invalid arguments
  (``invalid_symbol`` / ``invalid_date_range`` / ``unsupported_period``) are
  never retried.

A fourth category — an MCP/transport or unexpected server failure — is NOT a
status here; it surfaces as an exception/``is_error`` and stops the run visibly.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

STATUS_OK = "ok"
STATUS_NO_DATA = "no_data"

TRANSIENT_CODES = {"rate_limited", "provider_timeout", "provider_unavailable"}
NON_TRANSIENT_CODES = {"invalid_symbol", "invalid_date_range", "unsupported_period"}
ALL_ERROR_CODES = TRANSIENT_CODES | NON_TRANSIENT_CODES

MAX_RANGE_DAYS = 1830  # ~5 years — bounds the period/response size.
MAX_OBSERVATIONS = 2000


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ToolErrorInfo(BaseModel):
    code: str
    message: str
    retryable: bool


# --------------------------------------------------------------------------- #
# Tool 1 — get_stock_data
# --------------------------------------------------------------------------- #

class StockObservation(BaseModel):
    date: str  # YYYY-MM-DD
    close: float
    adjusted_close: float | None = None


class StockDataResult(BaseModel):
    status: str = STATUS_OK
    error: ToolErrorInfo | None = None
    symbol: str
    currency: str | None = None
    adjustment_basis: Literal["close", "adjusted_close"] | None = None
    observations: list[StockObservation] = Field(default_factory=list)
    source_references: list[str] = Field(default_factory=list)
    retrieved_at: str = Field(default_factory=utcnow_iso)
    data_as_of: str | None = None
    data_mode: str = "fixture"
    # Optional real-time snapshot fields (populated in live mode; null for fixtures).
    current_price: float | None = None
    previous_close: float | None = None
    fifty_two_week_high: float | None = None
    fifty_two_week_low: float | None = None
    market_time: str | None = None
    notes: str | None = None


def stock_error(symbol: str, code: str, message: str, data_mode: str) -> StockDataResult:
    return StockDataResult(
        status=code,
        error=ToolErrorInfo(code=code, message=message, retryable=code in TRANSIENT_CODES),
        symbol=symbol,
        data_mode=data_mode,
    )


def validate_stock_input(symbol: str, start_date: str, end_date: str) -> str | None:
    """Return an error code if the input is invalid, else None."""
    if not symbol or not symbol.strip():
        return "invalid_symbol"
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError:
        return "invalid_date_range"
    if start > end:
        return "invalid_date_range"
    if (end - start).days > MAX_RANGE_DAYS:
        return "invalid_date_range"
    return None


# --------------------------------------------------------------------------- #
# Tool 2 — get_company_financials
# --------------------------------------------------------------------------- #

class FinancialsResult(BaseModel):
    status: str = STATUS_OK
    error: ToolErrorInfo | None = None
    symbol: str
    company_name: str | None = None
    period_type: Literal["annual", "quarterly"] | None = None
    reporting_period: str | None = None
    fiscal_year: int | None = None
    period_start: str | None = None
    period_end: str | None = None
    revenue: float | None = None
    net_income: float | None = None
    total_debt: float | None = None  # null means "not available", NOT zero debt
    units: str | None = None         # e.g. "USD", values are absolute
    currency: str | None = None
    market_cap: float | None = None  # optional real-time quote stats (live mode)
    pe_ratio: float | None = None
    filing_date: str | None = None
    sources: list[str] = Field(default_factory=list)
    retrieved_at: str = Field(default_factory=utcnow_iso)
    data_mode: str = "fixture"
    notes: str | None = None


def financials_error(symbol: str, code: str, message: str, data_mode: str) -> FinancialsResult:
    return FinancialsResult(
        status=code,
        error=ToolErrorInfo(code=code, message=message, retryable=code in TRANSIENT_CODES),
        symbol=symbol,
        data_mode=data_mode,
    )


class _PeriodInput(BaseModel):
    symbol: str
    reporting_period: str
    period_type: Literal["annual", "quarterly"] = "annual"

    @field_validator("symbol")
    @classmethod
    def _symbol_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("symbol must be non-empty")
        return v.strip().upper()
