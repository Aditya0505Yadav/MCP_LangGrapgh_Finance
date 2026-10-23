"""End-to-end integration test (real Postgres + real MCP transport, mock agents).

Skipped unless RUN_INTEGRATION=1. Requires:
  * PostgreSQL reachable at DATABASE_URL (default localhost:5432),
  * the MCP server running at MCP_SERVER_URL (default localhost:8001/mcp) in
    fixture mode.

Run it with:
    docker compose up -d postgres
    DATABASE_URL=postgresql://finance:finance@localhost:5432/finance_demo \
    MCP_SERVER_URL=http://localhost:8001/mcp DATA_MODE=fixture \
    python -m mcp_server.server &            # terminal 1
    RUN_INTEGRATION=1 ANTHROPIC_API_KEY=mock \
    DATABASE_URL=... MCP_SERVER_URL=... python -m pytest tests/test_integration.py -v

It uses MOCK agents, so no real Anthropic call is made — this isolates the
LangGraph + MCP + PostgreSQL integration (parallelism, join, cross-process
tracing, checkpoint resume).
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from app.eventloop import use_selector_event_loop_on_windows

use_selector_event_loop_on_windows()  # psycopg async needs Selector loop on Windows

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1",
                       reason="set RUN_INTEGRATION=1 and start services"),
]


class _MockAgent:
    def __init__(self, role):
        self.role = role

    async def analyze(self, **_k):
        await asyncio.sleep(0.15)  # simulate latency so parallel branches overlap
        return SimpleNamespace(parsed={"summary": f"{self.role} mock"}, text="mock")

    async def write(self, **k):
        srcs = list(k.get("market_sources") or []) + list(k.get("financial_sources") or [])
        return SimpleNamespace(
            parsed={"report_markdown": "# Mock report", "risks": ["sample only"],
                    "limitations": ["mock"], "sources": srcs},
            text="# Mock report",
        )


async def _build_service():
    from app.config import Settings
    from app.db.migrate import run_migrations
    from app.db.pool import build_pools
    from app.db.repositories import Repositories
    from app.mcp.client import FinanceMCPClient
    from app.observability.tracing import TraceRecorder
    from app.observability.usage import UsageRecorder
    from app.service import AnalysisService
    from app.workflow.deps import WorkflowDeps
    from app.workflow.graph import build_checkpointer, build_graph

    os.environ.setdefault("ANTHROPIC_API_KEY", "mock")
    s = Settings()
    pools = build_pools(s)
    await pools.open()
    await run_migrations(pools.app_pool)
    repos = Repositories(pools.app_pool)
    rec = TraceRecorder(repos, s, "app")
    deps = WorkflowDeps(s, rec, repos, FinanceMCPClient(s, rec), UsageRecorder(repos),
                        _MockAgent("market_analyst"), _MockAgent("research_analyst"),
                        _MockAgent("report_writer"))
    ckpt = await build_checkpointer(pools.checkpointer_pool)
    graph = build_graph(deps, ckpt)
    svc = AnalysisService(settings=s, repos=repos, recorder=rec, graph=graph,
                          mcp_client=deps.mcp_client)
    return s, repos, svc, pools


async def test_success_overlap_and_cross_process_trace():
    s, repos, svc, pools = await _build_service()
    try:
        c = await svc.create_run(symbol="AAPL", start_date="2023-01-01",
                                 end_date="2023-12-31", reporting_period="FY2023")
        r = await svc.execute_run(c["run_id"])
        assert r["status"] == "completed"
        spans = await repos.get_spans_for_run(c["run_id"])
        by_node = {sp["node_name"]: sp for sp in spans if sp["node_name"]}
        ma, ra = by_node["market_analysis"], by_node["research_analysis"]
        assert ma["started_at"] < ra["finished_at"] and ra["started_at"] < ma["finished_at"]
        cli = {sp["span_id"] for sp in spans if sp["span_kind"] == "mcp_client"}
        srv = [sp for sp in spans if sp["span_kind"] == "mcp_server"]
        assert srv and any(sp["parent_span_id"] in cli for sp in srv)
    finally:
        await pools.close()


async def test_writer_failure_resume_does_not_rerun_retrieval():
    s, repos, svc, pools = await _build_service()
    try:
        s.fail_at = "writer"
        c = await svc.create_run(symbol="AAPL", start_date="2023-01-01",
                                 end_date="2023-12-31", reporting_period="FY2023")
        with pytest.raises(Exception):
            await svc.execute_run(c["run_id"])
        before = sum(1 for sp in await repos.get_spans_for_run(c["run_id"])
                     if sp["span_kind"] == "mcp_server")
        s.fail_at = None
        r = await svc.resume_run(c["run_id"])
        after = sum(1 for sp in await repos.get_spans_for_run(c["run_id"])
                    if sp["span_kind"] == "mcp_server")
        assert r["status"] == "completed"
        assert before == after  # retrieval not re-called on resume
    finally:
        await pools.close()


async def test_invalid_symbol_incomplete():
    s, repos, svc, pools = await _build_service()
    try:
        c = await svc.create_run(symbol="ZZZZ", start_date="2023-01-01", end_date="2023-12-31")
        r = await svc.execute_run(c["run_id"])
        assert r["result_status"] == "incomplete"
    finally:
        await pools.close()
