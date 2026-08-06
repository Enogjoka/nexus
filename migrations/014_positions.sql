-- NEXUS Task 21: the position ledger.
--
-- One row per position, for BOTH sources — swing signals and pod intents.
-- Two managers would eventually disagree about who owns a stop, and the
-- disagreement would surface as a position nobody closed.
--
-- THE ROW EXISTS BEFORE THE POSITION DOES. router.submit() inserts state
-- RESERVED before it touches the bridge, so client_order_id's UNIQUE
-- constraint IS the duplicate check: a concurrent or retried submit collides
-- here rather than opening a second position. This closes the Task 16 gap
-- where a fill could land while the ledger write failed, leaving an open
-- position the system had no record of.
--
-- state transitions:
--   RESERVED -> OPEN      the bridge filled it
--   RESERVED -> FAILED    kernel denied, or the bridge never filled
--   OPEN -> PARTIAL/BE    tp1 taken, half closed, stop moved to entry
--   BE -> TRAILING        stop now following price
--   any -> CLOSED         stop, tp2, doctrine flip or end-of-week flat
--
-- Idempotency comes from schema_migrations (this file runs exactly once,
-- recorded by filename) -- no IF NOT EXISTS gymnastics needed.

CREATE TABLE positions (
    id                BIGSERIAL PRIMARY KEY,
    client_order_id   TEXT NOT NULL UNIQUE,
    source            TEXT NOT NULL,          -- SWING | POD
    pod               TEXT,                   -- NULL for swing
    direction         TEXT,
    lots              NUMERIC(6,2),
    entry_px          NUMERIC(12,5),
    stop_px           NUMERIC(12,5),          -- the TRACKED stop; moves with BE/trail
    tp1_px            NUMERIC(12,5),
    tp2_px            NUMERIC(12,5),
    state             TEXT NOT NULL,          -- RESERVED/OPEN/PARTIAL/BE/TRAILING/CLOSED/FAILED
    opened_at         TIMESTAMPTZ,
    closed_at         TIMESTAMPTZ,
    realized_pnl_usd  NUMERIC(12,2),
    close_reason      TEXT,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- The engine's hot query is "everything still live".
CREATE INDEX idx_positions_state ON positions (state);
CREATE INDEX idx_positions_source ON positions (source, state);
