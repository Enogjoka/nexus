-- NEXUS Task 9: the economic calendar store -- arms validator RULE 1's
-- event blackout.
--
-- Idempotency comes from schema_migrations (this file runs exactly once,
-- recorded by filename) -- no IF NOT EXISTS gymnastics needed.
--
-- UNIQUE(event_ts, name) lets a later pass fill in `actual` after a release
-- via ON CONFLICT ... DO UPDATE SET actual = EXCLUDED.actual.

CREATE TABLE econ_events (
    id          BIGSERIAL PRIMARY KEY,
    event_ts    TIMESTAMPTZ NOT NULL,
    name        TEXT NOT NULL,
    currency    VARCHAR(5),
    impact      VARCHAR(10),
    actual      TEXT,
    forecast    TEXT,
    previous    TEXT,
    fetched_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (event_ts, name)
);
