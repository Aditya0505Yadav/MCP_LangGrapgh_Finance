"""Research Analyst role: explain revenue, profit, debt and business performance."""

from __future__ import annotations

import json
from typing import Any

from app.agents.base import AgentOutput, ClaudeAgent
from app.config import Settings
from app.observability.tracing import Span, TraceRecorder
from app.observability.usage import UsageRecorder

PROMPT_VERSION = "research-v1"

SYSTEM_PROMPT = """You are the Research Analyst in a finance analysis workflow.

Your job: explain revenue, profitability, debt and overall business performance
for the requested company and reporting period, using ONLY the evidence provided.

Hard rules:
- Use only the figures and pre-computed metrics given. Do not invent, estimate,
  or recall any numbers from memory.
- Reference the reporting period and sources for each point.
- Missing debt means debt is NOT reported — it is NOT zero debt. Say so if the
  `debt_reported` flag is false.
- Do not silently mix quarterly and annual metrics; note the `period_type`.
- If the evidence is labeled sample/fixture data, say so; never present it as live
  or audited data. Do not produce buy/sell recommendations.

Respond with ONLY a JSON object, no prose around it, with this shape:
{
  "summary": "<2-4 sentence explanation of business performance>",
  "revenue_comment": "<grounded in the revenue figure/period>",
  "profitability_comment": "<grounded in net income / net margin>",
  "debt_comment": "<grounded in debt; state clearly if debt is not reported>",
  "evidence_periods": ["FY2023", "..."],
  "caveats": ["<limitation of this evidence>", "..."]
}
"""


class ResearchAnalyst(ClaudeAgent):
    def __init__(self, settings: Settings, recorder: TraceRecorder, usage: UsageRecorder):
        super().__init__(
            settings=settings, recorder=recorder, usage_recorder=usage,
            role="research_analyst", prompt_version=PROMPT_VERSION,
            system_prompt=SYSTEM_PROMPT,
        )

    async def analyze(
        self,
        *,
        symbol: str,
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
            "computed_metrics": metrics,
            "sources": sources,
            "data_provenance_note": notes,
        }
        user_content = (
            "Analyze the following company financial evidence and respond with the "
            "required JSON only.\n\nEVIDENCE:\n"
            + json.dumps(evidence, indent=2, default=str)
        )
        return await self.run(
            user_content=user_content, node_span=node_span, trace_id=trace_id,
            run_id=run_id, execution_attempt_id=execution_attempt_id,
        )
