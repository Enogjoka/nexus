-- NEXUS Task 12: the learning loop's output table.
--
-- One row per (run, dim): the Spearman rank correlation between a numeric
-- state-vector dim and the realized outcome_r of the signals born into that
-- state. This is how the system starts studying itself.
--
-- Rank (not Pearson) correlation on purpose: the relationship between a macro
-- dim and R is monotonic at best, rarely linear, and rank is robust to the
-- outliers a stop-loss distribution guarantees.
--
-- Idempotency comes from schema_migrations (this file runs exactly once,
-- recorded by filename). UNIQUE(computed_at, dim) additionally makes a
-- re-run of the same nightly job idempotent.

CREATE TABLE dim_rankings (
    id           BIGSERIAL PRIMARY KEY,
    computed_at  TIMESTAMPTZ NOT NULL,
    dim          TEXT NOT NULL,
    spearman     NUMERIC(6,4),
    n_samples    INT,
    UNIQUE (computed_at, dim)
);

CREATE INDEX IF NOT EXISTS idx_dim_rankings_computed_at ON dim_rankings (computed_at DESC);
