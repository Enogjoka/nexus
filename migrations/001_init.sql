-- NEXUS Task 0: initial schema.
-- Hard architectural rule: no system_config / config table exists here.
-- Configuration lives in config.py; secrets live in the environment only.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS candles (
    id          BIGSERIAL PRIMARY KEY,
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    open        NUMERIC NOT NULL,
    high        NUMERIC NOT NULL,
    low         NUMERIC NOT NULL,
    close       NUMERIC NOT NULL,
    volume      NUMERIC,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, timeframe, ts)
);

CREATE INDEX IF NOT EXISTS idx_candles_symbol_timeframe_ts
    ON candles (symbol, timeframe, ts DESC);

CREATE TABLE IF NOT EXISTS indicator_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    indicators  JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, timeframe, ts)
);

CREATE INDEX IF NOT EXISTS idx_indicator_snapshots_symbol_timeframe_ts
    ON indicator_snapshots (symbol, timeframe, ts DESC);

CREATE TABLE IF NOT EXISTS signals (
    id                  BIGSERIAL PRIMARY KEY,
    ts                  TIMESTAMPTZ NOT NULL,
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL,
    anchors             JSONB,
    prices              JSONB,
    grade               TEXT,
    confidence          NUMERIC,
    thesis              TEXT,
    market_snapshot     JSONB,
    embedding           JSONB,
    outcome_hit         BOOLEAN,
    outcome_pnl         NUMERIC,
    outcome_ts          TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'PENDING',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts ON signals (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals (status);

CREATE TABLE IF NOT EXISTS validator_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT NOT NULL,
    rule_name   TEXT NOT NULL,
    rule_result TEXT NOT NULL,
    details     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_validator_log_symbol_ts ON validator_log (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_validator_log_rule_name ON validator_log (rule_name);

CREATE TABLE IF NOT EXISTS news_articles (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    source      TEXT NOT NULL,
    headline    TEXT NOT NULL,
    url         TEXT,
    sentiment   NUMERIC,
    raw         JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_news_articles_ts ON news_articles (ts DESC);
