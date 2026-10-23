"""The real MCP server: exactly two financial-data tools over Streamable HTTP.

Each tool call:
  * extracts trace context from the incoming HTTP request headers (server-side
    extraction — tested, not assumed),
  * opens an ``mcp_server`` span parented to the client's span (same trace_id),
  * opens a nested ``provider_http`` span around the provider call (live mode),
  * returns a structured result whose ``status`` distinguishes ok / no_data /
    error codes.

The server writes its spans to the SAME PostgreSQL ``app`` schema as the
application, so a trace links application -> MCP client -> MCP server -> provider.
No business/model-visible tool argument ever carries tracing fields.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import get_settings
from app.eventloop import use_selector_event_loop_on_windows
from app.db.migrate import run_migrations
from app.db.pool import build_app_pool
from app.db.repositories import Repositories
from app.observability.tracing import (
    TraceContext,
    TraceRecorder,
    new_trace_id,
    parse_trace_context,
)
from mcp_server import contracts as c
from mcp_server.providers import ProviderAdapter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp_server")

# psycopg async requires the Selector event loop on Windows.
use_selector_event_loop_on_windows()

settings = get_settings()
_pool = build_app_pool(settings)
_repos = Repositories(_pool)
_recorder = TraceRecorder(_repos, settings, service_name="mcp-server")
_provider = ProviderAdapter(settings)


@asynccontextmanager
async def _lifespan(server: FastMCP):
    await _pool.open(wait=True)
    await run_migrations(_pool)  # idempotent; advisory-locked
    logger.info("MCP server ready (data_mode=%s)", settings.data_mode)
    try:
        yield
    finally:
        await _pool.close()


mcp = FastMCP(name="finance-data", lifespan=_lifespan)


def _current_trace_context() -> TraceContext:
    try:
        request = get_http_request()
        return parse_trace_context(request.headers)
    except Exception:
        return TraceContext(None, None, None, None)


def _server_status(result_status: str) -> tuple[str, str | None]:
    """Map a tool result status to (span_status, error_code)."""
    if result_status in c.ALL_ERROR_CODES:
        return "error", result_status
    return "ok", None


@mcp.tool
async def get_stock_data(
    symbol: str, start_date: str, end_date: str
) -> c.StockDataResult:
    """Retrieve daily closing prices for a stock symbol over a date range.

    Args:
        symbol: Ticker symbol, e.g. "AAPL".
        start_date: Inclusive start date, ISO format YYYY-MM-DD.
        end_date: Inclusive end date, ISO format YYYY-MM-DD.
    """
    ctx = _current_trace_context()
    trace_id = ctx.trace_id or new_trace_id()
    async with _recorder.span(
        span_kind="mcp_server",
        trace_id=trace_id,
        run_id=ctx.run_id,
        execution_attempt_id=ctx.execution_attempt_id,
        parent_span_id=ctx.parent_span_id,
        tool_name="get_stock_data",
        operation_name="tool.get_stock_data",
        input_payload={"symbol": symbol, "start_date": start_date, "end_date": end_date},
    ) as span:
        if settings.data_mode == "live":
            async with _recorder.span(
                span_kind="provider_http",
                trace_id=trace_id,
                run_id=ctx.run_id,
                execution_attempt_id=ctx.execution_attempt_id,
                parent_span_id=span.span_id,
                tool_name="get_stock_data",
                operation_name="provider.stooq.daily",
            ) as psp:
                result = await _provider.get_stock_data(symbol, start_date, end_date)
                psp.output_payload = {"status": result.status,
                                      "observations": len(result.observations)}
                p_status, p_err = _server_status(result.status)
                psp.status_override, psp.error_code = p_status, p_err
        else:
            result = await _provider.get_stock_data(symbol, start_date, end_date)

        span.output_payload = {
            "status": result.status,
            "observations": len(result.observations),
            "data_mode": result.data_mode,
        }
        status, err = _server_status(result.status)
        span.status_override, span.error_code = status, err
        return result


@mcp.tool
async def get_company_financials(
    symbol: str, reporting_period: str = "latest", period_type: str = "annual"
) -> c.FinancialsResult:
    """Retrieve revenue, net income and debt for a company and reporting period.

    Args:
        symbol: Ticker symbol, e.g. "AAPL".
        reporting_period: e.g. "FY2023", or "latest" for the most recent annual.
        period_type: "annual" or "quarterly".
    """
    ctx = _current_trace_context()
    trace_id = ctx.trace_id or new_trace_id()
    async with _recorder.span(
        span_kind="mcp_server",
        trace_id=trace_id,
        run_id=ctx.run_id,
        execution_attempt_id=ctx.execution_attempt_id,
        parent_span_id=ctx.parent_span_id,
        tool_name="get_company_financials",
        operation_name="tool.get_company_financials",
        input_payload={"symbol": symbol, "reporting_period": reporting_period,
                       "period_type": period_type},
    ) as span:
        if settings.data_mode == "live":
            async with _recorder.span(
                span_kind="provider_http",
                trace_id=trace_id,
                run_id=ctx.run_id,
                execution_attempt_id=ctx.execution_attempt_id,
                parent_span_id=span.span_id,
                tool_name="get_company_financials",
                operation_name="provider.financials",
            ) as psp:
                result = await _provider.get_company_financials(
                    symbol, reporting_period, period_type
                )
                p_status, p_err = _server_status(result.status)
                psp.output_payload = {"status": result.status}
                psp.status_override, psp.error_code = p_status, p_err
        else:
            result = await _provider.get_company_financials(
                symbol, reporting_period, period_type
            )

        span.output_payload = {"status": result.status, "data_mode": result.data_mode}
        status, err = _server_status(result.status)
        span.status_override, span.error_code = status, err
        return result


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "mcp-server",
                         "data_mode": settings.data_mode})


def main() -> None:
    mcp.run(transport="http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
