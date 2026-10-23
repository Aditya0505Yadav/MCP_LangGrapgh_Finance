"""AnalysisService — create, execute, resume and inspect analysis runs.

* A per-thread execution lock (in-process asyncio.Lock) plus a DB heartbeat guard
  refuses concurrent execution/resume of the same thread.
* Resume recovers a failed/interrupted thread from the PostgreSQL checkpointer by
  invoking the graph with ``None`` input — checkpointed retrieval/analysis is NOT
  re-run. The original request is never re-injected as a new run.
* Each initial/resume invocation gets its own ``execution_attempt_id``.
* A terminal ``incomplete`` result is NOT a resumable failed run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from app.config import Settings
from app.db.repositories import Repositories, new_uuid
from app.mcp.client import FinanceMCPClient
from app.observability.tracing import TraceRecorder, new_trace_id

logger = logging.getLogger(__name__)

RESUMABLE_STATUSES = {"failed", "interrupted"}
HEARTBEAT_STALE_SECONDS = 60


class ConcurrencyError(RuntimeError):
    """Raised when a thread is already executing/resuming."""


class NotResumableError(RuntimeError):
    """Raised when a run is not in a resumable state."""


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class AnalysisService:
    def __init__(
        self,
        *,
        settings: Settings,
        repos: Repositories,
        recorder: TraceRecorder,
        graph: Any,
        mcp_client: FinanceMCPClient,
    ) -> None:
        self.settings = settings
        self.repos = repos
        self.recorder = recorder
        self.graph = graph
        self.mcp_client = mcp_client
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, thread_id: str) -> asyncio.Lock:
        return self._locks.setdefault(thread_id, asyncio.Lock())

    # ----------------------------------------------------------- create
    async def create_run(
        self,
        *,
        symbol: str,
        start_date: str,
        end_date: str,
        reporting_period: str = "latest",
        period_type: str = "annual",
        data_mode: str | None = None,
    ) -> dict[str, str]:
        run_id = new_uuid()
        thread_id = str(uuid.uuid4())
        trace_id = new_trace_id()
        mode = data_mode or self.settings.data_mode
        sanitized_input = {
            "symbol": symbol, "start_date": start_date, "end_date": end_date,
            "reporting_period": reporting_period, "period_type": period_type,
            "data_mode": mode,
        }
        await self.repos.create_run(
            run_id=run_id, thread_id=thread_id, trace_id=trace_id, symbol=symbol,
            sanitized_input=sanitized_input, data_mode=mode,
            workflow_version=self.settings.workflow_version,
            code_version=self.settings.code_version,
        )
        logger.info("Created run %s (thread %s) for %s", run_id, thread_id, symbol)
        return {"run_id": run_id, "thread_id": thread_id, "trace_id": trace_id}

    def _config(self, run: dict, execution_attempt_id: str, attempt_kind: str) -> dict:
        fail_at = self.settings.fail_at_normalized if attempt_kind == "initial" else None
        return {
            "configurable": {
                "thread_id": str(run["thread_id"]),
                "run_id": str(run["run_id"]),
                "trace_id": str(run["trace_id"]),
                "execution_attempt_id": execution_attempt_id,
                "attempt_kind": attempt_kind,
                "fail_at": fail_at,
            }
        }

    def _initial_state(self, run: dict) -> dict:
        si = run["sanitized_input"]
        return {
            "run_id": str(run["run_id"]),
            "symbol": run["symbol"],
            "date_range": {"start_date": si["start_date"], "end_date": si["end_date"]},
            "reporting_period": si.get("reporting_period", "latest"),
            "period_type": si.get("period_type", "annual"),
            "data_mode": run["data_mode"],
            "workflow_version": run["workflow_version"],
        }

    async def _guard_not_running(self, run_id: str) -> None:
        attempts = await self.repos.get_attempts(run_id)
        if not attempts:
            return
        last = attempts[-1]
        if last["status"] != "running":
            return
        hb = last.get("heartbeat_at")
        if hb is not None:
            age = (datetime.now(timezone.utc) - hb).total_seconds()
            if age < HEARTBEAT_STALE_SECONDS:
                raise ConcurrencyError(
                    f"run {run_id} has a live attempt (heartbeat {age:.0f}s ago)"
                )

    async def _invoke(
        self, run: dict, graph_input: Any, attempt_kind: str
    ) -> dict[str, Any]:
        run_id = run["run_id"]
        attempt_number = await self.repos.next_attempt_number(run_id)
        attempt_id = new_uuid()
        await self.repos.create_attempt(
            execution_attempt_id=attempt_id, run_id=run_id,
            attempt_number=attempt_number, kind=attempt_kind, worker_id=_worker_id(),
        )
        if attempt_kind == "resume":
            n = await self.repos.reconcile_running_spans(run_id, attempt_id)
            await self.recorder.event(
                event_type="resume_requested", run_id=run_id,
                execution_attempt_id=attempt_id,
                message=f"resuming; reconciled {n} orphaned span(s)",
            )
        await self.repos.mark_run_started(run_id)

        config = self._config(run, attempt_id, attempt_kind)
        started = perf_counter()
        try:
            result = await self.graph.ainvoke(graph_input, config)
        except BaseException as exc:  # noqa: BLE001 — record then re-raise
            duration = int((perf_counter() - started) * 1000)
            await self.repos.finish_attempt(
                execution_attempt_id=attempt_id, status="failed", duration_ms=duration,
                error_code=type(exc).__name__, error_message=str(exc)[:2000],
            )
            await self.repos.update_run_status(
                run_id, "failed", error_summary=f"{type(exc).__name__}: {exc}"[:2000],
                finished=True,
            )
            await self.recorder.event(
                event_type="run_failed", severity="error", run_id=run_id,
                execution_attempt_id=attempt_id, message=str(exc)[:500],
            )
            logger.exception("Run %s attempt %s failed", run_id, attempt_number)
            raise

        duration = int((perf_counter() - started) * 1000)
        result_status = result.get("result_status", "incomplete")
        run_status = "completed" if result_status == "completed" else "incomplete"
        final_report = result.get("final_report") or {}
        await self.repos.finish_attempt(
            execution_attempt_id=attempt_id, status=run_status, duration_ms=duration,
        )
        await self.repos.update_run_status(
            run_id, run_status, final_report_id=final_report.get("report_id"),
            finished=True,
        )
        await self.recorder.event(
            event_type="run_finished", run_id=run_id, execution_attempt_id=attempt_id,
            message=f"status={run_status}",
        )
        return {"run_id": run_id, "status": run_status, "result_status": result_status,
                "final_report": final_report}

    # ----------------------------------------------------------- execute
    async def execute_run(self, run_id: str) -> dict[str, Any]:
        run = await self.repos.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run_id {run_id}")
        async with self._lock_for(run["thread_id"]):
            await self._guard_not_running(run_id)
            return await self._invoke(run, self._initial_state(run), "initial")

    # ----------------------------------------------------------- resume
    async def resume_run(self, run_id: str) -> dict[str, Any]:
        run = await self.repos.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run_id {run_id}")
        status = run["status"]
        if status not in RESUMABLE_STATUSES and status != "running":
            raise NotResumableError(
                f"run {run_id} is '{status}', not resumable. A terminal "
                f"'incomplete'/'completed' result is not a suspended failed run."
            )
        async with self._lock_for(run["thread_id"]):
            await self._guard_not_running(run_id)
            # Resume from the checkpoint: None input continues the saved thread.
            return await self._invoke(run, None, "resume")

    # ----------------------------------------------------------- status
    async def get_status(self, run_id: str) -> dict[str, Any]:
        run = await self.repos.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run_id {run_id}")
        attempts = await self.repos.get_attempts(run_id)
        branch: dict[str, Any] = {}
        try:
            snapshot = await self.graph.aget_state(
                {"configurable": {"thread_id": run["thread_id"]}}
            )
            values = snapshot.values if snapshot else {}
            branch = {
                "market_fetch_status": values.get("market_fetch_status"),
                "market_status": values.get("market_status"),
                "financial_fetch_status": values.get("financial_fetch_status"),
                "research_status": values.get("research_status"),
                "data_ready": values.get("data_ready"),
                "result_status": values.get("result_status"),
                "next_nodes": list(snapshot.next) if snapshot else [],
            }
        except Exception as exc:  # checkpoint may not exist yet
            branch = {"note": f"no checkpoint state: {exc}"}
        return {
            "run_id": run_id,
            "symbol": run["symbol"],
            "status": run["status"],
            "data_mode": run["data_mode"],
            "created_at": run["created_at"],
            "attempts": attempts,
            "branches": branch,
        }

    async def verify_tools(self) -> list[str]:
        return await self.mcp_client.verify_tools()
