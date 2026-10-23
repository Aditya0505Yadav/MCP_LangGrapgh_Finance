"""Report Writer role: combine both analyses, explain evidence-supported risks."""

from __future__ import annotations

import json
from typing import Any

from app.agents.base import AgentOutput, ClaudeAgent
from app.config import Settings
from app.observability.tracing import Span, TraceRecorder
from app.observability.usage import UsageRecorder

PROMPT_VERSION = "writer-v1"

SYSTEM_PROMPT = """You are the Report Writer in a finance analysis workflow. You
have no data tools; you synthesize ONLY the two analyst findings and the evidence
references supplied to you.

Hard rules:
- Combine the Market Analyst and Research Analyst findings into one clear report.
- Use only supplied evidence. Do not introduce new numbers or facts.
- Include a Risks section whose every item is supported by the supplied evidence
  (e.g. high volatility, undisclosed debt, sample-only data). No speculative risks.
- Include a Limitations section (what the evidence does and does not cover).
- Cite the sources provided. If data is labeled sample/fixture, state plainly that
  the report is based on sample data, not live market data.
- Do NOT produce buy/sell recommendations or price targets.

Respond with ONLY a JSON object, no prose around it, with this shape:
{
  "report_markdown": "<the full report as Markdown>",
  "risks": ["<evidence-supported risk>", "..."],
  "limitations": ["<limitation>", "..."],
  "sources": ["<source reference>", "..."]
}
"""


class ReportWriter(ClaudeAgent):
    def __init__(self, settings: Settings, recorder: TraceRecorder, usage: UsageRecorder):
        super().__init__(
            settings=settings, recorder=recorder, usage_recorder=usage,
            role="report_writer", prompt_version=PROMPT_VERSION,
            system_prompt=SYSTEM_PROMPT,
        )

    async def write(
        self,
        *,
        symbol: str,
        market_analysis: dict[str, Any],
        research_analysis: dict[str, Any],
        market_sources: list[str],
        financial_sources: list[str],
        data_mode: str,
        node_span: Span,
        trace_id: str,
        run_id: str | None,
        execution_attempt_id: str | None,
        max_tokens: int,
    ) -> AgentOutput:
        evidence = {
            "symbol": symbol,
            "data_mode": data_mode,
            "market_analyst_findings": market_analysis,
            "research_analyst_findings": research_analysis,
            "all_sources": list(market_sources) + list(financial_sources),
        }
        user_content = (
            "Write the final report from the following two analyst findings and "
            "sources. Respond with the required JSON only.\n\nEVIDENCE:\n"
            + json.dumps(evidence, indent=2, default=str)
        )
        return await self.run(
            user_content=user_content, node_span=node_span, trace_id=trace_id,
            run_id=run_id, execution_attempt_id=execution_attempt_id,
            max_tokens=max_tokens,
        )
