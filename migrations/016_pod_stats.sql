-- NEXUS Task 23: per-pod rolling performance.
--
-- One row per pod per nightly run. This is the desk's memory of which of its
-- own strategies actually works, and it is what the doctrine reads before
-- deciding which pods to enable — closing the pod_stats=None gap Task 15 left
-- open.
--
-- A ZERO-TRADE ROW IS STILL WRITTEN. "This pod did nothing for two weeks" is a
-- fact the head of desk needs, and it is invisible if the absence of trades is
-- also an absence of rows. n_trades = 0 with NULL rates is the honest shape;
-- inventing a win rate for a pod that never traded would be worse than silence.
--
-- Cost columns are NULLABLE on purpose. Costs are joined from the fills ledger;
-- a position whose fills row is missing yields NULL, never an estimate. A
-- fabricated cost would flow straight into cost_drag_pct and from there into a
-- decision about whether a strategy pays for itself.
--
-- UNIQUE(computed_at, pod) makes a re-run of the same nightly idempotent.
--
-- Idempotency comes from schema_migrations (this file runs exactly once,
-- recorded by filename) -- no IF NOT EXISTS gymnastics needed.

CREATE TABLE pod_stats (
    id                     BIGSERIAL PRIMARY KEY,
    computed_at            TIMESTAMPTZ NOT NULL,
    pod                    TEXT NOT NULL,
    window_days            INT,
    n_trades               INT,
    wins                   INT,
    expectancy_usd         NUMERIC(12,4),
    wilson_lb              NUMERIC(6,4),
    gross_pnl_usd          NUMERIC(12,2),
    total_costs_usd        NUMERIC(12,2),
    cost_drag_pct          NUMERIC(6,2),
    max_consecutive_losses INT,
    UNIQUE (computed_at, pod)
);

CREATE INDEX idx_pod_stats_pod_ts ON pod_stats (pod, computed_at DESC);
