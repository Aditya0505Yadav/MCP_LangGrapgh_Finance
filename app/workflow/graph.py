"""WorkflowBuilder: nodes, parallel branches, genuine join, conditional route,
and the official PostgreSQL checkpointer.

Topology (see docs/diagrams):

    START -> ingest
    ingest -> market_fetch         ingest -> research_fetch      (parallel)
    market_fetch -> market_analysis
    research_fetch -> research_analysis
    [market_analysis, research_analysis] -> validate            (join: waits for BOTH)
    validate -(data_ready?)-> writer | incomplete               (conditional)
    writer -> END ; incomplete -> END
"""

from __future__ import annotations

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg_pool import AsyncConnectionPool

from app.workflow.deps import WorkflowDeps
from app.workflow.nodes import Nodes
from app.workflow.state import AnalysisState


async def build_checkpointer(pool: AsyncConnectionPool) -> AsyncPostgresSaver:
    """Construct the official async Postgres checkpointer and create its tables.

    ``setup()`` is idempotent, so calling it on every startup is safe. These are
    the SDK-managed checkpoint tables — we never alter them.
    """
    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    return saver


def build_graph(deps: WorkflowDeps, checkpointer: AsyncPostgresSaver):
    nodes = Nodes(deps)
    builder = StateGraph(AnalysisState)

    builder.add_node("ingest", nodes.ingest)
    builder.add_node("market_fetch", nodes.market_fetch)
    builder.add_node("research_fetch", nodes.research_fetch)
    builder.add_node("market_analysis", nodes.market_analysis)
    builder.add_node("research_analysis", nodes.research_analysis)
    builder.add_node("validate", nodes.validate)
    builder.add_node("writer", nodes.writer)
    builder.add_node("incomplete", nodes.incomplete)

    builder.add_edge(START, "ingest")
    # Fan out into two parallel branches.
    builder.add_edge("ingest", "market_fetch")
    builder.add_edge("ingest", "research_fetch")
    builder.add_edge("market_fetch", "market_analysis")
    builder.add_edge("research_fetch", "research_analysis")
    # Genuine multi-source join: validate runs once, only after BOTH analyses.
    builder.add_edge(["market_analysis", "research_analysis"], "validate")
    # Deterministic conditional route (application logic, not an LLM).
    builder.add_conditional_edges(
        "validate", nodes.route, {"writer": "writer", "incomplete": "incomplete"}
    )
    builder.add_edge("writer", END)
    builder.add_edge("incomplete", END)

    return builder.compile(checkpointer=checkpointer)
