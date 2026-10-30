-- NEXUS Task O2b: the agent action log.
--
-- Every agent already narrates what it does through Python logging. One
-- handler in backend.py (AgentLogHandler, attached only by main()) copies each
-- INFO+ record here through a bounded queue and a single writer thread, so no
-- agent module changes and no agent ever waits on this table.
--
-- `agent` is the display name mapped from `logger` (backend.AGENT_LOGGER_MAP;
-- unmapped loggers become "Other"). Messages are redacted (API keys, bot
-- token, HMAC secret, database password) and capped at 2,000 characters; an
-- exception contributes its type and message only, never a traceback.
--
-- Rows older than config.AGENT_LOG_RETENTION_DAYS are pruned once a day,
-- beside the ops_heartbeats pruning.

CREATE TABLE IF NOT EXISTS agent_log (
    id      BIGSERIAL PRIMARY KEY,
    ts      TIMESTAMPTZ NOT NULL,
    agent   TEXT NOT NULL,
    logger  TEXT NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL,
    thread  TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_log_ts ON agent_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_agent_log_agent_ts ON agent_log (agent, ts DESC);
