-- NEXUS Task 8: the positioning sensor's stores -- who is actually long gold.
--
-- Idempotency comes from schema_migrations (this file runs exactly once,
-- recorded by filename) -- no IF NOT EXISTS gymnastics needed.
--
-- etf_holdings carries gld_close/gld_volume alongside gld_tonnes: real GLD
-- tonnage requires a source this task deliberately does not build (see
-- sensors/positioning.py's HONEST SCOPE REDUCTION docstring) -- gld_tonnes
-- stays NULL until that source lands. gld_close/gld_volume are persisted now
-- as a flow proxy in the meantime (architect-approved extension of the
-- originally-specified single-column table).

CREATE TABLE cot_reports (
    id             BIGSERIAL PRIMARY KEY,
    report_date    DATE NOT NULL UNIQUE,
    mm_long        BIGINT,
    mm_short       BIGINT,
    mm_net         BIGINT,
    open_interest  BIGINT,
    fetched_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE etf_holdings (
    id          BIGSERIAL PRIMARY KEY,
    ts          DATE NOT NULL UNIQUE,
    gld_tonnes  NUMERIC(10,2),
    gld_close   NUMERIC(10,2),
    gld_volume  BIGINT,
    fetched_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE comex_stocks (
    id              BIGSERIAL PRIMARY KEY,
    ts              DATE NOT NULL UNIQUE,
    registered_oz   NUMERIC(16,2),
    eligible_oz     NUMERIC(16,2),
    fetched_at      TIMESTAMPTZ DEFAULT NOW()
);
