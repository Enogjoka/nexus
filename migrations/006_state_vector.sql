-- NEXUS Task 10: the fusion state vector -- one wide typed row per hour
-- carrying everything the sensors know.
--
-- Deliberately NOT JSONB: the learning loop (Task 12) runs per-column
-- correlations over this table, which wants real typed columns and indexes,
-- not JSON extraction on every scan.
--
-- Every column is NULLable on purpose: a sensor that is down/absent yields a
-- NULL, never a fabricated value. UNIQUE(ts) + hour-truncated timestamps give
-- one row per hour.
--
-- Idempotency comes from schema_migrations (this file runs exactly once,
-- recorded by filename) -- no IF NOT EXISTS gymnastics needed.

CREATE TABLE state_vectors (
    id                          BIGSERIAL PRIMARY KEY,
    ts                          TIMESTAMPTZ NOT NULL UNIQUE,

    -- L0 price / microstructure
    price                       NUMERIC(12,5),
    atr_h1                      NUMERIC(12,5),
    atr_h4                      NUMERIC(12,5),
    rsi_h1                      NUMERIC(6,3),
    rsi_h4                      NUMERIC(6,3),
    rsi_d1                      NUMERIC(6,3),
    bb_position_h1              NUMERIC(6,3),
    regime_h1                   VARCHAR(15),
    regime_h4                   VARCHAR(15),
    regime_d1                   VARCHAR(15),
    session                     VARCHAR(10),
    session_high                NUMERIC(12,5),
    session_low                 NUMERIC(12,5),

    -- L1 macro
    real_yield                  NUMERIC(8,4),
    real_yield_5d_delta         NUMERIC(8,4),
    curve_2s10s                 NUMERIC(8,4),
    breakeven_10y               NUMERIC(8,4),
    dxy                         NUMERIC(8,3),
    dxy_trend                   VARCHAR(5),

    -- L2 positioning
    cot_mm_net                  BIGINT,
    cot_mm_net_pctile           NUMERIC(6,2),
    comex_coverage              NUMERIC(8,4),

    -- L4 news / L5 time
    news_heat                   NUMERIC(6,4),
    minutes_to_next_high_event  NUMERIC(8,1),
    fix_window                  BOOLEAN,

    assembled_at                TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_state_vectors_ts ON state_vectors (ts DESC);
