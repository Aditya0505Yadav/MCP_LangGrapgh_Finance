"""Command-line interface — the simplest way to drive and inspect runs.

Examples:
    python -m app.cli analyze --symbol AAPL --start 2023-01-01 --end 2023-12-31
    python -m app.cli resume  --run-id <id>
    python -m app.cli status  --run-id <id>
    python -m app.cli report  --run-id <id>
    python -m app.cli trace   --run-id <id>
    python -m app.cli list
    python -m app.cli verify-tools
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.eventloop import use_selector_event_loop_on_windows
from app.runtime import Runtime
from app.service import ConcurrencyError, NotResumableError
from app.views import _to_jsonable, build_run_detail


def _print(obj) -> None:
    print(json.dumps(_to_jsonable(obj), indent=2, default=str))


async def _run(args) -> int:
    rt = await Runtime().startup()
    try:
        svc = rt.service
        if args.command == "verify-tools":
            _print({"tools": await svc.verify_tools()})

        elif args.command == "analyze":
            created = await svc.create_run(
                symbol=args.symbol, start_date=args.start, end_date=args.end,
                reporting_period=args.period, period_type=args.period_type,
                data_mode=args.data_mode,
            )
            print(f"Created run {created['run_id']} (thread {created['thread_id']})",
                  file=sys.stderr)
            result = await svc.execute_run(created["run_id"])
            _print({**created, **result})
            report = result.get("final_report") or {}
            if report.get("report_markdown"):
                print("\n===== REPORT =====\n", file=sys.stderr)
                print(report["report_markdown"])

        elif args.command == "resume":
            try:
                _print(await svc.resume_run(args.run_id))
            except (NotResumableError, ConcurrencyError) as exc:
                print(f"Cannot resume: {exc}", file=sys.stderr)
                return 2

        elif args.command == "status":
            _print(await svc.get_status(args.run_id))

        elif args.command == "report":
            report = await rt.repos.get_latest_report(args.run_id)
            if report is None:
                print("No report yet.", file=sys.stderr)
                return 1
            if args.json:
                _print({"version": report["version"], "content": report["content"],
                        "sources": report.get("sources")})
            else:
                print(report["content"])

        elif args.command == "trace":
            _print(await build_run_detail(rt.repos, rt.settings, args.run_id))

        elif args.command == "list":
            _print(await rt.repos.list_runs(args.limit))

        return 0
    finally:
        await rt.shutdown()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="finance-cli", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("analyze", help="create and execute a run")
    a.add_argument("--symbol", required=True)
    a.add_argument("--start", required=True, help="start date YYYY-MM-DD")
    a.add_argument("--end", required=True, help="end date YYYY-MM-DD")
    a.add_argument("--period", default="latest", help="reporting period, e.g. FY2023")
    a.add_argument("--period-type", default="annual", choices=["annual", "quarterly"])
    a.add_argument("--data-mode", default=None, choices=["fixture", "live"])

    r = sub.add_parser("resume", help="resume a failed/interrupted run")
    r.add_argument("--run-id", required=True)

    s = sub.add_parser("status", help="run + branch status")
    s.add_argument("--run-id", required=True)

    rep = sub.add_parser("report", help="latest report")
    rep.add_argument("--run-id", required=True)
    rep.add_argument("--json", action="store_true")

    t = sub.add_parser("trace", help="trace tree, timing and cost")
    t.add_argument("--run-id", required=True)

    ls = sub.add_parser("list", help="recent runs")
    ls.add_argument("--limit", type=int, default=25)

    sub.add_parser("verify-tools", help="verify MCP server advertises both tools")
    return p


def main() -> None:
    use_selector_event_loop_on_windows()
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
