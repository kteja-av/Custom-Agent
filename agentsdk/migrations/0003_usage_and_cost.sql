-- FR-31 (M9): what a run consumed and cost, and the settings and prices that
-- produced it.
--
-- runs: the six token totals (Usage's fields, FR-29) and cost_usd. NUMERIC, not
-- a float: money summed across calls must not drift. NULL means unknown -- a
-- model with no price, or a row written before this migration -- and a cost
-- that is unknown is never written as 0 (FR-30).
--
-- execution_manifests: the effective output limit and reasoning effort (FR-27,
-- FR-28) and the prices used, so a stored cost stays checkable after a price
-- changes. agent_spec_hash is deliberately untouched, so manifests stay
-- comparable across this migration.
--
-- schema.sql is NOT edited. It is migration 0001, its checksum is recorded on
-- every database, and an edited applied migration is refused (FR-17).
ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS prompt_tokens      BIGINT,
    ADD COLUMN IF NOT EXISTS completion_tokens  BIGINT,
    ADD COLUMN IF NOT EXISTS total_tokens       BIGINT,
    ADD COLUMN IF NOT EXISTS cache_read_tokens  BIGINT,
    ADD COLUMN IF NOT EXISTS cache_write_tokens BIGINT,
    ADD COLUMN IF NOT EXISTS reasoning_tokens   BIGINT,
    ADD COLUMN IF NOT EXISTS cost_usd           NUMERIC;

ALTER TABLE execution_manifests
    ADD COLUMN IF NOT EXISTS max_output_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS reasoning_effort  TEXT,
    ADD COLUMN IF NOT EXISTS pricing           JSONB;
