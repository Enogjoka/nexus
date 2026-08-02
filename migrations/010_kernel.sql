-- NEXUS Task 14: the sovereign risk kernel's black box.
--
-- Ring 0 writes one row here for EVERY decision it makes -- allows, denials,
-- clamps, flattens, halts, demotions. "No silent verdicts" is the rule: if the
-- kernel let an order through, or stopped one, there is a row saying so and
-- why. This table is how a human answers "what was the machine thinking?"
-- after the fact.
--
-- AUDIT ONLY, never an input. risk/kernel.py never reads this table; every
-- decision comes from injected callables, config, and risk/stage.py. A row
-- here cannot change what the kernel does, and writing one is best-effort --
-- a dead database costs the audit trail, never the safety check (INVARIANT 6).
--
-- Idempotency comes from schema_migrations (this file runs exactly once,
-- recorded by filename) -- no IF NOT EXISTS gymnastics needed.

CREATE TABLE kernel_events (
    id      BIGSERIAL PRIMARY KEY,
    ts      TIMESTAMPTZ DEFAULT NOW(),
    breaker TEXT NOT NULL,   -- which breaker decided; 'NONE' when nothing tripped
    action  TEXT NOT NULL,   -- ALLOW | DENY | CLAMP | FLATTEN | HALT | DEMOTE
    reason  TEXT,
    context JSONB            -- order fields + the numbers the decision turned on
);

-- The two questions actually asked of this table: "what happened just now?"
-- and "how often has breaker X fired?".
CREATE INDEX idx_kernel_events_ts ON kernel_events (ts DESC);
CREATE INDEX idx_kernel_events_breaker ON kernel_events (breaker, action);
