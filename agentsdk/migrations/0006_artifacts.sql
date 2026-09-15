-- FR-55 (M13): artifacts, with their metadata and content in one table (P2-D8).
--
-- tenant_id and project_id are NOT NULL and lead every index (NFR-2, ADR-11): a
-- store is bound to one scope, and every statement it makes is filtered by both.
-- source_run references runs, and the store checks in the same statement as its
-- insert that the run belongs to the artifact's own tenant and project (FR-21's
-- rule for parent runs). There is no ON DELETE: removing a run that artifacts
-- still name is refused, so test cleanup removes artifacts first.
--
-- Content is capped per store (10,485,760 bytes by default), in the store rather
-- than here, because the cap is set per store.
--
-- Idempotent (FR-17). schema.sql is NOT edited.
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id      UUID        PRIMARY KEY,
    tenant_id        TEXT        NOT NULL,
    project_id       TEXT        NOT NULL,
    uri              TEXT        NOT NULL,
    mime_type        TEXT        NOT NULL,
    content_hash     TEXT        NOT NULL,
    size             BIGINT      NOT NULL CHECK (size >= 0),
    created_by_agent TEXT        NOT NULL,
    source_run       UUID        REFERENCES runs (run_id),
    source_task      TEXT,
    provenance       JSONB       NOT NULL,
    classification   TEXT,
    created_at       TIMESTAMPTZ NOT NULL,
    expires_at       TIMESTAMPTZ,
    content          BYTEA       NOT NULL
);

CREATE INDEX IF NOT EXISTS artifacts_tenant_project_created
    ON artifacts (tenant_id, project_id, created_at);

-- expire() removes a scope's expired artifacts; only rows that can expire are indexed.
CREATE INDEX IF NOT EXISTS artifacts_tenant_project_expires
    ON artifacts (tenant_id, project_id, expires_at)
    WHERE expires_at IS NOT NULL;
