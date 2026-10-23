"""Application runtime wiring — builds pools, migrations, observability, the MCP
client, the three agents, the checkpointer, the compiled graph and the service.
Used by both the FastAPI app and the CLI so there is a single construction path.
"""

from __future__ import annotations

import logging

from app.agents.market import MarketAnalyst
from app.agents.research import ResearchAnalyst
from app.agents.writer import ReportWriter
from app.config import Settings, get_settings
from app.db.migrate import run_migrations
from app.db.pool import build_pools
from app.db.repositories import Repositories
from app.mcp.client import FinanceMCPClient
from app.observability.tracing import TraceRecorder
from app.observability.usage import UsageRecorder
from app.service import AnalysisService
from app.workflow.deps import WorkflowDeps
from app.workflow.graph import build_checkpointer, build_graph

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.pools = None
        self.repos: Repositories | None = None
        self.recorder: TraceRecorder | None = None
        self.service: AnalysisService | None = None
        self.mcp_client: FinanceMCPClient | None = None

    async def startup(self) -> "Runtime":
        self.pools = build_pools(self.settings)
        await self.pools.open()
        applied = await run_migrations(self.pools.app_pool)
        if applied:
            logger.info("Applied migrations: %s", applied)

        self.repos = Repositories(self.pools.app_pool)
        self.recorder = TraceRecorder(self.repos, self.settings, service_name="app")
        usage = UsageRecorder(self.repos)
        self.mcp_client = FinanceMCPClient(self.settings, self.recorder)

        deps = WorkflowDeps(
            settings=self.settings,
            recorder=self.recorder,
            repos=self.repos,
            mcp_client=self.mcp_client,
            usage_recorder=usage,
            market_agent=MarketAnalyst(self.settings, self.recorder, usage),
            research_agent=ResearchAnalyst(self.settings, self.recorder, usage),
            writer_agent=ReportWriter(self.settings, self.recorder, usage),
        )
        checkpointer = await build_checkpointer(self.pools.checkpointer_pool)
        graph = build_graph(deps, checkpointer)
        self.service = AnalysisService(
            settings=self.settings, repos=self.repos, recorder=self.recorder,
            graph=graph, mcp_client=self.mcp_client,
        )
        logger.info("Runtime ready (data_mode=%s, model=%s)",
                    self.settings.data_mode, self.settings.claude_model)
        return self

    async def shutdown(self) -> None:
        if self.pools:
            await self.pools.close()
