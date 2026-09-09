-- FR-21: a run may record the run that spawned it.
--
-- Phase 2 runs subagents. A subagent is a run, and without this column its
-- rows are indistinguishable from a top-level run's -- the parent's trace
-- cannot be reconstructed together with its children's, which is exactly what
-- NFR-3 promises for a single run and Phase 2 needs for a tree of them.
--
-- Nullable by construction: a top-level run has no parent, and that is the
-- common case rather than an exception to model around.
--
-- ADR-11 is NOT relaxed here. A child run carries its own tenant_id and
-- project_id like every other row; the parent link is a lineage fact, never a
-- substitute for tenancy, and nothing may infer a child's tenant from its
-- parent's row.
ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS parent_run_id UUID REFERENCES runs (run_id);

-- The read pattern this exists for: "everything spawned by this run".
CREATE INDEX IF NOT EXISTS runs_parent
    ON runs (parent_run_id)
    WHERE parent_run_id IS NOT NULL;
