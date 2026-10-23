"""FastAPI application exposing the run/monitor endpoints.

These are application endpoints, NOT MCP protocol endpoints — the MCP server is a
separate service at its own Streamable HTTP endpoint.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from app.config import get_settings
from app.dashboard import build_payload, render_dashboard_html, render_index
from app.eventloop import use_selector_event_loop_on_windows
from app.report_docx import build_report_docx
from app.runtime import Runtime
from app.service import ConcurrencyError, NotResumableError
from app.views import _to_jsonable, build_run_detail

# psycopg async requires the Selector event loop on Windows (incl. under uvicorn).
use_selector_event_loop_on_windows()


class AnalyzeRequest(BaseModel):
    symbol: str
    start_date: str
    end_date: str
    reporting_period: str = "latest"
    period_type: str = "annual"
    data_mode: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = await Runtime(get_settings()).startup()
    app.state.runtime = runtime
    try:
        yield
    finally:
        await runtime.shutdown()


app = FastAPI(title="Finance Analysis Assistant", version="0.1.0", lifespan=lifespan)


def _svc(app: FastAPI):
    return app.state.runtime.service


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    rt = app.state.runtime
    recent = await rt.repos.list_runs(15)
    return HTMLResponse(render_index(recent, rt.settings.data_mode, rt.settings.claude_model))


@app.get("/analyses/{run_id}/dashboard", response_class=HTMLResponse)
async def dashboard(run_id: str) -> HTMLResponse:
    rt = app.state.runtime
    payload = await build_payload(rt.repos, rt.settings, rt.service.graph, run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="unknown run")
    return HTMLResponse(render_dashboard_html(payload, run_id))


@app.get("/analyses/{run_id}/report.docx")
async def report_docx(run_id: str, request: Request) -> Response:
    rt = app.state.runtime
    payload = await build_payload(rt.repos, rt.settings, rt.service.graph, run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="unknown run")
    dash_url = str(request.base_url).rstrip("/") + f"/analyses/{run_id}/dashboard"
    data = build_report_docx(payload, dashboard_url=dash_url,
                             generated_on=datetime.now().strftime("%Y-%m-%d %H:%M"))
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{payload["symbol"]}_analysis.docx"'},
    )


@app.post("/analyses")
async def create_analysis(req: AnalyzeRequest) -> JSONResponse:
    svc = _svc(app)
    created = await svc.create_run(
        symbol=req.symbol, start_date=req.start_date, end_date=req.end_date,
        reporting_period=req.reporting_period, period_type=req.period_type,
        data_mode=req.data_mode,
    )
    result = await svc.execute_run(created["run_id"])
    return JSONResponse(_to_jsonable({**created, **result}))


@app.get("/analyses")
async def list_analyses(limit: int = 50) -> JSONResponse:
    runs = await app.state.runtime.repos.list_runs(limit)
    return JSONResponse(_to_jsonable(runs))


@app.get("/analyses/{run_id}")
async def get_analysis(run_id: str) -> JSONResponse:
    try:
        status = await _svc(app).get_status(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return JSONResponse(_to_jsonable(status))


@app.post("/analyses/{run_id}/resume")
async def resume_analysis(run_id: str) -> JSONResponse:
    try:
        result = await _svc(app).resume_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except NotResumableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ConcurrencyError as exc:
        raise HTTPException(status_code=423, detail=str(exc))
    return JSONResponse(_to_jsonable(result))


@app.get("/analyses/{run_id}/report")
async def get_report(run_id: str) -> JSONResponse:
    report = await app.state.runtime.repos.get_latest_report(run_id)
    if report is None:
        raise HTTPException(status_code=404, detail="no report yet")
    return JSONResponse(_to_jsonable({
        "run_id": run_id, "version": report["version"],
        "content": report["content"], "sources": report.get("sources"),
    }))


@app.get("/analyses/{run_id}/trace")
async def get_trace(run_id: str) -> JSONResponse:
    rt = app.state.runtime
    try:
        detail = await build_run_detail(rt.repos, rt.settings, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return JSONResponse(detail)


@app.get("/analyses/{run_id}/view", response_class=HTMLResponse)
async def get_run_html(run_id: str) -> HTMLResponse:
    rt = app.state.runtime
    try:
        detail = await build_run_detail(rt.repos, rt.settings, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    from app.htmlview import render_run_html
    return HTMLResponse(render_run_html(detail))


if __name__ == "__main__":
    from app.eventloop import run_uvicorn

    s = get_settings()
    run_uvicorn("app.main:app", s.api_host, s.api_port)
