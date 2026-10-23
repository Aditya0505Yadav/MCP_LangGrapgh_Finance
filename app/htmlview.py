"""Minimal HTML run-detail view (intentionally lightweight).

Renders the run summary, attempt timeline (parallel analyst bars overlap), token
and cost totals, and the report. No framework, no build step.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any


def _parse(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _timeline_bars(timeline: list[dict]) -> str:
    starts, ends = [], []
    for r in timeline:
        s, e = _parse(r.get("started_at")), _parse(r.get("finished_at"))
        if s:
            starts.append(s)
        if e:
            ends.append(e)
    if not starts or not ends:
        return "<p>No timeline data.</p>"
    t0, t1 = min(starts), max(ends)
    span = (t1 - t0).total_seconds() or 1.0

    colors = {
        "node": "#4e79a7", "mcp_client": "#59a14f", "mcp_server": "#8cd17d",
        "provider_http": "#b6992d", "llm": "#e15759", "validation": "#76b7b2",
        "root": "#555", "route": "#9c755f", "persist": "#bab0ac", "recovery": "#ff9da7",
    }
    rows = []
    for r in timeline:
        s, e = _parse(r.get("started_at")), _parse(r.get("finished_at"))
        if not s:
            continue
        e = e or s
        left = (s - t0).total_seconds() / span * 100.0
        width = max(0.8, (e - s).total_seconds() / span * 100.0)
        color = colors.get(r["span_kind"], "#888")
        label = html.escape(str(r.get("label")))
        dur = r.get("duration_ms")
        rows.append(
            f'<div class="row"><div class="lbl">{label} '
            f'<span class="k">[{html.escape(r["span_kind"])}]</span></div>'
            f'<div class="track"><div class="bar" style="left:{left:.1f}%;'
            f'width:{width:.1f}%;background:{color}" '
            f'title="{dur} ms">{dur if dur is not None else ""}</div></div></div>'
        )
    return "\n".join(rows)


def render_run_html(detail: dict) -> str:
    run = detail["run"]
    timing = detail["timing"]
    cost = detail["cost"]
    report = detail.get("report")
    tokens = cost["tokens"]

    attempts_rows = "".join(
        f"<tr><td>{a['attempt_number']}</td><td>{html.escape(a['kind'])}</td>"
        f"<td>{html.escape(a['status'])}</td><td>{a.get('duration_ms')}</td>"
        f"<td>{html.escape((a.get('error_code') or ''))}</td></tr>"
        for a in detail["attempts"]
    )
    report_html = (
        f"<pre class='report'>{html.escape(report['content'])}</pre>"
        if report else "<p>No report yet.</p>"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Run {html.escape(run['run_id'][:8])} — {html.escape(run['symbol'])}</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 1.5rem; color:#1a1a1a; }}
  h1 {{ font-size: 1.3rem; }} h2 {{ font-size: 1.05rem; margin-top:1.5rem; }}
  table {{ border-collapse: collapse; }} td,th {{ border:1px solid #ddd; padding:4px 8px; font-size:.9rem; }}
  .pill {{ padding:2px 8px; border-radius:10px; background:#eee; font-size:.85rem; }}
  .row {{ display:flex; align-items:center; margin:3px 0; }}
  .lbl {{ width:230px; font-size:.8rem; }} .k {{ color:#888; }}
  .track {{ position:relative; flex:1; height:18px; background:#f4f4f4; border-radius:3px; }}
  .bar {{ position:absolute; height:18px; border-radius:3px; color:#fff; font-size:.65rem;
          text-align:center; line-height:18px; overflow:hidden; }}
  pre.report {{ background:#fafafa; border:1px solid #eee; padding:1rem; white-space:pre-wrap; }}
  .grid {{ display:flex; gap:2rem; flex-wrap:wrap; }}
  .card {{ border:1px solid #eee; border-radius:8px; padding:.75rem 1rem; }}
</style></head><body>
<h1>{html.escape(run['symbol'])} — run {html.escape(run['run_id'][:8])}
  <span class="pill">{html.escape(run['status'])}</span>
  <span class="pill">{html.escape(run['data_mode'])}</span></h1>
<div class="grid">
  <div class="card"><b>Timing</b><br>total elapsed: {timing['total_elapsed_ms']} ms<br>
    active: {timing['active_run_ms']} ms<br>attempts: {timing['attempt_count']}</div>
  <div class="card"><b>Tokens</b><br>in: {tokens['input']} · out: {tokens['output']}<br>
    cache read: {tokens['cache_read']}</div>
  <div class="card"><b>Estimated cost</b><br>
    ${cost['known_estimated_cost_usd']} (known)<br>
    unknown-cost calls: {cost['unknown_cost_call_count']}<br>
    LLM calls: {cost['llm_call_count']}</div>
</div>
<p style="color:#777;font-size:.8rem">{html.escape(cost['note'])}</p>

<h2>Attempts</h2>
<table><tr><th>#</th><th>kind</th><th>status</th><th>ms</th><th>error</th></tr>
{attempts_rows}</table>

<h2>Timeline (UTC-ordered; parallel bars overlap)</h2>
{_timeline_bars(detail['timeline'])}

<h2>Report</h2>
{report_html}
</body></html>"""
