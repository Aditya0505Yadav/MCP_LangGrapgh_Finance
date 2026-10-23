"""Read-side view builders: trace tree, attempt timeline, token/cost summary and
timing — assembled from the observability tables for the CLI, API and HTML view.

Timing rules (deliberately not naive):
* total_elapsed_ms = first start -> last finish (includes downtime between attempts).
* active_run_ms    = SUM of execution-attempt durations (excludes downtime).
* Parallel branch durations are NOT added for end-to-end latency, and nested span
  durations are NOT summed (that would double-count). Timeline rows carry each
  span's own start/finish so parallel analyst bars overlap.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings
from app.db.repositories import Repositories


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def localize(dt: datetime | None, tz: str) -> dict[str, Any] | None:
    if dt is None:
        return None
    try:
        local = dt.astimezone(ZoneInfo(tz))
        local_iso = local.isoformat()
    except Exception:
        local_iso = None
    return {"utc": dt.isoformat(), "local": local_iso, "timezone": tz}


def build_trace_tree(spans: list[dict]) -> list[dict]:
    by_id = {s["span_id"]: s for s in spans}
    children: dict[str | None, list[dict]] = {}
    for s in spans:
        parent = s.get("parent_span_id")
        if parent not in by_id:
            parent = None  # root (or cross-process parent not present)
        children.setdefault(parent, []).append(s)

    def node(s: dict) -> dict:
        return {
            "span_id": s["span_id"],
            "span_kind": s["span_kind"],
            "service_name": s["service_name"],
            "node_name": s.get("node_name"),
            "operation_name": s.get("operation_name"),
            "tool_name": s.get("tool_name"),
            "agent_role": s.get("agent_role"),
            "status": s["status"],
            "attempt_number": s.get("attempt_number"),
            "duration_ms": s.get("duration_ms"),
            "started_at": s["started_at"],
            "finished_at": s.get("finished_at"),
            "error_code": s.get("error_code"),
            "children": [node(c) for c in sorted(
                children.get(s["span_id"], []), key=lambda x: x["started_at"]
            )],
        }

    roots = sorted(children.get(None, []), key=lambda x: x["started_at"])
    return [node(r) for r in roots]


def build_timeline(spans: list[dict]) -> list[dict]:
    """Flat rows for a Gantt-style view; analyst bars overlap in time."""
    rows = []
    for s in spans:
        rows.append({
            "label": s.get("node_name") or s.get("operation_name") or s["span_kind"],
            "span_kind": s["span_kind"],
            "service_name": s["service_name"],
            "attempt_number": s.get("attempt_number"),
            "started_at": s["started_at"],
            "finished_at": s.get("finished_at"),
            "duration_ms": s.get("duration_ms"),
            "status": s["status"],
        })
    return sorted(rows, key=lambda r: r["started_at"])


def compute_timing(run: dict, attempts: list[dict]) -> dict[str, Any]:
    first = run.get("first_started_at")
    last = run.get("last_finished_at")
    total_elapsed_ms = (
        int((last - first).total_seconds() * 1000) if first and last else None
    )
    active_run_ms = sum(a.get("duration_ms") or 0 for a in attempts) or None
    return {
        "total_elapsed_ms": total_elapsed_ms,
        "active_run_ms": active_run_ms,
        "attempt_count": len(attempts),
        "note": "total_elapsed includes downtime; active_run sums attempt durations; "
                "parallel/nested span durations are not summed.",
    }


async def build_cost(repos: Repositories, run_id: str) -> dict[str, Any]:
    summary = await repos.get_cost_summary(run_id)
    calls = await repos.get_llm_calls_for_run(run_id)
    call_rows = [
        {
            "requested_model": c["requested_model"],
            "returned_model": c.get("returned_model"),
            "prompt_version": c["prompt_version"],
            "input_tokens": c.get("input_tokens"),
            "output_tokens": c.get("output_tokens"),
            "cache_read_tokens": c.get("cache_read_tokens"),
            "usage_status": c.get("usage_status"),
            "estimated_cost_usd": c.get("estimated_cost_usd"),
            "stop_reason": c.get("stop_reason"),
        }
        for c in calls
    ]
    return {
        "llm_call_count": summary["call_count"],
        "tokens": {
            "input": summary["input_tokens"],
            "output": summary["output_tokens"],
            "cache_read": summary["cache_read_tokens"],
            "cache_write_short_ttl": summary["cache_write_short"],
            "cache_write_long_ttl": summary["cache_write_long"],
        },
        "known_estimated_cost_usd": summary["known_cost_usd"],
        "unknown_cost_call_count": summary["unknown_cost_calls"],
        "note": "Estimate only — not an authoritative invoice. Each actual call is "
                "counted once; unknown-cost calls are not shown as zero. LLM cost is "
                "separate from any MCP/provider or infrastructure charges.",
        "calls": call_rows,
    }


async def build_run_detail(
    repos: Repositories, settings: Settings, run_id: str
) -> dict[str, Any]:
    run = await repos.get_run(run_id)
    if run is None:
        raise ValueError(f"unknown run_id {run_id}")
    tz = settings.display_timezone
    attempts = await repos.get_attempts(run_id)
    spans = await repos.get_spans_for_run(run_id)
    events = await repos.get_events(run_id)
    report = await repos.get_latest_report(run_id)
    cost = await build_cost(repos, run_id)

    detail = {
        "run": {
            "run_id": run["run_id"],
            "symbol": run["symbol"],
            "status": run["status"],
            "data_mode": run["data_mode"],
            "workflow_version": run["workflow_version"],
            "code_version": run["code_version"],
            "trace_id": run["trace_id"],
            "created_at": localize(run["created_at"], tz),
            "first_started_at": localize(run.get("first_started_at"), tz),
            "last_finished_at": localize(run.get("last_finished_at"), tz),
            "error_summary": run.get("error_summary"),
        },
        "attempts": [
            {
                "attempt_number": a["attempt_number"],
                "kind": a["kind"],
                "status": a["status"],
                "duration_ms": a.get("duration_ms"),
                "started_at": localize(a.get("started_at"), tz),
                "finished_at": localize(a.get("finished_at"), tz),
                "error_code": a.get("error_code"),
            }
            for a in attempts
        ],
        "timing": compute_timing(run, attempts),
        "trace_tree": build_trace_tree(spans),
        "timeline": build_timeline(spans),
        "events": [
            {"event_time": localize(e["event_time"], tz), "event_type": e["event_type"],
             "severity": e["severity"], "message": e.get("message")}
            for e in events
        ],
        "cost": cost,
        "report": {
            "version": report["version"],
            "content": report["content"],
            "sources": report.get("sources"),
        } if report else None,
    }
    return _to_jsonable(detail)
