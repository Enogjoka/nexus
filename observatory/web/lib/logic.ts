// Pure logic behind every status the app shows. No React, no DOM, no clock of
// its own: every function takes `nowMs`, so each rule is unit-tested
// (lib/logic.test.ts) and the screens only render what these return.

import type { RouterRow, SwingRow } from "./types";

// --------------------------------------------------------------------------
// time formatting (all times in the app are UTC, labelled as such)
// --------------------------------------------------------------------------

export function parseMs(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

const pad = (n: number) => String(n).padStart(2, "0");

/** "09-25 11:53" (UTC). */
export function fmtStamp(iso: string | null | undefined): string {
  const ms = parseMs(iso);
  if (ms === null) return "—";
  const d = new Date(ms);
  return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

/** "11:53:08" (UTC). */
export function fmtClock(iso: string | null | undefined): string {
  const ms = parseMs(iso);
  if (ms === null) return "—";
  const d = new Date(ms);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}

/** A compact, human duration: 45s · 3m 12s · 4h 05m · 2d 03h. */
export function fmtDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${pad(s % 60)}s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${pad(m % 60)}m`;
  const d = Math.floor(h / 24);
  return `${d}d ${pad(h % 24)}h`;
}

export function ago(iso: string | null | undefined, nowMs: number): string | null {
  const ms = parseMs(iso);
  return ms === null ? null : `${fmtDuration((nowMs - ms) / 1000)} ago`;
}

// --------------------------------------------------------------------------
// numbers
// --------------------------------------------------------------------------

const MINUS = "−";

/** +1.25R · −1.00R · 0.00R · — (no value). Never invents a number. */
export function formatR(r: number | null | undefined): string {
  if (r === null || r === undefined || !Number.isFinite(r)) return "—";
  const rounded = Math.round(r * 100) / 100;
  if (rounded === 0) return "0.00R";
  return `${rounded > 0 ? "+" : MINUS}${Math.abs(rounded).toFixed(2)}R`;
}

export function rTone(r: number | null | undefined): "profit" | "loss" | "flat" | "none" {
  if (r === null || r === undefined || !Number.isFinite(r)) return "none";
  const rounded = Math.round(r * 100) / 100;
  return rounded > 0 ? "profit" : rounded < 0 ? "loss" : "flat";
}

export function formatPrice(p: number | null | undefined, digits = 2): string {
  return p === null || p === undefined || !Number.isFinite(p) ? "—" : p.toFixed(digits);
}

export function formatPct(x: number | null | undefined, digits = 1): string {
  return x === null || x === undefined || !Number.isFinite(x) ? "—" : `${(x * 100).toFixed(digits)}%`;
}

// --------------------------------------------------------------------------
// staleness — the red banner
// --------------------------------------------------------------------------

export const HEARTBEAT_STALE_S = 90;

export type StaleReason = "api_unreachable" | "no_heartbeat" | "heartbeat_old";

export interface Staleness {
  stale: boolean;
  reason: StaleReason | null;
  /** When the last good data arrived (ms), if known. */
  lastGoodMs: number | null;
  message: string | null;
}

/**
 * Stale when the API is unreachable (last request failed, or no success for
 * longer than the threshold), when the trading process has never written a
 * heartbeat, or when its newest heartbeat is older than the threshold.
 * Before the first response arrives nothing is claimed either way.
 */
export function computeStaleness(input: {
  nowMs: number;
  lastApiOkMs: number | null;
  apiError: boolean;
  heartbeatTs: string | null | undefined;
  thresholdS?: number;
}): Staleness {
  const threshold = (input.thresholdS ?? HEARTBEAT_STALE_S) * 1000;
  const { nowMs, lastApiOkMs, apiError } = input;

  if (apiError || (lastApiOkMs !== null && nowMs - lastApiOkMs > threshold)) {
    return {
      stale: true,
      reason: "api_unreachable",
      lastGoodMs: lastApiOkMs,
      message:
        lastApiOkMs === null
          ? "API unreachable — no data received yet"
          : `API unreachable — last good data ${fmtDuration((nowMs - lastApiOkMs) / 1000)} ago`,
    };
  }
  if (lastApiOkMs === null) {
    return { stale: false, reason: null, lastGoodMs: null, message: null };
  }
  const heartbeatMs = parseMs(input.heartbeatTs);
  if (heartbeatMs === null) {
    return {
      stale: true,
      reason: "no_heartbeat",
      lastGoodMs: null,
      message: "No heartbeat from the trading process — its health is unknown",
    };
  }
  if (nowMs - heartbeatMs > threshold) {
    return {
      stale: true,
      reason: "heartbeat_old",
      lastGoodMs: heartbeatMs,
      message: `Trading process silent — last heartbeat ${fmtDuration((nowMs - heartbeatMs) / 1000)} ago`,
    };
  }
  return { stale: false, reason: null, lastGoodMs: heartbeatMs, message: null };
}

// --------------------------------------------------------------------------
// AI status light
// --------------------------------------------------------------------------

export const AI_GREEN_MAX_S = 30 * 60;
export const AI_AMBER_MAX_S = 120 * 60;

export type AiLevel = "green" | "amber" | "red" | "unknown";

export interface AiLight {
  level: AiLevel;
  ageS: number | null;
  label: string;
}

/**
 * From heartbeat.model_ok_at: green under 30 min, amber under 120 min, red
 * beyond or when no successful call has ever been recorded. Without a
 * heartbeat there is no evidence either way: "unknown", never green.
 */
export function aiLight(heartbeatPresent: boolean, modelOkAt: string | null | undefined, nowMs: number): AiLight {
  if (!heartbeatPresent) {
    return { level: "unknown", ageS: null, label: "AI status unknown — no heartbeat" };
  }
  const okMs = parseMs(modelOkAt);
  if (okMs === null) {
    return { level: "red", ageS: null, label: "No successful AI call recorded" };
  }
  const ageS = Math.max(0, (nowMs - okMs) / 1000);
  const level: AiLevel = ageS < AI_GREEN_MAX_S ? "green" : ageS < AI_AMBER_MAX_S ? "amber" : "red";
  return { level, ageS, label: `Last successful AI call ${fmtDuration(ageS)} ago` };
}

// --------------------------------------------------------------------------
// doctrine expiry countdown
// --------------------------------------------------------------------------

export interface Expiry {
  expired: boolean;
  remainingS: number | null;
  text: string;
}

/** "12:04" / "1:02:09" while live; "EXPIRED 23h 28m ago" once past. */
export function expiry(expiresAt: string | null | undefined, nowMs: number): Expiry {
  const endMs = parseMs(expiresAt);
  if (endMs === null) return { expired: false, remainingS: null, text: "—" };
  const remainingS = Math.floor((endMs - nowMs) / 1000);
  if (remainingS < 0) {
    return { expired: true, remainingS, text: `EXPIRED ${fmtDuration(-remainingS)} ago` };
  }
  const h = Math.floor(remainingS / 3600);
  const m = Math.floor((remainingS % 3600) / 60);
  const s = remainingS % 60;
  return { expired: false, remainingS, text: h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}` };
}

// --------------------------------------------------------------------------
// sensor freshness (Agents screen)
// --------------------------------------------------------------------------

// How old each sensor's freshest row may be before its node turns amber.
// Cadences from the trading code: price every 300 s (candle ts is the bar
// OPEN, so an H1 candle is up to ~65 min old when healthy); state vector
// hourly; FRED is daily and skips weekends; COT is weekly; the calendar
// polls every 4 h; news polls continuously.
export const SENSOR_STALE_S: Record<string, number> = {
  candles: 2 * 3600,
  state_vectors: 2 * 3600,
  macro_observations: 4 * 86400,
  cot_reports: 8 * 86400,
  econ_events: 8 * 3600,
  news_articles: 2 * 3600,
};

export type SensorLevel = "fresh" | "stale" | "unknown";

export function sensorLevel(name: string, ageS: number | null | undefined): SensorLevel {
  if (ageS === null || ageS === undefined || !Number.isFinite(ageS)) return "unknown";
  const limit = SENSOR_STALE_S[name];
  if (limit === undefined) return "unknown";
  return ageS > limit ? "stale" : "fresh";
}

// --------------------------------------------------------------------------
// chart markers with clone clusters
// --------------------------------------------------------------------------

export const TF_SECONDS = { "1h": 3600, "4h": 4 * 3600, "1d": 86400 } as const;
export type Timeframe = keyof typeof TF_SECONDS;

/**
 * The candle a timestamp belongs to: the latest candle time <= t. Null when t
 * is before the first candle or more than one bar after the last one.
 * `times` must be ascending (seconds).
 */
export function snapToCandle(times: number[], t: number, barSeconds: number): number | null {
  if (times.length === 0 || t < times[0]) return null;
  const last = times[times.length - 1];
  if (t >= last) return t < last + barSeconds ? last : null;
  let lo = 0;
  let hi = times.length - 1;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (times[mid] <= t) lo = mid;
    else hi = mid - 1;
  }
  return times[lo];
}

export type MarkerKind = "entry" | "exit";
export type ExitTone = "stop" | "be" | "tp" | "flat";

export interface MarkerCluster {
  time: number;
  kind: MarkerKind;
  book: "swing" | "router";
  direction: string;
  tone: ExitTone | null;
  count: number;
  ids: number[];
}

const EXIT_TONE: Record<string, ExitTone> = { STOPPED: "stop", BE: "be", TP2: "tp" };

function routerExitTone(pnl: number | null): ExitTone {
  if (pnl === null || !Number.isFinite(pnl) || pnl === 0) return "flat";
  return pnl > 0 ? "tp" : "stop";
}

/**
 * Entry and exit markers, grouped: every trade that entered on the same candle
 * in the same direction is ONE marker with a count (a clone cluster, F-03);
 * exits group by candle and outcome. Sorted by time, as the chart requires.
 */
export function clusterMarkers(
  swing: SwingRow[],
  router: RouterRow[],
  candleTimes: number[],
  tf: Timeframe,
): MarkerCluster[] {
  const bar = TF_SECONDS[tf];
  const groups = new Map<string, MarkerCluster>();
  const add = (
    iso: string | null,
    kind: MarkerKind,
    book: "swing" | "router",
    direction: string,
    tone: ExitTone | null,
    id: number,
  ) => {
    const ms = parseMs(iso);
    if (ms === null) return;
    const time = snapToCandle(candleTimes, Math.floor(ms / 1000), bar);
    if (time === null) return;
    const key = `${time}|${kind}|${book}|${direction}|${tone ?? ""}`;
    const existing = groups.get(key);
    if (existing) {
      existing.count += 1;
      existing.ids.push(id);
    } else {
      groups.set(key, { time, kind, book, direction, tone, count: 1, ids: [id] });
    }
  };

  for (const row of swing) {
    if (row.bar_time) add(row.bar_time, "entry", "swing", row.direction, null, row.id);
    const tone = EXIT_TONE[row.status];
    if (tone && row.close_bar_time) add(row.close_bar_time, "exit", "swing", row.direction, tone, row.id);
  }
  for (const row of router) {
    const direction = row.direction ?? "?";
    if (row.opened_at) add(row.opened_at, "entry", "router", direction, null, row.id);
    if (row.closed_at) add(row.closed_at, "exit", "router", direction, routerExitTone(row.realized_pnl_usd), row.id);
  }
  return [...groups.values()].sort(
    (a, b) => a.time - b.time || (a.kind === b.kind ? 0 : a.kind === "entry" ? -1 : 1),
  );
}

// --------------------------------------------------------------------------
// books
// --------------------------------------------------------------------------

export const ROUTER_LIVE_STATES = ["OPEN", "PARTIAL", "BE", "TRAILING"];

export function openSwing(rows: SwingRow[]): SwingRow[] {
  return rows.filter((r) => r.status === "OPEN");
}

export function pendingSwing(rows: SwingRow[]): SwingRow[] {
  return rows.filter((r) => r.status === "PENDING");
}

export function openRouter(rows: RouterRow[]): RouterRow[] {
  return rows.filter((r) => r.state !== null && ROUTER_LIVE_STATES.includes(r.state));
}
