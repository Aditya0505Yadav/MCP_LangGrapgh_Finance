"""Span/event recorder with redaction, bounded payloads, and cross-process
trace correlation.

A span is created *before* work starts and finalized on success or exception.
Correlation across processes (the app and the MCP server) uses explicit
headers: the app sends ``x-trace-id`` / ``x-parent-span-id`` / ``x-run-id`` /
``x-execution-attempt-id`` (plus a W3C ``traceparent`` for standards
compatibility), and the MCP server extracts them from the incoming HTTP request.

Why explicit headers rather than pure W3C ``traceparent``: our span ids are
UUIDs (per the schema), while the W3C span-id is a 16-hex value. We propagate
both — the UUID headers link spans precisely to our tables; ``traceparent``
remains valid for any standard tooling.

Payloads are sanitized (secrets redacted), size-capped, and hashed. With
``LOG_PAYLOADS=false`` only the hashes are stored.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from app.config import Settings
from app.db.repositories import Repositories

# --------------------------------------------------------------------------- #
# ID + trace-context helpers (shared by the app and the MCP server).
# --------------------------------------------------------------------------- #

_SECRET_HINTS = {
    "authorization", "api_key", "apikey", "x-api-key", "anthropic_api_key",
    "key", "token", "password", "passwd", "secret", "database_url", "conninfo",
    "dsn", "cookie", "set-cookie",
}
_REDACTED = "[REDACTED]"


def new_trace_id() -> str:
    """32-hex trace id (also valid as the W3C trace-id)."""
    return uuid.uuid4().hex


def new_span_id() -> str:
    """UUID span id (matches the UUID columns in app.spans)."""
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _w3c_spanid_from_uuid(span_uuid: str) -> str:
    return span_uuid.replace("-", "")[:16]


def build_trace_headers(
    *,
    trace_id: str,
    parent_span_id: str,
    run_id: str | None,
    execution_attempt_id: str | None,
) -> dict[str, str]:
    # Coerce to str: DB-sourced ids (e.g. run_id from a UUID column) may be
    # uuid.UUID objects, which HTTP header encoders reject.
    trace_id = str(trace_id)
    parent_span_id = str(parent_span_id)
    headers = {
        "x-trace-id": trace_id,
        "x-parent-span-id": parent_span_id,
        "traceparent": f"00-{trace_id}-{_w3c_spanid_from_uuid(parent_span_id)}-01",
    }
    if run_id:
        headers["x-run-id"] = str(run_id)
    if execution_attempt_id:
        headers["x-execution-attempt-id"] = str(execution_attempt_id)
    return headers


@dataclass
class TraceContext:
    trace_id: str | None
    parent_span_id: str | None
    run_id: str | None
    execution_attempt_id: str | None


def parse_trace_context(headers: Any) -> TraceContext:
    """Extract trace context from an incoming request's headers.

    ``headers`` may be any mapping with case-insensitive ``.get`` (Starlette
    Headers qualifies).
    """
    def get(name: str) -> str | None:
        try:
            return headers.get(name)
        except Exception:
            return None

    return TraceContext(
        trace_id=get("x-trace-id"),
        parent_span_id=get("x-parent-span-id"),
        run_id=get("x-run-id"),
        execution_attempt_id=get("x-execution-attempt-id"),
    )


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.strip().lower() in _SECRET_HINTS:
                out[k] = _REDACTED
            else:
                out[k] = _sanitize(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


# --------------------------------------------------------------------------- #
# Span handle.
# --------------------------------------------------------------------------- #

@dataclass
class Span:
    span_id: str
    trace_id: str
    span_kind: str
    service_name: str
    started_monotonic: float
    run_id: str | None = None
    execution_attempt_id: str | None = None
    parent_span_id: str | None = None
    node_name: str | None = None
    operation_name: str | None = None
    tool_name: str | None = None
    agent_role: str | None = None
    logical_step_id: str | None = None
    attempt_number: int | None = None
    request_id: str | None = None
    timeout_ms: int | None = None
    # Mutable outputs the caller may set before the span is finalized.
    output_payload: Any = None
    provider_request_id: str | None = None
    status_override: str | None = None
    error_code: str | None = None
    retryable: bool | None = field(default=None)


# --------------------------------------------------------------------------- #
# Recorder.
# --------------------------------------------------------------------------- #

class TraceRecorder:
    def __init__(self, repos: Repositories, settings: Settings, service_name: str) -> None:
        self._repos = repos
        self._settings = settings
        self._service = service_name

    def _prepare_payload(self, value: Any) -> tuple[Any, str | None, bool]:
        if value is None:
            return None, None, False
        sanitized = _sanitize(value)
        try:
            canonical = json.dumps(sanitized, sort_keys=True, default=str)
        except (TypeError, ValueError):
            canonical = str(sanitized)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        cap = self._settings.payload_max_chars
        if not self._settings.log_payloads:
            return None, digest, False
        if len(canonical) > cap:
            return {"_preview": canonical[:cap], "_truncated_chars": len(canonical) - cap}, digest, True
        return sanitized, digest, False

    async def start_span(
        self,
        *,
        span_kind: str,
        trace_id: str,
        run_id: str | None = None,
        execution_attempt_id: str | None = None,
        parent_span_id: str | None = None,
        node_name: str | None = None,
        operation_name: str | None = None,
        tool_name: str | None = None,
        agent_role: str | None = None,
        logical_step_id: str | None = None,
        attempt_number: int | None = None,
        request_id: str | None = None,
        timeout_ms: int | None = None,
        input_payload: Any = None,
    ) -> Span:
        span_id = new_span_id()
        payload, in_hash, truncated = self._prepare_payload(input_payload)
        fields: dict[str, Any] = {
            "span_id": span_id,
            "trace_id": trace_id,
            "parent_span_id": parent_span_id,
            "run_id": run_id,
            "execution_attempt_id": execution_attempt_id,
            "logical_step_id": logical_step_id,
            "node_name": node_name,
            "agent_role": agent_role,
            "span_kind": span_kind,
            "service_name": self._service,
            "operation_name": operation_name,
            "tool_name": tool_name,
            "attempt_number": attempt_number,
            "status": "running",
            "started_at": _utcnow(),
            "timeout_ms": timeout_ms,
            "request_id": request_id,
            "input_payload": payload,
            "input_hash": in_hash,
            "payload_truncated": truncated,
        }
        await self._repos.insert_span(fields)
        return Span(
            span_id=span_id,
            trace_id=trace_id,
            span_kind=span_kind,
            service_name=self._service,
            started_monotonic=perf_counter(),
            run_id=run_id,
            execution_attempt_id=execution_attempt_id,
            parent_span_id=parent_span_id,
            node_name=node_name,
            operation_name=operation_name,
            tool_name=tool_name,
            agent_role=agent_role,
            logical_step_id=logical_step_id,
            attempt_number=attempt_number,
            request_id=request_id,
            timeout_ms=timeout_ms,
        )

    async def finish_span(
        self,
        span: Span,
        *,
        status: str = "ok",
        error: BaseException | None = None,
        error_code: str | None = None,
        error_type: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        duration_ms = int((perf_counter() - span.started_monotonic) * 1000)
        payload, out_hash, truncated = self._prepare_payload(span.output_payload)
        fields: dict[str, Any] = {
            "status": status,
            "finished_at": _utcnow(),
            "duration_ms": duration_ms,
            "output_payload": payload,
            "output_hash": out_hash,
            "payload_truncated": truncated,
            "provider_request_id": span.provider_request_id,
            "error_code": error_code if error_code is not None else span.error_code,
            "retryable": retryable if retryable is not None else span.retryable,
        }
        if error is not None:
            fields["error_type"] = error_type or type(error).__name__
            fields["error_message"] = str(error)[: self._settings.payload_max_chars]
        elif error_type is not None:
            fields["error_type"] = error_type
        await self._repos.update_span(span.span_id, fields)

    @asynccontextmanager
    async def span(self, **kwargs: Any):
        """Context manager that finalizes the span on success or exception.

        Set ``span.output_payload`` / ``span.status_override`` /
        ``span.provider_request_id`` before exiting to record them.
        """
        sp = await self.start_span(**kwargs)
        try:
            yield sp
        except BaseException as exc:  # noqa: BLE001 — finalized then re-raised
            await self.finish_span(
                sp, status="error", error=exc, retryable=sp.retryable,
                error_code=sp.error_code,
            )
            raise
        else:
            await self.finish_span(
                sp, status=sp.status_override or "ok", retryable=sp.retryable,
                error_code=sp.error_code,
            )

    async def event(
        self,
        *,
        event_type: str,
        severity: str = "info",
        message: str | None = None,
        run_id: str | None = None,
        execution_attempt_id: str | None = None,
        span_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        payload, _, _ = self._prepare_payload(details) if details else (None, None, False)
        await self._repos.insert_event(
            event_id=new_span_id(),
            run_id=run_id,
            execution_attempt_id=execution_attempt_id,
            span_id=span_id,
            event_type=event_type,
            severity=severity,
            message=message,
            sanitized_details=payload,
        )
