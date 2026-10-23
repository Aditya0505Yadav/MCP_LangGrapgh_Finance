"""Injected dependencies for workflow nodes.

Passed to nodes via closures (the Nodes object), never through checkpointed state
or config, so live clients/pools are never serialized into a checkpoint.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.market import MarketAnalyst
from app.agents.research import ResearchAnalyst
from app.agents.writer import ReportWriter
from app.config import Settings
from app.db.repositories import Repositories
from app.mcp.client import FinanceMCPClient
from app.observability.tracing import TraceRecorder
from app.observability.usage import UsageRecorder


@dataclass
class WorkflowDeps:
    settings: Settings
    recorder: TraceRecorder
    repos: Repositories
    mcp_client: FinanceMCPClient
    usage_recorder: UsageRecorder
    market_agent: MarketAnalyst
    research_agent: ResearchAnalyst
    writer_agent: ReportWriter
