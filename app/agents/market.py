"""Market Analyst role: explain observed price changes and variability."""

from __future__ import annotations

import json
from typing import Any

from app.agents.base import AgentOutput, ClaudeAgent
from app.config import Settings
from app.observability.tracing import Span, TraceRecorder
from app.observability.usage import UsageRecorder

PROMPT_VERSION = "market-v1"

SYSTEM_PROMPT = """You are the Market Analyst in a finance analysis workflow.

Your job: explain the observed price changes and variability for the requested
symbol over the requested period, using ONLY the evidence provided to you.

Hard rules:
- Use only the dated price observations and pre-computed metrics given. Do not
  invent, estimate, or recall any numbers from memory.
- Reference specific observation dates when you make a point.
- Distinguish historical observations from any current quotation. The evidence
  includes `data_as_of`; treat it as the last observed date, not "today".
- Do NOT produce buy/sell recommendations or price targets.
- If the evidence is labeled sample/fixture data, treat it as illustrative and
  say so; never present it as live market data.

Respond with ONLY a JSON object, no prose around it, with this shape:
{
  "summary": "<2-4 sentence explanation of the price behavior>",
  "observations": ["<dated point>", "..."],
  "volatility_comment": "<comment grounded in the volatility metric>",
  "evidence_dates": ["YYYY-MM-DD", "..."],
  "caveats": ["<limitation of this evidence>", "..."]
}
"""


class MarketAnalyst(ClaudeAgent):
    def __init__(self, settings: Settings, recorder: TraceRecorder, usage: UsageRecorder):
        super().__init__(
            settings=settings, recorder=recorder, usage_recorder=usage,
            role="market_analyst", prompt_version=PROMPT_VERSION,
            system_prompt=SYSTEM_PROMPT,
        )

    async def analyze(
        self,
        *,
        symbol: str,
        date_range: dict[str, str],
        metrics: dict[str, Any],
        notes: str | None,
        sources: list[str],
        node_span: Span,
        trace_id: str,
        run_id: str | None,
        execution_attempt_id: str | None,
    ) -> AgentOutput:
        evidence = {
            "symbol": symbol,
            "requested_period": date_range,
            "computed_metrics": metrics,
            "source_references": sources,
            "data_provenance_note": notes,
        }
        user_content = (
            "Analyze the following market evidence and respond with the required "
            "JSON only.\n\nEVIDENCE:\n" + json.dumps(evidence, indent=2, default=str)
        )
        return await self.run(
            user_content=user_content, node_span=node_span, trace_id=trace_id,
            run_id=run_id, execution_attempt_id=execution_attempt_id,
        )
