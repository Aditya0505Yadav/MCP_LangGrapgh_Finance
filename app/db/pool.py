"""PostgreSQL connection pools.

Two *separate* pools with bounded sizes, as the design requires:

* ``app_pool``        — application observability reads/writes (runs, spans,
  events, llm_calls, reports, pricing).
* ``checkpointer_pool`` — reserved exclusively for the LangGraph
  AsyncPostgresSaver, so heavy observability traffic can never starve
  checkpoint writes.

Both use autocommit + dict rows. The checkpointer pool additionally disables
server-side prepared statements (``prepare_threshold=0``), the configuration the
async Postgres saver expects.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import Settings


def _make_pool(
    conninfo: str, *, min_size: int, max_size: int, prepare_threshold: int | None
) -> AsyncConnectionPool:
    kwargs: dict = {"autocommit": True, "row_factory": dict_row}
    if prepare_threshold is not None:
        kwargs["prepare_threshold"] = prepare_threshold
    # open=False avoids opening the pool inside the constructor (deprecated for
    # the async pool); callers await .open() explicitly.
    return AsyncConnectionPool(
        conninfo=conninfo,
        min_size=min_size,
        max_size=max_size,
        kwargs=kwargs,
        open=False,
    )


@dataclass
class DatabasePools:
    app_pool: AsyncConnectionPool
    checkpointer_pool: AsyncConnectionPool

    async def open(self) -> None:
        await self.app_pool.open(wait=True)
        await self.checkpointer_pool.open(wait=True)

    async def close(self) -> None:
        await self.app_pool.close()
        await self.checkpointer_pool.close()


def build_pools(settings: Settings) -> DatabasePools:
    return DatabasePools(
        app_pool=_make_pool(
            settings.database_url,
            min_size=settings.app_pool_min_size,
            max_size=settings.app_pool_max_size,
            prepare_threshold=None,
        ),
        checkpointer_pool=_make_pool(
            settings.database_url,
            min_size=settings.checkpointer_pool_min_size,
            max_size=settings.checkpointer_pool_max_size,
            prepare_threshold=0,
        ),
    )


def build_app_pool(settings: Settings) -> AsyncConnectionPool:
    """A single app pool — used by the MCP server process, which only needs the
    observability schema (no checkpointer)."""
    return _make_pool(
        settings.database_url,
        min_size=settings.app_pool_min_size,
        max_size=settings.app_pool_max_size,
        prepare_threshold=None,
    )


__all__ = ["DatabasePools", "build_pools", "build_app_pool", "dict_row"]
