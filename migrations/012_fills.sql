-- NEXUS Task 16: the fills ledger -- what Ring 1's hands actually did.
--
-- One row per order EVENT, not per signal. A signal that is denied by the
-- kernel still lands here with status REJECTED and the kernel's reason: an
-- order we refused to send is as much a fact about the system as one we sent,
-- and a ledger that only records successes cannot answer "why didn't we
-- trade?".
--
-- client_order_id is UNIQUE and that uniqueness IS the idempotency mechanism.
-- The router derives it deterministically from the signal id (NEXUS-<id>), so
-- a retried or double-invoked submit collides here rather than opening a
-- second position. Closes are recorded under a derived '<id>-CLOSE' key so the
-- opening fill's price and slippage survive intact alongside the closing one.
--
-- slippage is stored SIGNED BY DIRECTION so that positive always means
-- adverse, whichever way the trade faced. Averaging the column is therefore
-- meaningful without a CASE on direction.
--
-- Idempotency comes from schema_migrations (this file runs exactly once,
-- recorded by filename) -- no IF NOT EXISTS gymnastics needed.

CREATE TABLE fills (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    signal_id       BIGINT,
    direction       TEXT,
    lots            NUMERIC(6,2),
    requested_px    NUMERIC(12,5),
    fill_px         NUMERIC(12,5),
    slippage        NUMERIC(10,5),   -- positive = adverse, both directions
    spread_at_send  NUMERIC(10,5),
    fill_mode       TEXT NOT NULL,   -- MODELED | SHADOW_REAL_BIDASK | LIVE_*
    status          TEXT NOT NULL,   -- FILLED | REJECTED | CLOSED
    kernel_reason   TEXT,            -- populated on REJECTED
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_fills_ts ON fills (ts DESC);
CREATE INDEX idx_fills_signal ON fills (signal_id);
CREATE INDEX idx_fills_status ON fills (status);
