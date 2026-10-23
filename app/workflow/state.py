"""Typed workflow state.

Each key is written by exactly one node (parallel branches own disjoint keys), so
no reducers are needed and there are no concurrent-write conflicts. Branch
sources/errors are branch-specific and merged at the validation node. Nodes
return partial updates — they never mutate a shared dict in place. Credentials
and DB handles are NOT part of state; they are injected via runtime config /
closures (see graph.py).
"""

from __future__ import annotations

from typing import Any

from typing_extensions import TypedDict


class AnalysisState(TypedDict, total=False):
    # --- identity / input (written by the input node) ---
    run_id: str
    symbol: str
    date_range: dict[str, str]        # {"start_date": ..., "end_date": ...}
    reporting_period: str
    period_type: str
    data_mode: str
    workflow_version: str

    # --- market retrieval branch ---
    market_data: dict[str, Any]
    market_fetch_status: str          # ok | no_data | failed
    market_fetch_error: str | None
    market_sources: list[str]

    # --- market analysis ---
    market_analysis: dict[str, Any]
    market_status: str                # ok | failed
    market_error: str | None

    # --- research retrieval branch ---
    financial_data: dict[str, Any]
    financial_fetch_status: str       # ok | no_data | failed
    financial_fetch_error: str | None
    financial_sources: list[str]

    # --- research analysis ---
    research_analysis: dict[str, Any]
    research_status: str              # ok | failed
    research_error: str | None

    # --- validation ---
    data_ready: bool
    validation_issues: list[str]

    # --- result ---
    final_report: dict[str, Any] | None
    result_status: str                # completed | incomplete
