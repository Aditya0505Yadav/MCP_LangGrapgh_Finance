"""Idempotent SQL migration runner.

Applies every ``NNNN_name.sql`` under ``app/db/migrations`` exactly once, in
filename order, tracking applied versions in ``app.schema_migrations``. Each
migration runs inside a transaction so a failure leaves nothing half-applied.
A session-level advisory lock lets the app and the MCP server both call this at
startup without racing. This is deliberately small and transparent — easy to
read and explain — rather than a full migration framework.
"""

from __future__ import annotations

import logging
from pathlib import Path

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Advisory-lock key so concurrent migrators (app + MCP server) serialize.
_MIGRATION_LOCK_KEY = 727274


def _split_sql_statements(script: str) -> list[str]:
    """Split a SQL script into individual statements.

    Strips ``--`` line comments and splits on semicolons that are not inside a
    single-quoted string literal. The migration SQL in this project uses
    semicolons only as statement terminators, so this is safe and keeps the
    runner dependency-free.
    """
    cleaned_lines = []
    for ln in script.splitlines():
        idx = ln.find("--")
        if idx != -1:
            ln = ln[:idx]
        cleaned_lines.append(ln)
    cleaned = "\n".join(cleaned_lines)

    statements: list[str] = []
    buf: list[str] = []
    in_string = False
    i = 0
    while i < len(cleaned):
        ch = cleaned[i]
        if ch == "'":
            if in_string and i + 1 < len(cleaned) and cleaned[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_string = not in_string
            buf.append(ch)
        elif ch == ";" and not in_string:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


async def _apply_pending(conn: AsyncConnection) -> list[str]:
    newly_applied: list[str] = []
    await conn.execute("CREATE SCHEMA IF NOT EXISTS app")
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS app.schema_migrations ("
        " version TEXT PRIMARY KEY,"
        " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    cur = await conn.execute("SELECT version FROM app.schema_migrations")
    applied = {row["version"] for row in await cur.fetchall()}

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        logger.warning("No migration files found in %s", MIGRATIONS_DIR)

    for path in files:
        version = path.stem
        if version in applied:
            continue
        statements = _split_sql_statements(path.read_text(encoding="utf-8"))
        async with conn.transaction():
            for stmt in statements:
                await conn.execute(stmt)
            await conn.execute(
                "INSERT INTO app.schema_migrations (version) VALUES (%s)", (version,)
            )
        logger.info("Applied migration %s (%d statements)", version, len(statements))
        newly_applied.append(version)
    return newly_applied


async def run_migrations(pool: AsyncConnectionPool) -> list[str]:
    """Apply pending migrations. Returns the list of versions newly applied."""
    async with pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
        try:
            return await _apply_pending(conn)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))
