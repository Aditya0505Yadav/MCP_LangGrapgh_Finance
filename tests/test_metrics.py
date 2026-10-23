"""Deterministic metric functions."""

from app.metrics import compute_financial_metrics, compute_stock_metrics


def test_stock_metrics_basic():
    data = {
        "observations": [
            {"date": "2023-01-03", "close": 100.0, "adjusted_close": 100.0},
            {"date": "2023-02-01", "close": 110.0, "adjusted_close": 110.0},
            {"date": "2023-03-01", "close": 90.0, "adjusted_close": 90.0},
        ],
        "data_as_of": "2023-03-01", "currency": "USD", "data_mode": "fixture",
    }
    m = compute_stock_metrics(data)
    assert m["observations"] == 3
    assert m["basis"] == "adjusted_close"
    assert m["start_price"] == 100.0
    assert m["end_price"] == 90.0
    assert m["period_return_pct"] == -10.0
    assert m["max_price"] == 110.0 and m["max_date"] == "2023-02-01"
    assert m["min_price"] == 90.0 and m["min_date"] == "2023-03-01"
    assert m["observation_to_observation_volatility_pct"] is not None


def test_stock_metrics_falls_back_to_close_when_adjusted_missing():
    data = {"observations": [
        {"date": "2023-01-03", "close": 100.0},
        {"date": "2023-02-01", "close": 120.0, "adjusted_close": 119.0},
    ]}
    m = compute_stock_metrics(data)
    assert m["basis"] == "close"  # not all obs have adjusted_close


def test_stock_metrics_empty():
    assert compute_stock_metrics({"observations": []})["observations"] == 0


def test_financial_metrics_with_debt():
    data = {"revenue": 1000.0, "net_income": 200.0, "total_debt": 500.0,
            "units": "USD", "fiscal_year": 2023}
    m = compute_financial_metrics(data)
    assert m["net_margin_pct"] == 20.0
    assert m["debt_reported"] is True
    assert m["debt_to_revenue"] == 0.5


def test_financial_metrics_missing_debt_is_not_zero():
    data = {"revenue": 1000.0, "net_income": 100.0, "total_debt": None}
    m = compute_financial_metrics(data)
    assert m["total_debt"] is None
    assert m["debt_reported"] is False
    assert m["debt_to_revenue"] is None  # not computed as 0
