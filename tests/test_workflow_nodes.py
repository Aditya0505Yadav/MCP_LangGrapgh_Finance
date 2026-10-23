"""Node logic with fakes (validation/route, analysis-skip, mocked-model writer).

These are 'mocked model' tests — no real Anthropic calls, no DB, no MCP.
"""

from types import SimpleNamespace

import pytest

from app.workflow.deps import WorkflowDeps
from app.workflow.nodes import Nodes

CONFIG = {"configurable": {"run_id": "r1", "trace_id": "t1",
                           "execution_attempt_id": None, "attempt_kind": "initial",
                           "fail_at": None}}


def _deps(settings, fake_recorder, fake_repos, **agents):
    return WorkflowDeps(
        settings=settings, recorder=fake_recorder, repos=fake_repos,
        mcp_client=None, usage_recorder=None,
        market_agent=agents.get("market"), research_agent=agents.get("research"),
        writer_agent=agents.get("writer"),
    )


async def test_validate_ready_routes_to_writer(settings, fake_recorder, fake_repos):
    nodes = Nodes(_deps(settings, fake_recorder, fake_repos))
    state = {
        "market_status": "ok", "research_status": "ok",
        "financial_data": {"units": "USD", "fiscal_year": 2023},
        "financial_sources": ["src"],
        "date_range": {"start_date": "2023-01-01", "end_date": "2023-12-31"},
    }
    out = await nodes.validate(state, CONFIG)
    assert out["data_ready"] is True
    assert nodes.route({**state, **out}) == "writer"


async def test_validate_not_ready_routes_to_incomplete(settings, fake_recorder, fake_repos):
    nodes = Nodes(_deps(settings, fake_recorder, fake_repos))
    state = {
        "market_status": "failed", "market_error": "retrieval unavailable",
        "research_status": "ok",
        "financial_data": {"units": "USD", "fiscal_year": 2023},
        "financial_sources": ["src"],
        "date_range": {"start_date": "2023-01-01", "end_date": "2023-12-31"},
    }
    out = await nodes.validate(state, CONFIG)
    assert out["data_ready"] is False
    assert any("market" in i for i in out["validation_issues"])
    assert nodes.route({**state, **out}) == "incomplete"


async def test_market_analysis_skips_when_fetch_failed(settings, fake_recorder, fake_repos):
    nodes = Nodes(_deps(settings, fake_recorder, fake_repos))  # no agent needed
    state = {"market_fetch_status": "failed", "market_fetch_error": "boom"}
    out = await nodes.market_analysis(state, CONFIG)
    assert out["market_status"] == "failed"
    assert "retrieval unavailable" in out["market_error"]


async def test_market_analysis_runs_with_mock_agent(settings, fake_recorder, fake_repos):
    class FakeMarketAgent:
        async def analyze(self, **_k):
            return SimpleNamespace(parsed={"summary": "ok"}, text="summary")

    nodes = Nodes(_deps(settings, fake_recorder, fake_repos, market=FakeMarketAgent()))
    state = {
        "symbol": "AAPL", "date_range": {"start_date": "2023-01-01", "end_date": "2023-12-31"},
        "market_fetch_status": "ok", "market_sources": ["src"],
        "market_data": {"observations": [
            {"date": "2023-01-03", "close": 100.0, "adjusted_close": 100.0},
            {"date": "2023-02-01", "close": 110.0, "adjusted_close": 110.0},
        ], "data_as_of": "2023-02-01"},
    }
    out = await nodes.market_analysis(state, CONFIG)
    assert out["market_status"] == "ok"
    assert out["market_analysis"]["findings"] == {"summary": "ok"}
    assert "metrics" in out["market_analysis"]


async def test_writer_persists_report_with_mock_agent(settings, fake_recorder, fake_repos):
    class FakeWriter:
        async def write(self, **_k):
            return SimpleNamespace(
                parsed={"report_markdown": "# Report", "risks": ["r"],
                        "limitations": ["l"], "sources": ["s"]},
                text="# Report",
            )

    nodes = Nodes(_deps(settings, fake_recorder, fake_repos, writer=FakeWriter()))
    state = {
        "symbol": "AAPL",
        "market_analysis": {"findings": {"summary": "m"}},
        "research_analysis": {"findings": {"summary": "r"}},
        "market_sources": ["ms"], "financial_sources": ["fs"], "data_mode": "fixture",
    }
    out = await nodes.writer(state, CONFIG)
    assert out["result_status"] == "completed"
    assert out["final_report"]["report_markdown"] == "# Report"
    assert out["final_report"]["version"] == 1
    assert len(fake_repos.reports) == 1  # persisted exactly once


async def test_writer_fault_injection_fires_on_initial(settings, fake_recorder, fake_repos):
    nodes = Nodes(_deps(settings, fake_recorder, fake_repos))
    cfg = {"configurable": {"run_id": "r1", "trace_id": "t", "execution_attempt_id": None,
                            "attempt_kind": "initial", "fail_at": "writer"}}
    with pytest.raises(RuntimeError):
        await nodes.writer({"symbol": "AAPL"}, cfg)


async def test_writer_fault_injection_skipped_on_resume(settings, fake_recorder, fake_repos):
    class FakeWriter:
        async def write(self, **_k):
            return SimpleNamespace(parsed={"report_markdown": "# R", "sources": []},
                                   text="# R")

    nodes = Nodes(_deps(settings, fake_recorder, fake_repos, writer=FakeWriter()))
    cfg = {"configurable": {"run_id": "r1", "trace_id": "t", "execution_attempt_id": None,
                            "attempt_kind": "resume", "fail_at": "writer"}}
    state = {"symbol": "AAPL",
             "market_analysis": {"findings": {}}, "research_analysis": {"findings": {}},
             "market_sources": [], "financial_sources": [], "data_mode": "fixture"}
    out = await nodes.writer(state, cfg)  # resume: fault NOT injected
    assert out["result_status"] == "completed"
