-- Phase 0 schema (LLD 2). One database, several tables, no separate
-- infrastructure per store.
--
-- ADR-11 is enforced here, not assumed at read time: tenant_id and project_id
-- are NOT NULL on every table and indexed. A row that cannot say which tenant
-- it belongs to must not be storable.

CREATE TABLE IF NOT EXISTS runs (
    run_id            UUID PRIMARY KEY,
    tenant_id         TEXT        NOT NULL,
    project_id        TEXT        NOT NULL,
    agent_spec_id     TEXT        NOT NULL,
    status            TEXT        NOT NULL
                      CHECK (status IN ('running', 'completed', 'failed', 'max_turns_exceeded')),
    principal_context JSONB,          -- lean metadata, unread by any Phase 0 consumer (ADR-27)
    max_turns         INTEGER     NOT NULL,
    model_id          TEXT,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ
);

-- The read pattern for tenant-scoped listing.
CREATE INDEX IF NOT EXISTS runs_tenant_project_started
    ON runs (tenant_id, project_id, started_at);

-- Conversation history. Insert-only in Phase 0: no UPDATE, no DELETE.
CREATE TABLE IF NOT EXISTS messages (
    message_id   UUID PRIMARY KEY,
    run_id       UUID        NOT NULL REFERENCES runs (run_id),
    tenant_id    TEXT        NOT NULL,   -- denormalised: query isolation without a join
    project_id   TEXT        NOT NULL,
    sequence_no  INTEGER     NOT NULL,
    role         TEXT        NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content      TEXT,
    tool_calls   JSONB,
    tool_results JSONB,                  -- each element carries its ContentProvenance
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Ordering is a database guarantee, not a convention. Two writers racing to
    -- append to the same run collide here rather than silently interleaving --
    -- which matters from Phase 2, when subagents write concurrently.
    UNIQUE (run_id, sequence_no)
);

-- The read pattern is always "every message for this run, in order".
CREATE INDEX IF NOT EXISTS messages_run_sequence ON messages (run_id, sequence_no);
CREATE INDEX IF NOT EXISTS messages_tenant_project ON messages (tenant_id, project_id);

-- The ordered audit stream. NOT the source of execution truth: runs + messages
-- are authoritative, and a trace reconstructs from state PLUS events together.
CREATE TABLE IF NOT EXISTS run_events (
    event_id        UUID PRIMARY KEY,
    schema_version  INTEGER     NOT NULL,
    sequence_no     INTEGER     NOT NULL,
    event_type      TEXT        NOT NULL,
    tenant_id       TEXT        NOT NULL,
    project_id      TEXT        NOT NULL,
    run_id          UUID        NOT NULL REFERENCES runs (run_id),
    -- Present and empty in Phase 0. Phase 2's subagents and Phase 6's execution
    -- attempts populate these columns; they do not add them.
    agent_id        TEXT,
    task_id         TEXT,
    tool_call_id    TEXT,
    attempt_id      TEXT,
    parent_event_id UUID,
    correlation_id  UUID,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (run_id, sequence_no)
);

CREATE INDEX IF NOT EXISTS run_events_run_sequence ON run_events (run_id, sequence_no);
CREATE INDEX IF NOT EXISTS run_events_tenant_project ON run_events (tenant_id, project_id);

-- Exactly one row per run (enforced by the primary key, not by discipline).
-- Nothing reads this in Phase 0; it is written so "what configuration produced
-- this run" is answerable during debugging now, and gateable in Phase 8.
CREATE TABLE IF NOT EXISTS execution_manifests (
    run_id                UUID PRIMARY KEY REFERENCES runs (run_id),
    tenant_id             TEXT        NOT NULL,
    project_id            TEXT        NOT NULL,
    sdk_version           TEXT        NOT NULL,
    agent_spec_hash       TEXT        NOT NULL,
    instructions_hash     TEXT        NOT NULL,
    model_id              TEXT,
    model_version         TEXT,
    model_adapter_version TEXT,
    tool_spec_hashes      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    policy_version        TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS execution_manifests_tenant_project
    ON execution_manifests (tenant_id, project_id);

CREATE TABLE IF NOT EXISTS model_registry (
    provider                TEXT    NOT NULL,
    model_id                TEXT    NOT NULL,
    model_version           TEXT    NOT NULL,
    adapter_version         TEXT    NOT NULL,
    capabilities            JSONB   NOT NULL DEFAULT '{}'::jsonb,
    known_quirks            TEXT,
    eval_status             TEXT,
    production_eligibility  BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (provider, model_id, model_version)
);
