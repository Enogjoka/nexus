// Shapes of the O2 Observatory API (observatory/queries.py, observatory/events.py).
// Every timestamp is an ISO 8601 UTC string; every number arrives as a plain
// JSON number (the API converts Decimal before it leaves the server).

export type Ring = 0 | 1 | 2 | 3;
export type Severity = "debug" | "info" | "warn" | "error" | "critical";

export interface Doctrine {
  id: number;
  ts: string;
  bias: "LONG_ONLY" | "SHORT_ONLY" | "BOTH" | "FLAT" | string;
  conviction: number | null;
  risk_multiplier: number | null;
  enabled_pods: string[];
  swing_signals_allowed: boolean | null;
  source: "FABLE" | "PARSE_FALLBACK" | "EXPIRY_FALLBACK" | string;
  no_trade_reason: string | null;
  review_horizon_min: number | null;
  expires_at: string | null;
  seconds_to_expiry: number | null;
  expired: boolean;
  api_failed: boolean;
}

export interface StageEvent {
  id: number;
  ts: string;
  event: string;
  env_stage: string | null;
  effective_stage: string | null;
  reason: string | null;
}

export interface HeartbeatView {
  ts: string;
  age_s: number;
  stage: string | null;
  alive: number | null;
  registered: number | null;
  dead: number | null;
  rss_mb: number | null;
  last_market_update_at: string | null;
  model_ok_at: string | null;
  budget_today_usd: number | null;
}

export interface SensorFreshness {
  age_at_heartbeat_s: number | null;
  age_now_s: number | null;
}

export interface StateResponse {
  as_of: string;
  stage: { heartbeat: string | null; last_stage_event: StageEvent | null; note: string };
  doctrine: Doctrine | null;
  heartbeat: HeartbeatView | null;
  freshness: Record<string, SensorFreshness> | null;
  kill_switch: { state: "unknown" | string; note: string };
  missing: Record<string, string> | null;
}

export interface SwingRow {
  id: number;
  ts: string;
  symbol: string;
  direction: "LONG" | "SHORT" | string;
  grade: string | null;
  confidence: number | null;
  status: "PENDING" | "OPEN" | "STOPPED" | "BE" | "TP2" | "EXPIRED" | string;
  lots: number | null;
  thesis: string | null;
  doctrine_bias: string | null;
  doctrine_risk_multiplier: number | null;
  entry: number | null;
  stop: number | null;
  tp1: number | null;
  tp2: number | null;
  bar_time: string | null;
  close_bar_time: string | null;
  outcome_r: number | null;
  mae_r: number | null;
  mfe_r: number | null;
}

export interface RouterRow {
  id: number;
  client_order_id: string | null;
  source: string | null;
  pod: string | null;
  direction: string | null;
  lots: number | null;
  entry_px: number | null;
  stop_px: number | null;
  tp1_px: number | null;
  tp2_px: number | null;
  state: string | null;
  opened_at: string | null;
  closed_at: string | null;
  realized_pnl_usd: number | null;
  close_reason: string | null;
  created_at: string | null;
}

export interface BookBlock<T> {
  book: string;
  count: number;
  truncated: boolean;
  rows: T[];
}

export interface TradesResponse {
  window_days: number;
  since: string;
  until: string;
  swing: BookBlock<SwingRow>;
  router: BookBlock<RouterRow>;
  notes: Record<string, string>;
}

export interface PerfBlock {
  label: string;
  n: number;
  r_sum: number;
  wins: number;
  win_rate: number | null;
  wilson95: [number | null, number | null];
  expectancy_r?: number | null;
}

export interface PerfResponse {
  window: "day" | "week" | "all";
  since: string | null;
  until: string;
  swing: {
    rows: PerfBlock;
    decisions: PerfBlock;
    statistically_meaningful: boolean;
    note: string | null;
    expired_unfilled: number | null;
    truncated: boolean;
    window_basis: string;
  };
  router: { label: string; n: number; pnl_usd: number; wins: number; wilson95: [number | null, number | null] };
}

export interface ObsEvent {
  id: number;
  ts: string | null;
  ring: Ring;
  agent: string;
  event_type: string;
  severity: Severity;
  correlation_id: string | null;
  trade_id: string | null;
  summary: string;
  payload: Record<string, unknown>;
}

export interface EventsResponse {
  epoch: string;
  last_event_id: number;
  count: number;
  events: ObsEvent[];
}

export interface Candle {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
  is_anomaly: boolean | null;
}

export interface CandlesResponse {
  symbol: string;
  tf: "1h" | "4h" | "1d";
  since: string;
  count: number;
  candles: Candle[];
}
