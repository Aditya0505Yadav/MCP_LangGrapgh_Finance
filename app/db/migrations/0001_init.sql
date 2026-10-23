-- Application observability schema (migration 0001).
--
-- This is the `app` schema only. The LangGraph checkpoint tables that provide
-- durable workflow recovery live in the public schema and are created/managed
-- by the official AsyncPostgresSaver.setup() — we never define or alter them.
--
-- All ids are UUIDs supplied by the application (uuid4). All wall-clock columns
-- are TIMESTAMPTZ stored in UTC. Monetary values are NUMERIC (never float).

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- runs: one logical analysis request.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.runs (
    run_id            UUID PRIMARY KEY,
    thread_id         TEXT NOT NULL UNIQUE,     -- correlates to the checkpointer thread
    trace_id          TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    sanitized_input   JSONB NOT NULL,
    data_mode         TEXT NOT NULL,
    workflow_version  TEXT NOT NULL,
    code_version      TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'created'
        CHECK (status IN ('created','running','completed','incomplete','failed','interrupted')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_started_at  TIMESTAMPTZ,
    last_finished_at  TIMESTAMPTZ,
    final_report_id   UUID,                      -- points at app.reports (no FK: avoids a cycle)
    error_summary     TEXT
);

-- ---------------------------------------------------------------------------
-- execution_attempts: one initial OR resume invocation of a run's thread.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.execution_attempts (
    execution_attempt_id UUID PRIMARY KEY,
    run_id               UUID NOT NULL REFERENCES app.runs(run_id),
    attempt_number       INTEGER NOT NULL,
    kind                 TEXT NOT NULL CHECK (kind IN ('initial','resume')),
    status               TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','completed','incomplete','failed','interrupted')),
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at          TIMESTAMPTZ,
    duration_ms          BIGINT,
    checkpoint_reference TEXT,
    worker_id            TEXT,
    heartbeat_at         TIMESTAMPTZ,
    error_code           TEXT,
    error_message        TEXT,
    UNIQUE (run_id, attempt_number)
);

-- ---------------------------------------------------------------------------
-- spans: timed record of one operation, correlated across processes by
-- trace_id + parent_span_id (app process AND the MCP server process write here).
-- run_id is nullable so a server-side span is still recorded if correlation
-- headers are missing; in normal operation it is always populated.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.spans (
    span_id              UUID PRIMARY KEY,
    trace_id             TEXT NOT NULL,
    parent_span_id       UUID,
    run_id               UUID REFERENCES app.runs(run_id),
    execution_attempt_id UUID REFERENCES app.execution_attempts(execution_attempt_id),
    logical_step_id      TEXT,
    node_name            TEXT,
    agent_role           TEXT,
    span_kind            TEXT NOT NULL
        CHECK (span_kind IN ('root','node','mcp_client','mcp_server','provider_http',
                             'llm','validation','route','persist','recovery')),
    service_name         TEXT NOT NULL,
    operation_name       TEXT,
    tool_name            TEXT,
    attempt_number       INTEGER,
    status               TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','ok','error','interrupted','unknown')),
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at          TIMESTAMPTZ,
    duration_ms          BIGINT,
    timeout_ms           BIGINT,
    request_id           TEXT,
    provider_request_id  TEXT,
    input_payload        JSONB,
    output_payload       JSONB,
    input_hash           TEXT,
    output_hash          TEXT,
    payload_truncated    BOOLEAN NOT NULL DEFAULT FALSE,
    error_type           TEXT,
    error_code           TEXT,
    error_message        TEXT,
    retryable            BOOLEAN
);

-- ---------------------------------------------------------------------------
-- events: discrete, timestamped occurrences inside a run.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.events (
    event_id             UUID PRIMARY KEY,
    run_id               UUID REFERENCES app.runs(run_id),
    execution_attempt_id UUID REFERENCES app.execution_attempts(execution_attempt_id),
    span_id              UUID REFERENCES app.spans(span_id),
    event_time           TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type           TEXT NOT NULL,
    severity             TEXT NOT NULL DEFAULT 'info'
        CHECK (severity IN ('debug','info','warning','error')),
    message              TEXT,
    sanitized_details    JSONB
);

-- ---------------------------------------------------------------------------
-- pricing: versioned provider pricing records used for cost estimates.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.pricing (
    pricing_id        UUID PRIMARY KEY,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    effective_from    TIMESTAMPTZ NOT NULL,
    effective_to      TIMESTAMPTZ,
    currency          TEXT NOT NULL DEFAULT 'USD',
    rates_per_million JSONB NOT NULL,   -- {input, output, cache_read, cache_write_5m, cache_write_1h}
    modifiers         JSONB,
    source_url        TEXT,
    checked_at        TIMESTAMPTZ,
    UNIQUE (provider, model, effective_from)
);

-- ---------------------------------------------------------------------------
-- llm_calls: one actual Claude API call (1:1 with its llm span).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.llm_calls (
    llm_call_id                  UUID PRIMARY KEY,
    span_id                      UUID NOT NULL UNIQUE REFERENCES app.spans(span_id),
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    provider                     TEXT NOT NULL,
    requested_model              TEXT NOT NULL,
    returned_model               TEXT,
    prompt_version               TEXT NOT NULL,
    prompt_hash                  TEXT NOT NULL,
    generation_parameters        JSONB,
    usage_raw                    JSONB,
    input_tokens                 BIGINT,
    output_tokens                BIGINT,
    cache_read_tokens            BIGINT,
    cache_write_tokens_short_ttl BIGINT,
    cache_write_tokens_long_ttl  BIGINT,
    usage_status                 TEXT NOT NULL DEFAULT 'unknown'
        CHECK (usage_status IN ('recorded','partial','unknown')),
    estimated_cost_usd           NUMERIC,
    pricing_id                   UUID REFERENCES app.pricing(pricing_id),
    stop_reason                  TEXT,
    provider_request_id          TEXT,
    ttft_ms                      BIGINT
);

-- ---------------------------------------------------------------------------
-- reports: the writer's output (versioned per run).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.reports (
    report_id      UUID PRIMARY KEY,
    run_id         UUID NOT NULL REFERENCES app.runs(run_id),
    version        INTEGER NOT NULL,
    content        TEXT NOT NULL,
    sources        JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    writer_span_id UUID REFERENCES app.spans(span_id),
    UNIQUE (run_id, version)
);

-- ---------------------------------------------------------------------------
-- Indexes (status/time, trace correlation, hot lookup paths).
-- JSON payloads are intentionally NOT indexed.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_runs_created_at       ON app.runs (created_at);
CREATE INDEX IF NOT EXISTS idx_runs_trace_id         ON app.runs (trace_id);
CREATE INDEX IF NOT EXISTS idx_attempts_run_started  ON app.execution_attempts (run_id, started_at);
CREATE INDEX IF NOT EXISTS idx_spans_run_started     ON app.spans (run_id, started_at);
CREATE INDEX IF NOT EXISTS idx_spans_trace_id        ON app.spans (trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_parent          ON app.spans (parent_span_id);
CREATE INDEX IF NOT EXISTS idx_spans_status_started  ON app.spans (status, started_at);
CREATE INDEX IF NOT EXISTS idx_spans_tool_started    ON app.spans (tool_name, started_at);
CREATE INDEX IF NOT EXISTS idx_events_run_time       ON app.events (run_id, event_time);
CREATE INDEX IF NOT EXISTS idx_llm_model_created     ON app.llm_calls (requested_model, created_at);
CREATE INDEX IF NOT EXISTS idx_reports_run           ON app.reports (run_id);
