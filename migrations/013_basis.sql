-- NEXUS Task 20: the futures/spot basis series.
--
-- basis = COMEX gold future (GC=F) minus spot (XAUUSD=X). The two track each
-- other closely because they are the same metal; the spread between them moves
-- with carry, funding and the occasional dislocation. S3 trades the reversion
-- of that spread, so this table is the pod's entire evidence base and its band
-- is only as good as the history stored here.
--
-- UNIQUE(ts) makes a re-poll at the same instant idempotent rather than a
-- duplicate reading that would quietly tighten the band.
--
-- Every price column is NULLable: a leg that fails to fetch yields NULL, never
-- a fabricated price. A row with a NULL basis is a recorded absence, which is
-- worth keeping — it says the sensor ran and the market did not answer.
--
-- Idempotency comes from schema_migrations (this file runs exactly once,
-- recorded by filename) -- no IF NOT EXISTS gymnastics needed.

CREATE TABLE basis_readings (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL UNIQUE,
    gc_price    NUMERIC(12,5),
    spot_price  NUMERIC(12,5),
    basis       NUMERIC(10,5),
    fetched_at  TIMESTAMPTZ DEFAULT NOW()
);

-- The band query is always "the most recent N readings".
CREATE INDEX idx_basis_readings_ts ON basis_readings (ts DESC);
