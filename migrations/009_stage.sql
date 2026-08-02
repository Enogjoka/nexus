-- NEXUS Task 13: the stage governor's audit trail.
--
-- Every boot records which stage the environment asked for and which stage
-- the process actually adopted. When those two disagree the reason is on the
-- row, so "why was this trade MODELED when NEXUS_STAGE said SHADOW?" is
-- answerable months later without reading a log file that has since rotated.
--
-- This table is an AUDIT LOG, never a source of truth: risk/stage.py reads
-- the environment and the demotion flag file, never this table. Config is not
-- stored in the database (see core/database.py) and the stage ladder is no
-- exception -- a row here can never change how the process behaves.
--
-- Idempotency comes from schema_migrations (this file runs exactly once,
-- recorded by filename) -- no IF NOT EXISTS gymnastics needed.

CREATE TABLE stage_events (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ DEFAULT NOW(),
    event           TEXT NOT NULL,   -- BOOT | DEMOTION_WRITTEN
    env_stage       TEXT,            -- what NEXUS_STAGE asked for
    effective_stage TEXT,            -- what the process actually adopted
    reason          TEXT             -- demotion reason; NULL on an undemoted boot
);

-- Boot rows accumulate one per process start, so the common query is "what
-- happened most recently".
CREATE INDEX idx_stage_events_ts ON stage_events (ts DESC);
