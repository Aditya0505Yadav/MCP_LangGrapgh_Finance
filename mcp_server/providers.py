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

import json
from datetime import date
from pathlib import Path

import anyio

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
        """Real-time daily history + current quote from Yahoo Finance (yfinance).

        yfinance is blocking, so it runs in a worker thread (never on the async
        event loop).
        """
        from datetime import timedelta

        def _fetch():
            import yfinance as yf
            ticker = yf.Ticker(sym)
            hist = ticker.history(
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                auto_adjust=False,
            )
            try:
                fast = ticker.fast_info
            except Exception:
                fast = None
            return hist, fast

        try:
            hist, fast = await anyio.to_thread.run_sync(_fetch)
        except Exception as exc:  # noqa: BLE001
            return c.stock_error(sym, "provider_unavailable",
                                 f"yfinance error: {exc}", "live")

        if hist is None or len(hist) == 0:
            return c.StockDataResult(
                status=c.STATUS_NO_DATA, symbol=sym, data_mode="live",
                source_references=["Yahoo Finance via yfinance"],
                notes="No live observations in the requested range.",
            )

        has_adj = "Adj Close" in hist.columns
        observations: list[c.StockObservation] = []
        for idx, row in hist.iterrows():
            adj = float(row["Adj Close"]) if has_adj else None
            observations.append(c.StockObservation(
                date=idx.date().isoformat(),
                close=round(float(row["Close"]), 4),
                adjusted_close=round(adj, 4) if adj is not None else None,
            ))
        observations = observations[: c.MAX_OBSERVATIONS]

        def num(attr):
            try:
                return float(getattr(fast, attr))
            except Exception:
                return None

        currency = getattr(fast, "currency", None) or "USD"
        return c.StockDataResult(
            status=c.STATUS_OK, symbol=sym, currency=currency,
            adjustment_basis="adjusted_close" if has_adj else "close",
            observations=observations,
            source_references=["Yahoo Finance via yfinance (real-time quote + daily history)"],
            data_as_of=observations[-1].date, data_mode="live",
            current_price=num("last_price"), previous_close=num("previous_close"),
            fifty_two_week_high=num("year_high"), fifty_two_week_low=num("year_low"),
            market_time=c.utcnow_iso(),
            notes="Live Yahoo Finance data. Quotes may be ~15 min delayed; the latest "
                  "point may be an in-progress session close.",
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
        return await self._live_financials(sym, reporting_period, period_type)

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

    async def _live_financials(
        self, sym: str, reporting_period: str, period_type: str
    ) -> c.FinancialsResult:
        """Real-time fundamentals from Yahoo Finance (yfinance), off the event loop."""

        def _fetch():
            import yfinance as yf
            ticker = yf.Ticker(sym)
            inc = ticker.income_stmt
            bs = ticker.balance_sheet
            try:
                info = ticker.info
            except Exception:
                info = {}
            return inc, bs, info

        try:
            inc, bs, info = await anyio.to_thread.run_sync(_fetch)
        except Exception as exc:  # noqa: BLE001
            return c.financials_error(sym, "provider_unavailable",
                                      f"yfinance error: {exc}", "live")

        def pick(df, *names):
            try:
                for n in names:
                    if n in df.index:
                        s = df.loc[n].dropna()
                        if len(s):
                            return float(s.iloc[0]), s.index[0]
            except Exception:
                pass
            return None, None

        revenue, rev_dt = pick(inc, "Total Revenue", "Operating Revenue")
        net_income, _ = pick(inc, "Net Income", "Net Income Common Stockholders")
        total_debt, _ = pick(bs, "Total Debt", "Long Term Debt")
        if revenue is None and net_income is None:
            return c.financials_error(sym, "no_data",
                                      "Yahoo returned no fundamentals", "live")

        fiscal_year = period_end = None
        if rev_dt is not None:
            try:
                period_end = rev_dt.date().isoformat()
                fiscal_year = rev_dt.year
            except Exception:
                pass

        info = info if isinstance(info, dict) else {}
        return c.FinancialsResult(
            status=c.STATUS_OK, symbol=sym,
            company_name=info.get("longName") or sym, period_type="annual",
            reporting_period=f"FY{fiscal_year}" if fiscal_year else reporting_period,
            fiscal_year=fiscal_year, period_end=period_end,
            revenue=revenue, net_income=net_income, total_debt=total_debt,
            units="USD", currency="USD",
            market_cap=info.get("marketCap"), pe_ratio=info.get("trailingPE"),
            sources=["Yahoo Finance via yfinance (income statement, balance sheet, key stats)"],
            data_mode="live",
            notes="Live Yahoo Finance fundamentals (most recent annual period)."
                  + (" Total debt for financial-sector firms includes large borrowings."
                     if total_debt else ""),
        )
