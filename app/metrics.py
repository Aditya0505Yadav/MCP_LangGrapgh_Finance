"""Deterministic numerical metrics.

These are computed in plain Python from retrieved data and handed to the analyst
agents as evidence. The LLM interprets the evidence; it never invents or mentally
estimates the numbers. Historical series are clearly distinguished from a current
quotation (``data_as_of``).
"""

from __future__ import annotations

import statistics
from typing import Any


def compute_stock_metrics(stock_data: dict[str, Any]) -> dict[str, Any]:
    """Metrics over a price series, using a single consistent close series.

    Uses ``adjusted_close`` when available for every observation, otherwise
    ``close`` — and records which basis was used, so the series is consistent.
    """
    observations = stock_data.get("observations") or []
    if not observations:
        return {"observations": 0, "note": "no observations"}

    use_adjusted = all(o.get("adjusted_close") is not None for o in observations)
    basis = "adjusted_close" if use_adjusted else "close"
    series = [
        (o["date"], float(o["adjusted_close"] if use_adjusted else o["close"]))
        for o in observations
    ]
    dates = [d for d, _ in series]
    prices = [p for _, p in series]

    start_price, end_price = prices[0], prices[-1]
    period_return_pct = (
        (end_price - start_price) / start_price * 100.0 if start_price else None
    )

    daily_returns = [
        (prices[i] - prices[i - 1]) / prices[i - 1]
        for i in range(1, len(prices))
        if prices[i - 1]
    ]
    volatility_pct = (
        statistics.pstdev(daily_returns) * 100.0 if len(daily_returns) >= 1 else None
    )

    max_price = max(prices)
    min_price = min(prices)
    return {
        "basis": basis,
        "observations": len(prices),
        "first_date": dates[0],
        "last_date": dates[-1],
        "start_price": round(start_price, 4),
        "end_price": round(end_price, 4),
        "period_return_pct": round(period_return_pct, 4) if period_return_pct is not None else None,
        "max_price": round(max_price, 4),
        "max_date": dates[prices.index(max_price)],
        "min_price": round(min_price, 4),
        "min_date": dates[prices.index(min_price)],
        "observation_to_observation_volatility_pct": (
            round(volatility_pct, 4) if volatility_pct is not None else None
        ),
        "data_as_of": stock_data.get("data_as_of"),
        "currency": stock_data.get("currency"),
        "data_mode": stock_data.get("data_mode"),
    }


def compute_financial_metrics(financial_data: dict[str, Any]) -> dict[str, Any]:
    """Margins and leverage ratios. Missing debt stays missing (not zero)."""
    revenue = financial_data.get("revenue")
    net_income = financial_data.get("net_income")
    total_debt = financial_data.get("total_debt")

    net_margin_pct = (
        net_income / revenue * 100.0
        if revenue not in (None, 0) and net_income is not None
        else None
    )
    debt_to_revenue = (
        total_debt / revenue
        if revenue not in (None, 0) and total_debt is not None
        else None
    )
    return {
        "revenue": revenue,
        "net_income": net_income,
        "total_debt": total_debt,
        "debt_reported": total_debt is not None,
        "net_margin_pct": round(net_margin_pct, 4) if net_margin_pct is not None else None,
        "debt_to_revenue": round(debt_to_revenue, 4) if debt_to_revenue is not None else None,
        "units": financial_data.get("units"),
        "currency": financial_data.get("currency"),
        "fiscal_year": financial_data.get("fiscal_year"),
        "period_start": financial_data.get("period_start"),
        "period_end": financial_data.get("period_end"),
        "period_type": financial_data.get("period_type"),
        "data_mode": financial_data.get("data_mode"),
    }
