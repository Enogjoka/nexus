-- NEXUS Task O1: the Observatory foundation — process health, one row per
-- supervisor poll.
--
-- Everything else the Observatory shows is already in the audit trail
-- (validator_log, doctrines, signals, positions, fills, kernel_events,
-- stage_events, command_audit, the sensor tables). Process health lived only
-- in memory; this table is the one new writer (backend.py, fire-and-forget).
--
-- model_ok_at is the LATER of the analyst's last successful model call and
-- the newest FABLE doctrine, so a dead model is visible even while the
-- doctrine gate keeps the analyst silent. `sensors` maps sensor name ->
-- seconds since that sensor's freshest row (null when unknown).
--
-- Rows older than config.HEARTBEAT_RETENTION_DAYS are pruned once a day.

CREATE TABLE IF NOT EXISTS ops_heartbeats (
    id                    BIGSERIAL PRIMARY KEY,
    ts                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stage                 TEXT,
    alive                 INT,
    registered            INT,
    dead                  INT,
    rss_mb                NUMERIC(10,1),
    last_market_update_at TIMESTAMPTZ,
    model_ok_at           TIMESTAMPTZ,
    doctrine_bias         TEXT,
    doctrine_source       TEXT,
    doctrine_age_s        INT,
    budget_today_usd      NUMERIC(10,4),
    sensors               JSONB
);

CREATE INDEX IF NOT EXISTS idx_ops_heartbeats_ts ON ops_heartbeats (ts DESC);
