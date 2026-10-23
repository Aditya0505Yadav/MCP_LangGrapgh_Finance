"""Provider adapters.

Both the deterministic *fixture* provider and the *live* provider return the same
output contract. Fixtures are loaded into memory at startup (no blocking I/O per
call); live calls use an async HTTP client (never a blocking library inside the
async path). The system NEVER silently falls back from live to fixture — a live
failure is reported as a labeled error.

Live data sources:
* Stocks — Stooq public daily CSV (no API key). Adjustment basis is raw close.
* Financials — no free provider is wired by default; live mode returns
  ``provider_unavailable`` with an explanation rather than fabricating figures.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from pathlib import Path

import httpx

from app.config import Settings
from mcp_server import contracts as c

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Per-process fault-injection counters (demo/tests only).
_fault_counters: dict[tuple[str, str], int] = {}


def _should_inject_transient(settings: Settings, tool: str, symbol: str) -> bool:
    target = settings.mcp_fault_transient_symbol
    if not target or settings.mcp_fault_transient_times <= 0:
        return False
    if symbol.upper() != target.upper():
        return False
    key = (tool, symbol.upper())
    seen = _fault_counters.get(key, 0)
    if seen < settings.mcp_fault_transient_times:
        _fault_counters[key] = seen + 1
        return True
    return False


class ProviderAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stock_fixtures: dict[str, dict] = {}
        self._fin_fixtures: dict[str, dict] = {}
        self._load_fixtures()

    def _load_fixtures(self) -> None:
        for path in FIXTURES_DIR.glob("stock_*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            self._stock_fixtures[data["symbol"].upper()] = data
        for path in FIXTURES_DIR.glob("financials_*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            self._fin_fixtures[data["symbol"].upper()] = data

    # ---------------------------------------------------------------- stocks
    async def get_stock_data(
        self, symbol: str, start_date: str, end_date: str
    ) -> c.StockDataResult:
        mode = self.settings.data_mode
        err = c.validate_stock_input(symbol, start_date, end_date)
        if err:
            return c.stock_error(symbol, err, f"invalid stock input: {err}", mode)

        sym = symbol.strip().upper()
        if _should_inject_transient(self.settings, "get_stock_data", sym):
            return c.stock_error(
                sym, "provider_timeout",
                "injected transient fault (demo): provider timed out", mode,
            )

        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        if mode == "fixture":
            return self._fixture_stock(sym, start, end)
        return await self._live_stock(sym, start, end)

    def _fixture_stock(self, sym: str, start: date, end: date) -> c.StockDataResult:
        fx = self._stock_fixtures.get(sym)
        if fx is None:
            return c.stock_error(
                sym, "invalid_symbol",
                f"no fixture for symbol {sym}; available: "
                f"{sorted(self._stock_fixtures)}",
                "fixture",
            )
        obs = [
            c.StockObservation(**o)
            for o in fx["observations"]
            if start <= date.fromisoformat(o["date"]) <= end
        ]
        if not obs:
            return c.StockDataResult(
                status=c.STATUS_NO_DATA, symbol=sym, currency=fx.get("currency"),
                adjustment_basis=fx.get("adjustment_basis"), observations=[],
                source_references=fx.get("source_references", []),
                data_as_of=fx.get("data_as_of"), data_mode="fixture",
                notes="No sample observations fall within the requested range.",
            )
        obs = obs[: c.MAX_OBSERVATIONS]
        return c.StockDataResult(
            status=c.STATUS_OK, symbol=sym, currency=fx.get("currency", "USD"),
            adjustment_basis=fx.get("adjustment_basis", "close"), observations=obs,
            source_references=fx.get("source_references", []),
            data_as_of=obs[-1].date, data_mode="fixture",
            notes="SAMPLE fixture data served through the real MCP transport — not live market data.",
        )

    async def _live_stock(self, sym: str, start: date, end: date) -> c.StockDataResult:
        url = f"https://stooq.com/q/d/l/?s={sym.lower()}.us&i=d"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
        except httpx.TimeoutException as exc:
            return c.stock_error(sym, "provider_timeout", f"stooq timeout: {exc}", "live")
        except httpx.HTTPError as exc:
            return c.stock_error(sym, "provider_unavailable", f"stooq error: {exc}", "live")
        if resp.status_code != 200:
            return c.stock_error(
                sym, "provider_unavailable",
                f"stooq HTTP {resp.status_code}", "live",
            )
        text = resp.text.strip()
        if not text or text.lower().startswith("no data") or "N/D" in text[:64]:
            return c.stock_error(sym, "invalid_symbol", "stooq returned no series", "live")

        observations: list[c.StockObservation] = []
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            raw_date = row.get("Date")
            raw_close = row.get("Close")
            if not raw_date or not raw_close:
                continue
            try:
                d = date.fromisoformat(raw_date)
                close = float(raw_close)
            except ValueError:
                continue
            if start <= d <= end:
                observations.append(c.StockObservation(date=raw_date, close=close))
        if not observations:
            return c.StockDataResult(
                status=c.STATUS_NO_DATA, symbol=sym, currency="USD",
                adjustment_basis="close", observations=[],
                source_references=[f"Stooq daily CSV: {url}"],
                data_mode="live", notes="No live observations in the requested range.",
            )
        observations = observations[: c.MAX_OBSERVATIONS]
        return c.StockDataResult(
            status=c.STATUS_OK, symbol=sym, currency="USD",
            adjustment_basis="close", observations=observations,
            source_references=[f"Stooq daily CSV: {url}"],
            data_as_of=observations[-1].date, data_mode="live",
            notes="Live daily close from Stooq (raw close, not split/dividend adjusted).",
        )

    # ------------------------------------------------------------ financials
    async def get_company_financials(
        self, symbol: str, reporting_period: str, period_type: str
    ) -> c.FinancialsResult:
        mode = self.settings.data_mode
        if not symbol or not symbol.strip():
            return c.financials_error(symbol, "invalid_symbol", "empty symbol", mode)
        if period_type not in ("annual", "quarterly"):
            return c.financials_error(
                symbol, "unsupported_period",
                f"unsupported period_type: {period_type}", mode,
            )

        sym = symbol.strip().upper()
        if _should_inject_transient(self.settings, "get_company_financials", sym):
            return c.financials_error(
                sym, "provider_timeout",
                "injected transient fault (demo): provider timed out", mode,
            )

        if mode == "fixture":
            return self._fixture_financials(sym, reporting_period, period_type)
        return self._live_financials(sym, reporting_period, period_type)

    def _fixture_financials(
        self, sym: str, reporting_period: str, period_type: str
    ) -> c.FinancialsResult:
        fx = self._fin_fixtures.get(sym)
        if fx is None:
            return c.financials_error(
                sym, "invalid_symbol",
                f"no fixture for symbol {sym}; available: {sorted(self._fin_fixtures)}",
                "fixture",
            )
        periods = fx.get("periods", {})
        period = periods.get(reporting_period)
        if period is None:
            # Fall back to the latest annual period if none specified/matched.
            if reporting_period in ("", "latest", None):
                annuals = {k: v for k, v in periods.items() if v.get("period_type") == "annual"}
                if annuals:
                    reporting_period = sorted(annuals)[-1]
                    period = annuals[reporting_period]
        if period is None:
            return c.FinancialsResult(
                status=c.STATUS_NO_DATA, symbol=sym,
                company_name=fx.get("company_name"), period_type=period_type,
                reporting_period=reporting_period, currency=fx.get("currency"),
                units=fx.get("units"), data_mode="fixture",
                notes=f"No sample period '{reporting_period}'. Available: {sorted(periods)}",
            )
        debt = period.get("total_debt", None)
        return c.FinancialsResult(
            status=c.STATUS_OK, symbol=sym, company_name=fx.get("company_name"),
            period_type=period.get("period_type", period_type),
            reporting_period=reporting_period, fiscal_year=period.get("fiscal_year"),
            period_start=period.get("period_start"), period_end=period.get("period_end"),
            revenue=period.get("revenue"), net_income=period.get("net_income"),
            total_debt=debt, units=fx.get("units", "USD"),
            currency=fx.get("currency", "USD"), filing_date=period.get("filing_date"),
            sources=period.get("sources", []), data_mode="fixture",
            notes=(
                "SAMPLE fixture data through the real MCP transport — not live or audited. "
                + ("Debt is null (not reported in this sample): null means unavailable, not zero."
                   if debt is None else "")
            ),
        )

    def _live_financials(
        self, sym: str, reporting_period: str, period_type: str
    ) -> c.FinancialsResult:
        # Honest: no free fundamentals provider is wired. We do not fabricate.
        return c.financials_error(
            sym, "provider_unavailable",
            "live financials provider is not configured; set up a licensed provider "
            "and wire it here. The demo uses DATA_MODE=fixture for financials.",
            "live",
        )
