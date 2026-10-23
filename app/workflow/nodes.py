"""Workflow nodes.

Retrieval, validation, routing and failure reporting are ordinary code nodes;
only the three analyst/writer steps call an LLM. Splitting retrieval from analysis
means a successful retrieval is checkpointed before any later model call can fail,
so a resume re-runs only the failed step.

Each node returns *partial* state updates and owns disjoint keys. Nodes read the
current execution's ids (trace/run/attempt), the attempt kind, and the optional
fault-injection target from the runtime config — not from checkpointed state.
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from app.metrics import compute_financial_metrics, compute_stock_metrics
from app.observability.tracing import new_span_id
from app.workflow.deps import WorkflowDeps


def _cfg(config: dict | None) -> dict:
    return (config or {}).get("configurable", {})


def _maybe_fail(cfg: dict, node_name: str) -> None:
    """Injected failure for the recovery demo — fires only on the INITIAL attempt
    so a resume of the same node succeeds."""
    if cfg.get("fail_at") == node_name and cfg.get("attempt_kind") == "initial":
        raise RuntimeError(
            f"Injected failure at node '{node_name}' (demo: initial attempt only)"
        )


class Nodes:
    def __init__(self, deps: WorkflowDeps) -> None:
        self.d = deps

    # ------------------------------------------------------------- helpers
    async def _node_span(self, cfg: dict, name: str, kind: str = "node"):
        if cfg.get("execution_attempt_id"):
            await self.d.repos.heartbeat_attempt(cfg["execution_attempt_id"])
        return self.d.recorder.span(
            span_kind=kind,
            trace_id=cfg.get("trace_id"),
            run_id=cfg.get("run_id"),
            execution_attempt_id=cfg.get("execution_attempt_id"),
            node_name=name,
            logical_step_id=name,
            operation_name=f"node.{name}",
        )

    # ------------------------------------------------------------- ingest
    async def ingest(self, state: dict, config: RunnableConfig) -> dict:
        cfg = _cfg(config)
        async with await self._node_span(cfg, "ingest") as sp:
            symbol = state.get("symbol")
            date_range = state.get("date_range") or {}
            if not symbol or not date_range.get("start_date") or not date_range.get("end_date"):
                raise ValueError("input validation failed: symbol and date_range required")
            sp.output_payload = {"symbol": symbol, "validated": True}
            await self.d.recorder.event(
                event_type="input_validated", run_id=cfg.get("run_id"),
                execution_attempt_id=cfg.get("execution_attempt_id"), span_id=sp.span_id,
                message=f"Input validated for {symbol}",
            )
        return {}

    # --------------------------------------------------- retrieval branches
    async def market_fetch(self, state: dict, config: RunnableConfig) -> dict:
        cfg = _cfg(config)
        async with await self._node_span(cfg, "market_fetch") as sp:
            _maybe_fail(cfg, "market_fetch")
            dr = state["date_range"]
            data = await self.d.mcp_client.call_tool(
                "get_stock_data",
                {"symbol": state["symbol"], "start_date": dr["start_date"],
                 "end_date": dr["end_date"]},
                node_span=sp, trace_id=cfg.get("trace_id"), run_id=cfg.get("run_id"),
                execution_attempt_id=cfg.get("execution_attempt_id"),
            )
            status = data.get("status", "ok")
            sp.output_payload = {"fetch_status": status}
            if status == "ok":
                return {"market_data": data, "market_fetch_status": "ok",
                        "market_sources": data.get("source_references", [])}
            err = (data.get("error") or {}).get("message") or f"status={status}"
            return {"market_data": data,
                    "market_fetch_status": "no_data" if status == "no_data" else "failed",
                    "market_fetch_error": err,
                    "market_sources": data.get("source_references", [])}

    async def research_fetch(self, state: dict, config: RunnableConfig) -> dict:
        cfg = _cfg(config)
        async with await self._node_span(cfg, "research_fetch") as sp:
            _maybe_fail(cfg, "research_fetch")
            data = await self.d.mcp_client.call_tool(
                "get_company_financials",
                {"symbol": state["symbol"],
                 "reporting_period": state.get("reporting_period", "latest"),
                 "period_type": state.get("period_type", "annual")},
                node_span=sp, trace_id=cfg.get("trace_id"), run_id=cfg.get("run_id"),
                execution_attempt_id=cfg.get("execution_attempt_id"),
            )
            status = data.get("status", "ok")
            sp.output_payload = {"fetch_status": status}
            if status == "ok":
                return {"financial_data": data, "financial_fetch_status": "ok",
                        "financial_sources": data.get("sources", [])}
            err = (data.get("error") or {}).get("message") or f"status={status}"
            return {"financial_data": data,
                    "financial_fetch_status": "no_data" if status == "no_data" else "failed",
                    "financial_fetch_error": err,
                    "financial_sources": data.get("sources", [])}

    # ----------------------------------------------------- analysis branches
    async def market_analysis(self, state: dict, config: RunnableConfig) -> dict:
        cfg = _cfg(config)
        async with await self._node_span(cfg, "market_analysis") as sp:
            sp.agent_role = "market_analyst"
            if state.get("market_fetch_status") != "ok":
                reason = state.get("market_fetch_error") or state.get("market_fetch_status")
                sp.output_payload = {"skipped": True, "reason": reason}
                return {"market_status": "failed",
                        "market_error": f"retrieval unavailable: {reason}"}
            metrics = compute_stock_metrics(state["market_data"])
            out = await self.d.market_agent.analyze(
                symbol=state["symbol"], date_range=state["date_range"], metrics=metrics,
                notes=state["market_data"].get("notes"),
                sources=state.get("market_sources", []),
                node_span=sp, trace_id=cfg.get("trace_id"), run_id=cfg.get("run_id"),
                execution_attempt_id=cfg.get("execution_attempt_id"),
            )
            sp.output_payload = {"ok": True}
            return {"market_analysis": {"metrics": metrics, "findings": out.parsed,
                                        "raw_text": out.text},
                    "market_status": "ok"}

    async def research_analysis(self, state: dict, config: RunnableConfig) -> dict:
        cfg = _cfg(config)
        async with await self._node_span(cfg, "research_analysis") as sp:
            sp.agent_role = "research_analyst"
            if state.get("financial_fetch_status") != "ok":
                reason = state.get("financial_fetch_error") or state.get("financial_fetch_status")
                sp.output_payload = {"skipped": True, "reason": reason}
                return {"research_status": "failed",
                        "research_error": f"retrieval unavailable: {reason}"}
            metrics = compute_financial_metrics(state["financial_data"])
            out = await self.d.research_agent.analyze(
                symbol=state["symbol"], metrics=metrics,
                notes=state["financial_data"].get("notes"),
                sources=state.get("financial_sources", []),
                node_span=sp, trace_id=cfg.get("trace_id"), run_id=cfg.get("run_id"),
                execution_attempt_id=cfg.get("execution_attempt_id"),
            )
            sp.output_payload = {"ok": True}
            return {"research_analysis": {"metrics": metrics, "findings": out.parsed,
                                          "raw_text": out.text},
                    "research_status": "ok"}

    # --------------------------------------------------------- validation
    async def validate(self, state: dict, config: RunnableConfig) -> dict:
        cfg = _cfg(config)
        async with await self._node_span(cfg, "validate", kind="validation") as sp:
            issues: list[str] = []
            if state.get("market_status") != "ok":
                issues.append(f"market analysis unavailable: {state.get('market_error')}")
            if state.get("research_status") != "ok":
                issues.append(f"research analysis unavailable: {state.get('research_error')}")

            # Advisory checks (do not by themselves block, but are surfaced).
            fin = state.get("financial_data") or {}
            if state.get("research_status") == "ok":
                if not fin.get("units"):
                    issues.append("financial units missing")
                if not state.get("financial_sources"):
                    issues.append("financial sources missing")
                fy = fin.get("fiscal_year")
                dr = state.get("date_range") or {}
                try:
                    start_year = int(dr.get("start_date", "0-0-0")[:4])
                    end_year = int(dr.get("end_date", "0-0-0")[:4])
                    if fy and not (start_year - 1 <= fy <= end_year + 1):
                        issues.append(
                            f"period alignment note: financial FY{fy} is outside the "
                            f"requested price window {start_year}-{end_year}"
                        )
                except (ValueError, TypeError):
                    pass

            data_ready = (
                state.get("market_status") == "ok"
                and state.get("research_status") == "ok"
            )
            route = "writer" if data_ready else "incomplete"
            sp.output_payload = {"data_ready": data_ready, "route": route, "issues": issues}
            await self.d.recorder.event(
                event_type="route_decision", run_id=cfg.get("run_id"),
                execution_attempt_id=cfg.get("execution_attempt_id"), span_id=sp.span_id,
                message=f"route={route} data_ready={data_ready}",
                details={"issues": issues},
            )
            return {"data_ready": data_ready, "validation_issues": issues}

    def route(self, state: dict) -> str:
        """Deterministic routing (plain application logic, not another LLM)."""
        return "writer" if state.get("data_ready") else "incomplete"

    # ------------------------------------------------------------- writer
    async def writer(self, state: dict, config: RunnableConfig) -> dict:
        cfg = _cfg(config)
        async with await self._node_span(cfg, "writer") as sp:
            sp.agent_role = "report_writer"
            _maybe_fail(cfg, "writer")
            out = await self.d.writer_agent.write(
                symbol=state["symbol"],
                market_analysis=state["market_analysis"]["findings"],
                research_analysis=state["research_analysis"]["findings"],
                market_sources=state.get("market_sources", []),
                financial_sources=state.get("financial_sources", []),
                data_mode=state.get("data_mode", "fixture"),
                node_span=sp, trace_id=cfg.get("trace_id"), run_id=cfg.get("run_id"),
                execution_attempt_id=cfg.get("execution_attempt_id"),
                max_tokens=self.d.settings.claude_max_tokens * 2,
            )
            parsed = out.parsed
            content = parsed.get("report_markdown") or out.text
            sources = parsed.get("sources") or (
                list(state.get("market_sources", [])) + list(state.get("financial_sources", []))
            )
            report = {
                "report_markdown": content,
                "risks": parsed.get("risks", []),
                "limitations": parsed.get("limitations", []),
                "sources": sources,
            }
            report_id = new_span_id()
            if cfg.get("run_id"):
                version = await self.d.repos.next_report_version(cfg["run_id"])
                await self.d.repos.insert_report(
                    report_id=report_id, run_id=cfg["run_id"], version=version,
                    content=content, sources={"sources": sources,
                                              "risks": report["risks"],
                                              "limitations": report["limitations"]},
                    writer_span_id=sp.span_id,
                )
                report["report_id"] = report_id
                report["version"] = version
            sp.output_payload = {"report_id": report_id, "chars": len(content)}
            return {"final_report": report, "result_status": "completed"}

    # --------------------------------------------------------- incomplete
    async def incomplete(self, state: dict, config: RunnableConfig) -> dict:
        cfg = _cfg(config)
        async with await self._node_span(cfg, "incomplete") as sp:
            issues = state.get("validation_issues", [])
            lines = [
                f"# Incomplete analysis for {state.get('symbol')}",
                "",
                "The workflow could not produce a full report because the evidence "
                "was insufficient. No findings were fabricated.",
                "",
                "## Unresolved issues",
            ]
            lines += [f"- {i}" for i in issues] or ["- (none recorded)"]
            if state.get("market_fetch_status") not in (None, "ok"):
                lines.append(f"- market retrieval: {state.get('market_fetch_error')}")
            if state.get("financial_fetch_status") not in (None, "ok"):
                lines.append(f"- financial retrieval: {state.get('financial_fetch_error')}")
            content = "\n".join(lines)
            sources = list(state.get("market_sources", [])) + list(
                state.get("financial_sources", [])
            )
            report = {"report_markdown": content, "risks": [],
                      "limitations": issues, "sources": sources, "incomplete": True}
            report_id = new_span_id()
            if cfg.get("run_id"):
                version = await self.d.repos.next_report_version(cfg["run_id"])
                await self.d.repos.insert_report(
                    report_id=report_id, run_id=cfg["run_id"], version=version,
                    content=content, sources={"incomplete": True, "issues": issues},
                    writer_span_id=sp.span_id,
                )
                report["report_id"] = report_id
                report["version"] = version
            sp.output_payload = {"incomplete": True, "issues": len(issues)}
            return {"final_report": report, "result_status": "incomplete"}
