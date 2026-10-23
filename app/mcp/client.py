"""FinanceMCPClient — the application's connector to the real MCP server.

Responsibilities:
  * manage the SDK connection lifecycle (a short-lived Streamable HTTP connection
    per call, which also makes concurrent calls from parallel branches trivially
    safe),
  * tool discovery / contract verification,
  * propagate trace context via HTTP headers (never via model-visible args),
  * own the single retry layer for transient tool failures, emitting a separate
    ``mcp_client`` span per attempt.

Outcome handling preserves the three categories:
  * valid data / valid-empty data / provider-reported error  -> returned as a
    dict (the node inspects ``status``); a provider error, even after exhausting
    retries, is a branch failure, not a crash.
  * MCP transport failure (exhausted) or an unexpected tool crash  -> raised, so
    the run stops visibly.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from app.config import Settings
from app.observability.tracing import Span, TraceRecorder, build_trace_headers
from app.retry import Classification, run_with_retries
from mcp_server import contracts as c

TOOL_TIMEOUT_MS = 15000
EXPECTED_TOOLS = {"get_stock_data", "get_company_financials"}


class McpConnectionError(Exception):
    """Transport/JSON-RPC failure reaching the MCP server (retryable transient)."""


class McpToolCrashError(Exception):
    """The server reported an unexpected tool crash (is_error) — not retried."""


class RetryableProviderError(Exception):
    """A transient provider status (retryable). Carries the last result dict."""

    def __init__(self, error_code: str, data: dict[str, Any]) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.data = data


class FinanceMCPClient:
    def __init__(self, settings: Settings, recorder: TraceRecorder) -> None:
        self._url = settings.mcp_server_url
        self._recorder = recorder
        self._max_attempts = settings.max_external_attempts

    def _transport(self, headers: dict[str, str] | None = None) -> StreamableHttpTransport:
        return StreamableHttpTransport(url=self._url, headers=headers or {})

    async def list_tool_names(self) -> list[str]:
        async with Client(self._transport()) as client:
            tools = await client.list_tools()
            return [t.name for t in tools]

    async def verify_tools(self) -> list[str]:
        """Verify the server advertises exactly the two expected tools.

        Raises RuntimeError if an expected tool is missing (contract mismatch).
        """
        names = set(await self.list_tool_names())
        missing = EXPECTED_TOOLS - names
        if missing:
            raise RuntimeError(
                f"MCP server is missing expected tool(s): {sorted(missing)}; "
                f"advertised: {sorted(names)}"
            )
        return sorted(names)

    async def call_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        node_span: Span,
        trace_id: str,
        run_id: str | None,
        execution_attempt_id: str | None,
    ) -> dict[str, Any]:
        """Call a tool with retries. Returns the structured result dict."""

        async def operation(attempt: int) -> dict[str, Any]:
            async with self._recorder.span(
                span_kind="mcp_client",
                trace_id=trace_id,
                run_id=run_id,
                execution_attempt_id=execution_attempt_id,
                parent_span_id=node_span.span_id,
                tool_name=tool_name,
                operation_name="mcp.call_tool",
                attempt_number=attempt,
                timeout_ms=TOOL_TIMEOUT_MS,
                input_payload=args,
            ) as csp:
                headers = build_trace_headers(
                    trace_id=trace_id,
                    parent_span_id=csp.span_id,
                    run_id=run_id,
                    execution_attempt_id=execution_attempt_id,
                )
                try:
                    async with Client(self._transport(headers)) as client:
                        res = await client.call_tool(
                            tool_name, args, raise_on_error=False
                        )
                except McpToolCrashError:
                    raise
                except Exception as exc:  # transport / JSON-RPC / connection
                    csp.status_override = "error"
                    csp.error_code = "mcp_transport"
                    raise McpConnectionError(str(exc)) from exc

                if res.is_error:
                    detail = (
                        res.content[0].text
                        if res.content and hasattr(res.content[0], "text")
                        else "unknown tool error"
                    )
                    csp.status_override = "error"
                    csp.error_code = "tool_crash"
                    csp.output_payload = {"is_error": True, "detail": detail[:500]}
                    raise McpToolCrashError(detail)

                data = res.structured_content
                if data is None:
                    data = res.data if isinstance(res.data, dict) else {"status": "ok"}
                status = data.get("status", "ok")
                csp.output_payload = {"status": status}
                if status in c.TRANSIENT_CODES:
                    csp.status_override = "error"
                    csp.error_code = status
                    raise RetryableProviderError(status, data)
                if status in c.NON_TRANSIENT_CODES:
                    csp.status_override = "error"
                    csp.error_code = status
                return data

        def classify(exc: BaseException) -> Classification:
            if isinstance(exc, RetryableProviderError):
                return Classification(True, None, exc.error_code, "provider_error")
            if isinstance(exc, McpConnectionError):
                return Classification(True, None, "mcp_transport", "McpConnectionError")
            return Classification(False, None, getattr(exc, "error_code", None),
                                  type(exc).__name__)

        async def record_attempt(outcome) -> None:
            await self._recorder.event(
                event_type="tool_attempt",
                severity="info" if outcome.succeeded else "warning",
                message=f"{tool_name} attempt {outcome.attempt_number} "
                        f"{'succeeded' if outcome.succeeded else 'failed'}",
                run_id=run_id,
                execution_attempt_id=execution_attempt_id,
                details={
                    "tool": tool_name,
                    "attempt": outcome.attempt_number,
                    "succeeded": outcome.succeeded,
                    "sleep_seconds": outcome.sleep_seconds,
                    "error_code": outcome.classification.error_code
                    if outcome.classification else None,
                },
            )

        try:
            return await run_with_retries(
                operation,
                max_attempts=self._max_attempts,
                classify=classify,
                record_attempt=record_attempt,
            )
        except RetryableProviderError as exc:
            # Transient provider failure that never cleared -> branch failure,
            # NOT a crash. Return the last result so validation can see it.
            return exc.data
