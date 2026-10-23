"""Redaction, payload capping, and trace-context header round-trip."""

from app.config import Settings
from app.observability.tracing import (
    TraceRecorder,
    _sanitize,
    build_trace_headers,
    parse_trace_context,
)


class _Headers:
    def __init__(self, d):
        self._d = {k.lower(): v for k, v in d.items()}

    def get(self, name):
        return self._d.get(name.lower())


def test_sanitize_redacts_secrets():
    data = {"api_key": "sk-123", "nested": {"authorization": "Bearer x", "ok": 1}}
    out = _sanitize(data)
    assert out["api_key"] == "[REDACTED]"
    assert out["nested"]["authorization"] == "[REDACTED]"
    assert out["nested"]["ok"] == 1


def test_trace_headers_round_trip():
    headers = build_trace_headers(
        trace_id="a" * 32, parent_span_id="11111111-1111-1111-1111-111111111111",
        run_id="r1", execution_attempt_id="e1",
    )
    assert "traceparent" in headers
    ctx = parse_trace_context(_Headers(headers))
    assert ctx.trace_id == "a" * 32
    assert ctx.parent_span_id == "11111111-1111-1111-1111-111111111111"
    assert ctx.run_id == "r1"
    assert ctx.execution_attempt_id == "e1"


def test_prepare_payload_caps_and_hashes():
    settings = Settings(anthropic_api_key="t", payload_max_chars=256, log_payloads=True)
    rec = TraceRecorder(repos=object(), settings=settings, service_name="t")
    big = {"text": "x" * 1000}
    payload, digest, truncated = rec._prepare_payload(big)
    assert truncated is True
    assert "_preview" in payload
    assert digest and len(digest) == 64


def test_prepare_payload_hash_only_when_logging_disabled():
    settings = Settings(anthropic_api_key="t", log_payloads=False)
    rec = TraceRecorder(repos=object(), settings=settings, service_name="t")
    payload, digest, truncated = rec._prepare_payload({"a": 1})
    assert payload is None
    assert digest is not None
