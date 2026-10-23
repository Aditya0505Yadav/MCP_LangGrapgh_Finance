"""Data-access repositories for the ``app`` observability schema.

All SQL is parameterized. JSONB values are wrapped with psycopg's ``Json``
adapter. Methods are small and explicit so the persistence boundary is easy to
audit. Repositories never see secrets — the caller passes already-sanitized
payloads (see ``app.observability.tracing``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class Repositories:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------ runs
    async def create_run(
        self,
        *,
        run_id: str,
        thread_id: str,
        trace_id: str,
        symbol: str,
        sanitized_input: dict[str, Any],
        data_mode: str,
        workflow_version: str,
        code_version: str,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO app.runs
                    (run_id, thread_id, trace_id, symbol, sanitized_input,
                     data_mode, workflow_version, code_version, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'created')
                """,
                (
                    run_id, thread_id, trace_id, symbol, Json(sanitized_input),
                    data_mode, workflow_version, code_version,
                ),
            )

    async def mark_run_started(self, run_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE app.runs
                   SET status = 'running',
                       first_started_at = COALESCE(first_started_at, %s)
                 WHERE run_id = %s
                """,
                (_utcnow(), run_id),
            )

    async def update_run_status(
        self,
        run_id: str,
        status: str,
        *,
        error_summary: str | None = None,
        final_report_id: str | None = None,
        finished: bool = False,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE app.runs
                   SET status = %s,
                       error_summary = COALESCE(%s, error_summary),
                       final_report_id = COALESCE(%s, final_report_id),
                       last_finished_at = CASE WHEN %s THEN %s ELSE last_finished_at END
                 WHERE run_id = %s
                """,
                (status, error_summary, final_report_id, finished, _utcnow(), run_id),
            )

    async def get_run(self, run_id: str) -> dict | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT * FROM app.runs WHERE run_id = %s", (run_id,))
            return await cur.fetchone()

    async def get_run_by_thread(self, thread_id: str) -> dict | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM app.runs WHERE thread_id = %s", (thread_id,)
            )
            return await cur.fetchone()

    async def list_runs(self, limit: int = 50) -> list[dict]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM app.runs ORDER BY created_at DESC LIMIT %s", (limit,)
            )
            return await cur.fetchall()

    # ---------------------------------------------------- execution_attempts
    async def next_attempt_number(self, run_id: str) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) AS n "
                "FROM app.execution_attempts WHERE run_id = %s",
                (run_id,),
            )
            row = await cur.fetchone()
            return int(row["n"]) + 1

    async def create_attempt(
        self,
        *,
        execution_attempt_id: str,
        run_id: str,
        attempt_number: int,
        kind: str,
        worker_id: str,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO app.execution_attempts
                    (execution_attempt_id, run_id, attempt_number, kind, status,
                     started_at, worker_id, heartbeat_at)
                VALUES (%s,%s,%s,%s,'running',%s,%s,%s)
                """,
                (execution_attempt_id, run_id, attempt_number, kind,
                 _utcnow(), worker_id, _utcnow()),
            )

    async def heartbeat_attempt(self, execution_attempt_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE app.execution_attempts SET heartbeat_at = %s "
                "WHERE execution_attempt_id = %s",
                (_utcnow(), execution_attempt_id),
            )

    async def finish_attempt(
        self,
        *,
        execution_attempt_id: str,
        status: str,
        duration_ms: int,
        checkpoint_reference: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                UPDATE app.execution_attempts
                   SET status = %s, finished_at = %s, duration_ms = %s,
                       checkpoint_reference = COALESCE(%s, checkpoint_reference),
                       error_code = %s, error_message = %s
                 WHERE execution_attempt_id = %s
                """,
                (status, _utcnow(), duration_ms, checkpoint_reference,
                 error_code, error_message, execution_attempt_id),
            )

    async def get_attempts(self, run_id: str) -> list[dict]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM app.execution_attempts WHERE run_id = %s "
                "ORDER BY attempt_number",
                (run_id,),
            )
            return await cur.fetchall()

    # ----------------------------------------------------------------- spans
    async def insert_span(self, fields: dict[str, Any]) -> None:
        payload = dict(fields)
        for key in ("input_payload", "output_payload"):
            if key in payload and payload[key] is not None:
                payload[key] = Json(payload[key])
        cols = ", ".join(payload.keys())
        placeholders = ", ".join(["%s"] * len(payload))
        async with self._pool.connection() as conn:
            await conn.execute(
                f"INSERT INTO app.spans ({cols}) VALUES ({placeholders})",
                tuple(payload.values()),
            )

    async def update_span(self, span_id: str, fields: dict[str, Any]) -> None:
        payload = dict(fields)
        for key in ("input_payload", "output_payload"):
            if key in payload and payload[key] is not None:
                payload[key] = Json(payload[key])
        set_clause = ", ".join(f"{col} = %s" for col in payload)
        async with self._pool.connection() as conn:
            await conn.execute(
                f"UPDATE app.spans SET {set_clause} WHERE span_id = %s",
                (*payload.values(), span_id),
            )

    async def get_spans_for_run(self, run_id: str) -> list[dict]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM app.spans WHERE run_id = %s ORDER BY started_at", (run_id,)
            )
            return await cur.fetchall()

    async def reconcile_running_spans(
        self, run_id: str, execution_attempt_id: str
    ) -> int:
        """Mark still-'running' spans from a prior attempt as 'unknown'.

        A process kill can leave spans open. We cannot invent a completion time,
        so we set status='unknown' and record that it was interrupted — distinct
        from a confirmed failure.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                UPDATE app.spans
                   SET status = 'unknown',
                       error_type = COALESCE(error_type, 'interrupted'),
                       error_message = COALESCE(error_message,
                           'process ended before span was finalized')
                 WHERE run_id = %s AND status = 'running'
                   AND execution_attempt_id IS DISTINCT FROM %s
                """,
                (run_id, execution_attempt_id),
            )
            return cur.rowcount

    # ---------------------------------------------------------------- events
    async def insert_event(
        self,
        *,
        event_id: str,
        run_id: str | None,
        execution_attempt_id: str | None,
        span_id: str | None,
        event_type: str,
        severity: str,
        message: str | None,
        sanitized_details: dict | None,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO app.events
                    (event_id, run_id, execution_attempt_id, span_id, event_time,
                     event_type, severity, message, sanitized_details)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (event_id, run_id, execution_attempt_id, span_id, _utcnow(),
                 event_type, severity, message,
                 Json(sanitized_details) if sanitized_details is not None else None),
            )

    async def get_events(self, run_id: str) -> list[dict]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM app.events WHERE run_id = %s ORDER BY event_time",
                (run_id,),
            )
            return await cur.fetchall()

    # ------------------------------------------------------------- llm_calls
    async def insert_llm_call(self, fields: dict[str, Any]) -> None:
        payload = dict(fields)
        for key in ("generation_parameters", "usage_raw"):
            if key in payload and payload[key] is not None:
                payload[key] = Json(payload[key])
        cols = ", ".join(payload.keys())
        placeholders = ", ".join(["%s"] * len(payload))
        async with self._pool.connection() as conn:
            await conn.execute(
                f"INSERT INTO app.llm_calls ({cols}) VALUES ({placeholders})",
                tuple(payload.values()),
            )

    async def get_llm_calls_for_run(self, run_id: str) -> list[dict]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT lc.*
                  FROM app.llm_calls lc
                  JOIN app.spans s ON s.span_id = lc.span_id
                 WHERE s.run_id = %s
                 ORDER BY lc.created_at
                """,
                (run_id,),
            )
            return await cur.fetchall()

    async def get_cost_summary(self, run_id: str) -> dict:
        """Aggregate each actual LLM call exactly once (one row per call).

        Known estimated subtotal + the count of calls whose cost is unknown.
        Nested spans are not llm_calls, so this never double-counts.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT
                    COUNT(*)                                        AS call_count,
                    COALESCE(SUM(input_tokens), 0)                  AS input_tokens,
                    COALESCE(SUM(output_tokens), 0)                 AS output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0)             AS cache_read_tokens,
                    COALESCE(SUM(cache_write_tokens_short_ttl), 0)  AS cache_write_short,
                    COALESCE(SUM(cache_write_tokens_long_ttl), 0)   AS cache_write_long,
                    COALESCE(SUM(estimated_cost_usd)
                        FILTER (WHERE estimated_cost_usd IS NOT NULL), 0) AS known_cost_usd,
                    COUNT(*) FILTER (WHERE estimated_cost_usd IS NULL)    AS unknown_cost_calls
                  FROM app.llm_calls lc
                  JOIN app.spans s ON s.span_id = lc.span_id
                 WHERE s.run_id = %s
                """,
                (run_id,),
            )
            return await cur.fetchone()

    # --------------------------------------------------------------- pricing
    async def get_active_pricing(
        self, provider: str, model: str, at: datetime | None = None
    ) -> dict | None:
        at = at or _utcnow()
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT * FROM app.pricing
                 WHERE provider = %s AND model = %s
                   AND effective_from <= %s
                   AND (effective_to IS NULL OR effective_to > %s)
                 ORDER BY effective_from DESC
                 LIMIT 1
                """,
                (provider, model, at, at),
            )
            return await cur.fetchone()

    # --------------------------------------------------------------- reports
    async def next_report_version(self, run_id: str) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM app.reports WHERE run_id = %s",
                (run_id,),
            )
            row = await cur.fetchone()
            return int(row["v"]) + 1

    async def insert_report(
        self,
        *,
        report_id: str,
        run_id: str,
        version: int,
        content: str,
        sources: list | dict | None,
        writer_span_id: str | None,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO app.reports
                    (report_id, run_id, version, content, sources, writer_span_id)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (report_id, run_id, version, content,
                 Json(sources) if sources is not None else None, writer_span_id),
            )

    async def get_latest_report(self, run_id: str) -> dict | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM app.reports WHERE run_id = %s "
                "ORDER BY version DESC LIMIT 1",
                (run_id,),
            )
            return await cur.fetchone()
