-- NEXUS Task 7: the macro sensor's observation store.
--
-- Idempotency comes from schema_migrations (this file runs exactly once,
-- recorded by filename) — no IF NOT EXISTS gymnastics needed.

CREATE TABLE macro_observations (
    id          BIGSERIAL PRIMARY KEY,
    series      VARCHAR(20) NOT NULL,
    ts          DATE NOT NULL,
    value       NUMERIC(14,5),
    fetched_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (series, ts)
);
