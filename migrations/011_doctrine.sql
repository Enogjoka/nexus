-- NEXUS Task 15: the doctrine store -- Ring 2's output, caged.
--
-- A doctrine is a POSTURE, not an order: a bias, a conviction, a risk
-- multiplier, and which pods may run. There is no price, no lot size and no
-- entry anywhere in this table, and that absence is the point (INVARIANT 2 --
-- the AI never emits raw prices or raw orders).
--
-- `source` records WHO produced the row, which is the column an auditor reads
-- first:
--   FABLE            -- the model answered and its answer validated
--   PARSE_FALLBACK   -- the model failed, timed out, or emitted something that
--                       did not validate; a FLAT doctrine was substituted
--   EXPIRY_FALLBACK  -- no doctrine was issued in time; the held one went stale
-- Both fallback sources are FLAT by construction. A dense run of them is how
-- "the head of desk has been silent" becomes visible after the fact.
--
-- raw_response keeps the model's untouched text even when parsing failed --
-- that string is the only evidence of WHY a fallback happened.
--
-- Idempotency comes from schema_migrations (this file runs exactly once,
-- recorded by filename) -- no IF NOT EXISTS gymnastics needed.

CREATE TABLE doctrines (
    id                    BIGSERIAL PRIMARY KEY,
    ts                    TIMESTAMPTZ NOT NULL,
    regime                TEXT,
    bias                  TEXT NOT NULL,
    conviction            INT,
    risk_multiplier       NUMERIC(4,3),
    enabled_pods          TEXT[],
    swing_signals_allowed BOOLEAN,
    no_trade_reason       TEXT,
    review_horizon_min    INT,
    state_vector_id       BIGINT REFERENCES state_vectors(id),
    source                TEXT NOT NULL,
    raw_response          TEXT,
    created_at            TIMESTAMPTZ DEFAULT NOW()
);

-- "What is the current posture?" and "how often did we fall back?"
CREATE INDEX idx_doctrines_ts ON doctrines (ts DESC);
CREATE INDEX idx_doctrines_source ON doctrines (source);
