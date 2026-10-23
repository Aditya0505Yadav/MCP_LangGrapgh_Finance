"""Server-rendered frontend: the entry form and the investment-report dashboard,
served by the application itself (standalone localhost pages, no external host).

``build_payload`` reads a run's durable result (run row + LangGraph checkpoint
state) into the shape the dashboard, the Word report and the API all use.
"""

from __future__ import annotations

import html
import json
from typing import Any

from app.config import Settings
from app.db.repositories import Repositories
from app.views import _to_jsonable, build_cost


async def build_payload(repos: Repositories, settings: Settings, graph: Any,
                        run_id: str) -> dict[str, Any] | None:
    run = await repos.get_run(run_id)
    if run is None:
        return None
    snap = await graph.aget_state(
        {"configurable": {"thread_id": str(run["thread_id"])}})
    st = snap.values if snap else {}
    md = st.get("market_data", {}) or {}
    fd = st.get("financial_data", {}) or {}
    ma = st.get("market_analysis", {}) or {}
    ra = st.get("research_analysis", {}) or {}
    fr = st.get("final_report", {}) or {}
    cost = _to_jsonable(await build_cost(repos, run_id))  # Decimal -> float for JSON

    cur, prev = md.get("current_price"), md.get("previous_close")
    day_change = (cur - prev) if (cur is not None and prev) else None
    day_pct = (day_change / prev * 100.0) if (day_change is not None and prev) else None

    model = settings.claude_model
    if cost.get("calls"):
        model = (cost["calls"][0].get("returned_model")
                 or cost["calls"][0].get("requested_model") or model)

    return {
        "run_id": str(run["run_id"]),
        "symbol": run["symbol"],
        "company_name": fd.get("company_name") or run["symbol"],
        "model": model,
        "status": run["status"],
        "data_mode": run["data_mode"],
        "as_of": md.get("market_time"),
        "quote": {
            "current_price": cur, "previous_close": prev,
            "day_change": round(day_change, 2) if day_change is not None else None,
            "day_change_pct": round(day_pct, 2) if day_pct is not None else None,
            "currency": md.get("currency"),
            "fifty_two_week_high": md.get("fifty_two_week_high"),
            "fifty_two_week_low": md.get("fifty_two_week_low"),
            "data_as_of": md.get("data_as_of"),
        },
        "fundamentals": {
            "revenue": fd.get("revenue"), "net_income": fd.get("net_income"),
            "total_debt": fd.get("total_debt"), "market_cap": fd.get("market_cap"),
            "pe_ratio": fd.get("pe_ratio"), "fiscal_year": fd.get("fiscal_year"),
            "period_end": fd.get("period_end"),
        },
        "observations": md.get("observations", []),
        "stock_metrics": ma.get("metrics", {}),
        "financial_metrics": ra.get("metrics", {}),
        "market_findings": ma.get("findings", {}),
        "research_findings": ra.get("findings", {}),
        "report_markdown": fr.get("report_markdown"),
        "risks": fr.get("risks", []),
        "limitations": fr.get("limitations", []),
        "sources": fr.get("sources", []),
        "cost": cost,
    }


# --------------------------------------------------------------------------- #
# Home / entry form
# --------------------------------------------------------------------------- #

def render_index(recent: list[dict], default_mode: str, default_model: str) -> str:
    rows = "".join(
        f'<li><a href="/analyses/{html.escape(str(r["run_id"]))}/dashboard">'
        f'{html.escape(r["symbol"])}</a> '
        f'<span class="muted">· {html.escape(r["status"])} · {html.escape(str(r["data_mode"]))}'
        f' · {r["created_at"]:%Y-%m-%d %H:%M}</span></li>'
        for r in recent
    ) or '<li class="muted">No analyses yet.</li>'
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Finance Analysis</title>
<style>
  :root{{--bg:#eef2f7;--surface:#fff;--ink:#1a2436;--muted:#5d6d7e;--line:#e3ebf0;
    --navy1:#1e3c72;--navy2:#2a5298;--accent:#2a5298}}
  @media (prefers-color-scheme:dark){{:root{{--bg:#0e141f;--surface:#161f2e;--ink:#e7edf5;
    --muted:#9fb0c3;--line:#26324a;--accent:#6ea0e0;color-scheme:dark}}}}
  *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);
    font-family:"Segoe UI",system-ui,Arial,sans-serif;line-height:1.5}}
  .wrap{{max-width:720px;margin:0 auto;padding:28px 18px}}
  .hero{{background:linear-gradient(135deg,var(--navy1),var(--navy2));color:#fff;
    border-radius:14px;padding:26px 24px;margin-bottom:20px}}
  .hero h1{{margin:0 0 6px;font-size:26px}} .hero p{{margin:0;opacity:.9;font-size:14px}}
  .card{{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:20px}}
  label{{display:block;font-size:12px;letter-spacing:.5px;text-transform:uppercase;
    color:var(--muted);margin:12px 0 4px}}
  input,select{{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;
    background:var(--bg);color:var(--ink);font-size:15px}}
  .rowf{{display:flex;gap:12px;flex-wrap:wrap}} .rowf>div{{flex:1;min-width:140px}}
  button{{margin-top:18px;width:100%;padding:12px;border:0;border-radius:8px;cursor:pointer;
    background:var(--accent);color:#fff;font-size:16px;font-weight:600}}
  button:disabled{{opacity:.6;cursor:progress}}
  details{{margin-top:8px}} summary{{cursor:pointer;color:var(--accent);font-size:14px}}
  .muted{{color:var(--muted);font-size:13px}} ul{{padding-left:18px}} li{{margin:6px 0}}
  h2{{font-size:14px;letter-spacing:.4px;text-transform:uppercase;color:var(--accent)}}
  #err{{color:#d14343;margin-top:10px;font-size:14px}}
  .spin{{display:none;margin-top:14px;color:var(--muted)}} .spin.on{{display:block}}
</style></head><body><div class="wrap">
  <div class="hero"><h1>Finance Analysis Assistant</h1>
    <p>Enter a stock symbol. A multi-agent workflow (MCP + LangGraph + Claude) pulls
       real-time data and writes an analysis — not financial advice.</p></div>
  <div class="card">
    <form id="f">
      <label for="symbol">Stock symbol</label>
      <input id="symbol" name="symbol" placeholder="e.g. MS, AAPL, MSFT, NVDA" required autofocus>
      <label for="period">Reporting period</label>
      <input id="period" name="period" value="latest">
      <p class="muted" style="margin-top:8px">Live (real-time) market data via Yahoo Finance.</p>
      <details><summary>Advanced: date range</summary>
        <div class="rowf">
          <div><label for="start">Start</label><input id="start" name="start" placeholder="YYYY-MM-DD"></div>
          <div><label for="end">End</label><input id="end" name="end" placeholder="YYYY-MM-DD"></div>
        </div>
      </details>
      <button id="go" type="submit">Analyze</button>
      <div id="err"></div>
      <div class="spin" id="spin">Running the agents and pulling live data… this takes ~20-40s.</div>
    </form>
  </div>
  <div class="card" style="margin-top:18px"><h2>Recent analyses</h2><ul>{rows}</ul>
    <p class="muted">Model: {html.escape(default_model)}</p></div>
</div>
<script>
const f=document.getElementById('f');
f.addEventListener('submit',async e=>{{
  e.preventDefault();
  const go=document.getElementById('go'), spin=document.getElementById('spin'), err=document.getElementById('err');
  err.textContent=''; go.disabled=true; spin.classList.add('on');
  const today=new Date().toISOString().slice(0,10);
  const d=new Date(); d.setMonth(d.getMonth()-3); const ago=d.toISOString().slice(0,10);
  const body={{
    symbol:document.getElementById('symbol').value.trim().toUpperCase(),
    start_date:document.getElementById('start').value||ago,
    end_date:document.getElementById('end').value||today,
    reporting_period:document.getElementById('period').value||'latest',
    data_mode:'live'
  }};
  try{{
    const r=await fetch('/analyses',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify(body)}});
    const j=await r.json();
    if(!r.ok){{throw new Error(j.detail||('HTTP '+r.status));}}
    window.location='/analyses/'+j.run_id+'/dashboard';
  }}catch(ex){{err.textContent='Error: '+ex.message; go.disabled=false; spin.classList.remove('on');}}
}});
</script></body></html>"""


# --------------------------------------------------------------------------- #
# Dashboard (full standalone page)
# --------------------------------------------------------------------------- #

def render_dashboard_html(payload: dict, run_id: str) -> str:
    data = {
        "symbol": payload["symbol"], "company": payload["company_name"],
        "model": payload.get("model"), "as_of": payload.get("as_of"),
        "data_mode": payload.get("data_mode"),
        "quote": payload["quote"], "fundamentals": payload["fundamentals"],
        "stock_metrics": payload["stock_metrics"],
        "financial_metrics": payload["financial_metrics"],
        "series": [{"d": o["date"], "c": o.get("adjusted_close") or o.get("close")}
                   for o in payload["observations"]],
        "market": payload["market_findings"], "research": payload["research_findings"],
        "risks": payload["risks"], "limitations": payload["limitations"],
        "sources": payload["sources"],
        "cost": {"usd": payload["cost"]["known_estimated_cost_usd"],
                 "calls": payload["cost"]["llm_call_count"]},
    }
    return (_DASHBOARD
            .replace("__TITLE__", html.escape(f"{payload['symbol']} Analysis"))
            .replace("__DATA__", json.dumps(data, default=str))
            .replace("__RUN__", html.escape(run_id)))


_DASHBOARD = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root{--bg:#eef2f7;--surface:#fff;--ink:#1a2436;--muted:#5d6d7e;--line:#e3ebf0;
    --navy1:#1e3c72;--navy2:#2a5298;--accent:#2a5298;--good:#1b9c6b;--bad:#d14343;
    --panel:#2c3e50;--panel-ink:#eaf0f6}
  @media (prefers-color-scheme:dark){:root{--bg:#0e141f;--surface:#161f2e;--ink:#e7edf5;
    --muted:#9fb0c3;--line:#26324a;--accent:#6ea0e0;--good:#43c08a;--bad:#ff6b6b;
    --panel:#101826;--panel-ink:#e7edf5;color-scheme:dark}}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);
    font-family:"Segoe UI",system-ui,Arial,sans-serif;line-height:1.55}
  .wrap{max-width:940px;margin:0 auto;padding:20px 18px}
  .tnum{font-variant-numeric:tabular-nums}
  .bar{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}
  .bar a{color:var(--accent);text-decoration:none;font-size:14px;font-weight:600}
  .hdr{background:linear-gradient(135deg,var(--navy1),var(--navy2));color:#fff;border-radius:14px;
    padding:22px 24px}
  .hdr .eyebrow{font-size:12px;letter-spacing:1.5px;text-transform:uppercase;opacity:.85}
  .hdr h1{margin:6px 0 2px;font-size:26px} .hdr .sub{font-size:13px;opacity:.9}
  .grid{display:grid;gap:14px;margin-top:16px} .kpis{grid-template-columns:repeat(4,1fr)}
  @media (max-width:640px){.kpis{grid-template-columns:repeat(2,1fr)}}
  .card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px}
  .kpi .label{font-size:11px;letter-spacing:.6px;text-transform:uppercase;color:var(--muted)}
  .kpi .value{font-size:22px;font-weight:650;margin-top:4px}
  .kpi .delta{font-size:13px;font-weight:600;margin-top:2px}
  .up{color:var(--good)} .down{color:var(--bad)}
  h2{font-size:15px;letter-spacing:.4px;text-transform:uppercase;color:var(--accent);margin:0 0 10px}
  .two{grid-template-columns:1fr 1fr} @media (max-width:720px){.two{grid-template-columns:1fr}}
  .chartbox{position:relative;height:300px}
  .stats{display:flex;flex-wrap:wrap;gap:16px;margin-top:12px;color:var(--muted);font-size:13px}
  .stats b{color:var(--ink)} ul{margin:0;padding-left:18px} li{margin:5px 0}
  .panel{background:var(--panel);color:var(--panel-ink);border-radius:12px;padding:16px}
  .foot{margin-top:18px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:14px}
  .disc{background:rgba(209,67,67,.08);border:1px solid rgba(209,67,67,.3);border-radius:10px;
    padding:10px 14px;font-size:12.5px;margin-top:14px}
  .dl{display:inline-block;background:var(--accent);color:#fff;padding:8px 14px;border-radius:8px;
    text-decoration:none;font-weight:600;font-size:14px}
</style></head><body><div class="wrap">
  <div class="bar"><a href="/">← New analysis</a>
    <a class="dl" href="/analyses/__RUN__/report.docx">⬇ Download Word report</a></div>
  <header class="hdr"><div class="eyebrow">AI-generated real-time analysis</div>
    <h1 id="h-title"></h1><div class="sub" id="h-sub"></div></header>
  <div class="grid kpis" id="kpis"></div>
  <div class="grid"><section class="card"><h2>Price — selected window (adjusted close)</h2>
    <div class="chartbox"><canvas id="chart"></canvas></div><div class="stats" id="pstats"></div></section></div>
  <div class="grid two">
    <section class="card"><h2>Market analysis</h2><div id="market"></div></section>
    <section class="card"><h2>Fundamental research</h2><div id="research"></div></section></div>
  <div class="grid two">
    <section class="card"><h2>Risks</h2><ul id="risks"></ul></section>
    <section class="card"><h2>Limitations</h2><ul id="limits"></ul></section></div>
  <div class="grid"><section class="panel"><h2 style="color:#aecbff">Analyst view — descriptive, not advice</h2>
    <div id="analyst"></div></section></div>
  <div class="disc" id="disclaimer"></div><div class="foot" id="foot"></div>
</div>
<script>
const DATA=__DATA__;
const q=DATA.quote,f=DATA.fundamentals,sm=DATA.stock_metrics,fm=DATA.financial_metrics;
const $=id=>document.getElementById(id);
const usd=n=>n==null?"—":"$"+Number(n).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const big=n=>{if(n==null)return"—";const a=Math.abs(n);
  if(a>=1e12)return"$"+(n/1e12).toFixed(2)+"T";if(a>=1e9)return"$"+(n/1e9).toFixed(2)+"B";
  if(a>=1e6)return"$"+(n/1e6).toFixed(2)+"M";return usd(n);};
const pct=n=>n==null?"—":(n>=0?"+":"")+Number(n).toFixed(2)+"%";
document.title=DATA.company+" ("+DATA.symbol+")";
$("h-title").textContent=DATA.company+" ("+DATA.symbol+")";
$("h-sub").textContent="Data as of "+q.data_as_of+" · "+(DATA.data_mode||"")+" · model "+(DATA.model||"");
const dchg=q.day_change,dir=(dchg||0)<0?"down":"up";
$("kpis").innerHTML=[
  {l:"Current price",v:usd(q.current_price),d:(dchg==null?"":(dchg>=0?"+":"")+dchg.toFixed(2))+" ("+pct(q.day_change_pct)+") "+dir.toUpperCase(),c:dir},
  {l:"Market cap",v:big(f.market_cap)},
  {l:"P/E (trailing)",v:f.pe_ratio==null?"—":Number(f.pe_ratio).toFixed(2)},
  {l:"52-week range",v:usd(q.fifty_two_week_low)+" – "+usd(q.fifty_two_week_high)},
].map(k=>'<div class="card kpi"><div class="label">'+k.l+'</div><div class="value tnum">'+k.v+'</div>'+(k.d?'<div class="delta tnum '+k.c+'">'+k.d+'</div>':'')+'</div>').join("");
$("pstats").innerHTML=[
  "Start <b class='tnum'>"+usd(sm.start_price)+"</b>","End <b class='tnum'>"+usd(sm.end_price)+"</b>",
  "Return <b class='tnum "+(sm.period_return_pct<0?'down':'up')+"'>"+pct(sm.period_return_pct)+"</b>",
  "High <b class='tnum'>"+usd(sm.max_price)+"</b> ("+sm.max_date+")",
  "Low <b class='tnum'>"+usd(sm.min_price)+"</b> ("+sm.min_date+")","Obs <b>"+sm.observations+"</b>",
].map(s=>"<span>"+s+"</span>").join("");
const esc=s=>(s==null?"":String(s)).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
$("market").innerHTML="<p>"+esc(DATA.market.summary)+"</p>";
$("research").innerHTML="<p>"+esc(DATA.research.summary)+"</p>";
const li=a=>(a||[]).map(x=>"<li>"+esc(x)+"</li>").join("");
$("risks").innerHTML=li(DATA.risks); $("limits").innerHTML=li(DATA.limitations);
$("analyst").innerHTML="<p>Over the selected window the price trend is <b>"+(sm.period_return_pct<0?"down":"up")+" "+pct(sm.period_return_pct)+"</b>, ending at <b>"+usd(sm.end_price)+"</b> within a 52-week range of "+usd(q.fifty_two_week_low)+"–"+usd(q.fifty_two_week_high)+". Valuation shows a trailing P/E of <b>"+(f.pe_ratio?Number(f.pe_ratio).toFixed(1):"—")+"</b> with an FY"+f.fiscal_year+" net margin of <b>"+(fm.net_margin_pct?Number(fm.net_margin_pct).toFixed(1):"—")+"%</b>. These are observations from the data, not a buy/sell call or price target.</p>";
$("disclaimer").innerHTML="<b>Not financial advice.</b> Automated AI analysis for information only; not affiliated with or endorsed by "+esc(DATA.company)+"; no buy/sell recommendation or price target.";
$("foot").innerHTML="Sources: "+esc((DATA.sources||[]).join("; "))+".<br>Multi-agent workflow (Market + Research analysts, Report Writer) on "+esc(DATA.model)+"; "+DATA.cost.calls+" model calls, est. $"+Number(DATA.cost.usd||0).toFixed(4)+". Live Yahoo Finance data (may be ~15 min delayed), not audited.";
const css=getComputedStyle(document.documentElement);
const accent=css.getPropertyValue("--accent").trim()||"#2a5298";
const grid=css.getPropertyValue("--line").trim()||"#e3ebf0";
const muted=css.getPropertyValue("--muted").trim()||"#5d6d7e";
const ctx=$("chart").getContext("2d");
const g=ctx.createLinearGradient(0,0,0,300);g.addColorStop(0,accent+"55");g.addColorStop(1,accent+"05");
const last=DATA.series.length-1;
new Chart(ctx,{type:"line",data:{labels:DATA.series.map(p=>p.d),datasets:[{data:DATA.series.map(p=>p.c),
  borderColor:accent,backgroundColor:g,fill:true,tension:.25,borderWidth:2,
  pointRadius:DATA.series.map((_,i)=>i===last?4:0),pointBackgroundColor:accent}]},
  options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},
    tooltip:{callbacks:{label:c=>usd(c.parsed.y)}}},
    scales:{x:{grid:{display:false},ticks:{color:muted,maxTicksLimit:7,autoSkip:true}},
      y:{grid:{color:grid},ticks:{color:muted,callback:v=>"$"+v}}}}});
</script></body></html>"""
