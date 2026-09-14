-- FR-43 (M11): the scheduler limits a run executed under -- its
-- max_concurrent_tools and tool_concurrency_limits, and the Runner's
-- provider_concurrency_limits -- so how far a run fanned out stays explainable
-- after the fact, as its prices are (0003).
--
-- NULL for a manifest written before this migration. That run executed its tool
-- calls one at a time with no limits to record, and writing today's defaults
-- into it would claim a configuration it never had.
--
-- schema.sql is NOT edited. It is migration 0001, its checksum is recorded on
-- every database, and an edited applied migration is refused (FR-17).
ALTER TABLE execution_manifests
    ADD COLUMN IF NOT EXISTS scheduler_limits JSONB;
