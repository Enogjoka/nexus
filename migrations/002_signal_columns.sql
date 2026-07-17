-- NEXUS Task 6: pay down the Task 5 JSONB debt and add the paper-engine
-- outcome-tracking columns.
--
-- Idempotency is provided by schema_migrations (run_migrations() records each
-- filename exactly once); this file is written to run a single time, not to be
-- defensively re-runnable. Do NOT add IF NOT EXISTS gymnastics.
--
-- The three "debt paydown" columns (execution_mode, lots, validator_warnings)
-- promote fields the Task 5 analyst only had a home for inside market_snapshot
-- JSONB. The five outcome columns (filled_at, outcome_r, outcome_pips, mae_r,
-- mfe_r) are what the paper engine writes as it tracks a signal to close;
-- without them acceptance C's SELECT could not run. (Architect-approved
-- extension of the originally-specified three columns.)

ALTER TABLE signals
    ADD COLUMN execution_mode     TEXT,
    ADD COLUMN lots               NUMERIC(6,2),
    ADD COLUMN validator_warnings JSONB,
    ADD COLUMN filled_at          TIMESTAMPTZ,
    ADD COLUMN outcome_r          NUMERIC,
    ADD COLUMN outcome_pips       NUMERIC,
    ADD COLUMN mae_r              NUMERIC,
    ADD COLUMN mfe_r              NUMERIC;

-- Backfill the three debt-paydown columns from the market_snapshot JSONB the
-- analyst wrote (execution_mode marker, sized_lots, validator_warnings).
UPDATE signals
   SET execution_mode     = COALESCE(execution_mode, market_snapshot->>'execution_mode'),
       lots               = COALESCE(lots, (market_snapshot->>'sized_lots')::numeric),
       validator_warnings = COALESCE(validator_warnings, market_snapshot->'validator_warnings')
 WHERE market_snapshot IS NOT NULL;

ALTER TABLE signals ALTER COLUMN execution_mode SET DEFAULT 'PAPER';
