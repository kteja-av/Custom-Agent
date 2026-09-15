-- FR-51 (M12): runs.status admits 'cancelled' (P2-D3, DECISION-a44db7e4).
--
-- schema.sql declares the status CHECK inline and unnamed, so its name is whatever
-- Postgres generated for it: runs_status_check on every database measured, but an
-- assumed name is exactly the kind of guess a migration must not rest on. So every
-- CHECK constraint on runs.status is found by its definition -- the column it
-- constrains -- and dropped, and one constraint is added under that name admitting
-- 'cancelled' beside the existing statuses. Idempotent (FR-17): run again, it finds
-- the constraint it added, drops it and adds the same one.
--
-- run_events.event_type has no constraint, so RunCancelled needs no migration.
-- schema.sql is NOT edited. It is migration 0001, its checksum is recorded on every
-- database, and an edited applied migration is refused (FR-17).
DO $$
DECLARE
    found record;
BEGIN
    FOR found IN
        SELECT DISTINCT c.conname
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
        WHERE c.conrelid = 'runs'::regclass
          AND c.contype = 'c'
          AND a.attname = 'status'
    LOOP
        EXECUTE format('ALTER TABLE runs DROP CONSTRAINT %I', found.conname);
    END LOOP;
END
$$;

ALTER TABLE runs ADD CONSTRAINT runs_status_check
    CHECK (status IN ('running', 'completed', 'failed', 'max_turns_exceeded', 'cancelled'));
