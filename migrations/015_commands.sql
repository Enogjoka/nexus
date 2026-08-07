-- NEXUS Task 22: the command audit trail.
--
-- Every command the bot sees lands here — accepted, rejected, expired, and
-- the ones from chat ids that are not allowed to speak to it at all. An audit
-- log that only records successful commands cannot answer the question you
-- actually ask it after an incident: "who else was trying?"
--
-- outcome vocabulary:
--   OK              the command ran
--   CONFIRM_SENT    a dangerous command asked for confirmation
--   CONFIRMED       the confirmation arrived and the action executed
--   EXPIRED         the confirmation window closed unused
--   REFUSED         a confirmation arrived from the wrong chat, or none pending
--   REJECTED        the chat id is not on the allowlist
--   UNKNOWN         no such command
--   ERROR           the command raised
--
-- `stage` is recorded per row rather than joined later: it is the single most
-- important piece of context for reading a destructive command months on, and
-- the stage a command ran under is not recoverable from anywhere else.
--
-- Idempotency comes from schema_migrations (this file runs exactly once,
-- recorded by filename) -- no IF NOT EXISTS gymnastics needed.

CREATE TABLE command_audit (
    id      BIGSERIAL PRIMARY KEY,
    ts      TIMESTAMPTZ DEFAULT NOW(),
    chat_id TEXT,
    command TEXT,
    args    TEXT,
    stage   TEXT,
    outcome TEXT,
    detail  TEXT
);

CREATE INDEX idx_command_audit_ts ON command_audit (ts DESC);
CREATE INDEX idx_command_audit_command ON command_audit (command, outcome);
