"""Shared Claude call for the agent roles.

* The Anthropic SDK's own retries are disabled (``max_retries=0``); this module's
  single retry layer owns transient retries, so attempts never multiply.
* Every *actual* (successful) API call is recorded once in ``app.llm_calls`` with
  its real usage metadata and a versioned cost estimate — including a successful
  call that then returns a refusal (it was still billed).
* Each attempt is an ``llm`` span; a failed attempt's span is marked error.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

from app.config import Settings
from app.observability.tracing import Span, TraceRecorder, new_span_id
from app.observability.usage import UsageRecorder
from app.retry import Classification, run_with_retries


class AgentRefusalError(Exception):
    """The model returned stop_reason == 'refusal' (non-retryable)."""


@dataclass
class AgentOutput:
    text: str
    parsed: dict[str, Any]
    requested_model: str
    returned_model: str | None
    stop_reason: str | None
    request_id: str | None


def _extract_text(content: list) -> str:
    parts = [getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text"]
    return "\n".join(p for p in parts if p).strip()


def _extract_json(text: str) -> dict[str, Any]:
    """Best-effort parse of a JSON object from the model's text."""
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {"_value": obj}
    except (json.JSONDecodeError, ValueError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else {"_value": obj}
        except (json.JSONDecodeError, ValueError):
            pass
    return {"_unparsed_text": text}


class ClaudeAgent:
    def __init__(
        self,
        *,
        settings: Settings,
        recorder: TraceRecorder,
        usage_recorder: UsageRecorder,
        role: str,
        prompt_version: str,
        system_prompt: str,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self._settings = settings
        self._recorder = recorder
        self._usage = usage_recorder
        self.role = role
        self.prompt_version = prompt_version
        self.system_prompt = system_prompt
        # max_retries=0: this module owns retries (see app.retry).
        self._client = client or AsyncAnthropic(
            api_key=settings.anthropic_api_key, max_retries=0
        )

    def _prompt_hash(self, user_content: str) -> str:
        blob = f"{self.prompt_version}\n{self.system_prompt}\n{user_content}"
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    async def run(
        self,
        *,
        user_content: str,
        node_span: Span,
        trace_id: str,
        run_id: str | None,
        execution_attempt_id: str | None,
        max_tokens: int | None = None,
    ) -> AgentOutput:
        model = self._settings.claude_model
        effort = self._settings.claude_effort
        max_toks = max_tokens or self._settings.claude_max_tokens
        prompt_hash = self._prompt_hash(user_content)
        gen_params = {
            "model": model,
            "max_tokens": max_toks,
            "effort": effort,
            "thinking": "adaptive",
        }

        async def operation(attempt: int) -> AgentOutput:
            async with self._recorder.span(
                span_kind="llm",
                trace_id=trace_id,
                run_id=run_id,
                execution_attempt_id=execution_attempt_id,
                parent_span_id=node_span.span_id,
                agent_role=self.role,
                operation_name="claude.messages.create",
                attempt_number=attempt,
                input_payload={
                    "prompt_version": self.prompt_version,
                    "params": gen_params,
                    "user_preview": user_content[:1000],
                },
            ) as sp:
                resp = await self._client.messages.create(
                    model=model,
                    max_tokens=max_toks,
                    system=self.system_prompt,
                    messages=[{"role": "user", "content": user_content}],
                    thinking={"type": "adaptive"},
                    output_config={"effort": effort},
                )
                text = _extract_text(resp.content)
                request_id = getattr(resp, "_request_id", None)
                sp.provider_request_id = request_id
                sp.output_payload = {
                    "stop_reason": resp.stop_reason,
                    "returned_model": resp.model,
                    "text_preview": text[:800],
                }

                # Record this actual, billed call exactly once.
                await self._usage.record(
                    llm_call_id=new_span_id(),
                    span_id=sp.span_id,
                    provider="anthropic",
                    requested_model=model,
                    returned_model=resp.model,
                    prompt_version=self.prompt_version,
                    prompt_hash=prompt_hash,
                    generation_parameters=gen_params,
                    usage=resp.usage,
                    stop_reason=resp.stop_reason,
                    provider_request_id=request_id,
                )

                if resp.stop_reason == "refusal":
                    details = getattr(resp, "stop_details", None)
                    sp.status_override = "error"
                    sp.error_code = "refusal"
                    raise AgentRefusalError(
                        f"model refused: {getattr(details, 'category', None)}"
                    )

                return AgentOutput(
                    text=text,
                    parsed=_extract_json(text),
                    requested_model=model,
                    returned_model=resp.model,
                    stop_reason=resp.stop_reason,
                    request_id=request_id,
                )

        def classify(exc: BaseException) -> Classification:
            if isinstance(exc, AgentRefusalError):
                return Classification(False, None, "refusal", "AgentRefusalError")
            if isinstance(exc, anthropic.RateLimitError):
                retry_after = None
                try:
                    retry_after = float(exc.response.headers.get("retry-after"))
                except Exception:
                    retry_after = None
                return Classification(True, retry_after, "rate_limited", "RateLimitError")
            if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
                return Classification(True, None, "provider_timeout", type(exc).__name__)
            if isinstance(exc, anthropic.APIStatusError):
                retryable = exc.status_code >= 500
                return Classification(
                    retryable, None,
                    "server_error" if retryable else "client_error",
                    type(exc).__name__,
                )
            return Classification(False, None, None, type(exc).__name__)

        async def record_attempt(outcome) -> None:
            await self._recorder.event(
                event_type="llm_attempt",
                severity="info" if outcome.succeeded else "warning",
                message=f"{self.role} attempt {outcome.attempt_number} "
                        f"{'succeeded' if outcome.succeeded else 'failed'}",
                run_id=run_id,
                execution_attempt_id=execution_attempt_id,
                details={
                    "role": self.role,
                    "attempt": outcome.attempt_number,
                    "succeeded": outcome.succeeded,
                    "sleep_seconds": outcome.sleep_seconds,
                    "error_code": outcome.classification.error_code
                    if outcome.classification else None,
                },
            )

        return await run_with_retries(
            operation,
            max_attempts=self._settings.max_external_attempts,
            classify=classify,
            record_attempt=record_attempt,
        )
