-- NEXUS Task 0: initial schema.
-- Hard architectural rule: no system_config / config table exists here.
-- Configuration lives in config.py; secrets live in the environment only.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS candles (
    id          BIGSERIAL PRIMARY KEY,
    symbol      VARCHAR(10) NOT NULL,
    timeframe   VARCHAR(5) NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    open        NUMERIC(12,5) NOT NULL,
    high        NUMERIC(12,5) NOT NULL,
    low         NUMERIC(12,5) NOT NULL,
    close       NUMERIC(12,5) NOT NULL,
    volume      BIGINT,
    is_anomaly  BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (symbol, timeframe, ts)
);

CREATE INDEX IF NOT EXISTS idx_candles_symbol_timeframe_ts
    ON candles (symbol, timeframe, ts DESC);

CREATE TABLE IF NOT EXISTS indicator_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    symbol      VARCHAR(10) NOT NULL,
    timeframe   VARCHAR(5) NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    ema20       NUMERIC(12,5),
    ema50       NUMERIC(12,5),
    rsi14       NUMERIC(6,3),
    atr14       NUMERIC(12,5),
    swing_high  NUMERIC(12,5),
    swing_low   NUMERIC(12,5),
    regime      VARCHAR(15),
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
    id            BIGSERIAL PRIMARY KEY,
    url_hash      CHAR(16) UNIQUE NOT NULL,
    url           TEXT,
    source        TEXT,
    title         TEXT,
    summary       TEXT,
    published_at  TIMESTAMPTZ,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    relevance     SMALLINT,
    direction     TEXT,
    magnitude     SMALLINT,
    confidence    SMALLINT,
    thesis        TEXT,
    tags          TEXT[]
);

CREATE INDEX IF NOT EXISTS idx_news_articles_published_at ON news_articles (published_at DESC);
