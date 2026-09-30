import { describe, expect, it } from "vitest";

import {
  aiLight,
  clusterMarkers,
  computeStaleness,
  expiry,
  fmtDuration,
  formatR,
  rTone,
  sensorLevel,
  snapToCandle,
} from "./logic";
import type { RouterRow, SwingRow } from "./types";

const T0 = Date.parse("2026-09-30T12:00:00Z");
const iso = (ms: number) => new Date(ms).toISOString();
const sec = (ms: number) => Math.floor(ms / 1000);

// --------------------------------------------------------------------------

describe("computeStaleness", () => {
  const fresh = { nowMs: T0, lastApiOkMs: T0 - 5_000, apiError: false };

  it("claims nothing before the first response", () => {
    const s = computeStaleness({ nowMs: T0, lastApiOkMs: null, apiError: false, heartbeatTs: null });
    expect(s.stale).toBe(false);
  });

  it("is live with a recent heartbeat", () => {
    const s = computeStaleness({ ...fresh, heartbeatTs: iso(T0 - 30_000) });
    expect(s).toMatchObject({ stale: false, reason: null });
  });

  it("goes stale strictly past 90 s of heartbeat silence", () => {
    expect(computeStaleness({ ...fresh, heartbeatTs: iso(T0 - 90_000) }).stale).toBe(false);
    const s = computeStaleness({ ...fresh, heartbeatTs: iso(T0 - 91_000) });
    expect(s).toMatchObject({ stale: true, reason: "heartbeat_old", lastGoodMs: T0 - 91_000 });
    expect(s.message).toBe("Trading process silent — last heartbeat 1m 31s ago");
  });

  it("is stale when there has never been a heartbeat", () => {
    const s = computeStaleness({ ...fresh, heartbeatTs: null });
    expect(s).toMatchObject({ stale: true, reason: "no_heartbeat", lastGoodMs: null });
  });

  it("is stale when the API fails, and says how old the last good data is", () => {
    const s = computeStaleness({
      nowMs: T0,
      lastApiOkMs: T0 - 192_000,
      apiError: true,
      heartbeatTs: iso(T0 - 10_000),
    });
    expect(s).toMatchObject({ stale: true, reason: "api_unreachable", lastGoodMs: T0 - 192_000 });
    expect(s.message).toBe("API unreachable — last good data 3m 12s ago");
  });

  it("is stale when the API has not answered for longer than the threshold", () => {
    const s = computeStaleness({ nowMs: T0, lastApiOkMs: T0 - 120_000, apiError: false, heartbeatTs: iso(T0) });
    expect(s.reason).toBe("api_unreachable");
  });

  it("reports an API that never answered", () => {
    const s = computeStaleness({ nowMs: T0, lastApiOkMs: null, apiError: true, heartbeatTs: null });
    expect(s.message).toBe("API unreachable — no data received yet");
  });
});

// --------------------------------------------------------------------------

describe("aiLight", () => {
  const at = (minutesAgo: number) => iso(T0 - minutesAgo * 60_000);

  it("is unknown without a heartbeat, never green", () => {
    expect(aiLight(false, at(1), T0).level).toBe("unknown");
  });

  it("is red when no successful call has ever been recorded", () => {
    expect(aiLight(true, null, T0).level).toBe("red");
  });

  it("applies the 30 / 120 minute thresholds", () => {
    expect(aiLight(true, at(0), T0).level).toBe("green");
    expect(aiLight(true, at(29.99), T0).level).toBe("green");
    expect(aiLight(true, at(30), T0).level).toBe("amber");
    expect(aiLight(true, at(119.99), T0).level).toBe("amber");
    expect(aiLight(true, at(120), T0).level).toBe("red");
    expect(aiLight(true, at(60 * 24 * 4), T0).level).toBe("red");
  });

  it("labels the age of the last success", () => {
    expect(aiLight(true, at(45), T0).label).toBe("Last successful AI call 45m 00s ago");
  });
});

// --------------------------------------------------------------------------

describe("expiry countdown", () => {
  it("counts down in m:ss under an hour", () => {
    expect(expiry(iso(T0 + 754_000), T0)).toEqual({ expired: false, remainingS: 754, text: "12:34" });
  });

  it("counts down in h:mm:ss over an hour", () => {
    expect(expiry(iso(T0 + 3_729_000), T0).text).toBe("1:02:09");
  });

  it("reads 0:00 at the expiry instant and EXPIRED one second later", () => {
    expect(expiry(iso(T0), T0)).toMatchObject({ expired: false, text: "0:00" });
    expect(expiry(iso(T0 - 1000), T0)).toMatchObject({ expired: true, text: "EXPIRED 1s ago" });
  });

  it("says how long ago it expired", () => {
    expect(expiry(iso(T0 - 84_522_000), T0).text).toBe("EXPIRED 23h 28m ago");
  });

  it("shows nothing for a missing expiry", () => {
    expect(expiry(null, T0)).toEqual({ expired: false, remainingS: null, text: "—" });
  });
});

describe("fmtDuration", () => {
  it("scales units", () => {
    expect(fmtDuration(45)).toBe("45s");
    expect(fmtDuration(192)).toBe("3m 12s");
    expect(fmtDuration(4 * 3600 + 5 * 60)).toBe("4h 05m");
    expect(fmtDuration(2 * 86400 + 3 * 3600)).toBe("2d 03h");
    expect(fmtDuration(-5)).toBe("0s");
  });
});

// --------------------------------------------------------------------------

describe("formatR", () => {
  it("signs and rounds to two decimals with a true minus", () => {
    expect(formatR(1.25)).toBe("+1.25R");
    expect(formatR(-1)).toBe("−1.00R");
    expect(formatR(-65.45)).toBe("−65.45R");
    expect(formatR(0.004)).toBe("0.00R");
    expect(formatR(-0.004)).toBe("0.00R");
  });

  it("never invents a number", () => {
    expect(formatR(null)).toBe("—");
    expect(formatR(undefined)).toBe("—");
    expect(formatR(Number.NaN)).toBe("—");
  });

  it("tones profit, loss and flat", () => {
    expect(rTone(3)).toBe("profit");
    expect(rTone(-1)).toBe("loss");
    expect(rTone(0.001)).toBe("flat");
    expect(rTone(null)).toBe("none");
  });
});

// --------------------------------------------------------------------------

describe("sensorLevel", () => {
  it("uses per-sensor limits and admits ignorance", () => {
    expect(sensorLevel("candles", 3600)).toBe("fresh");
    expect(sensorLevel("candles", 3 * 3600)).toBe("stale");
    expect(sensorLevel("cot_reports", 5 * 86400)).toBe("fresh");
    expect(sensorLevel("news_articles", null)).toBe("unknown");
    expect(sensorLevel("not_a_sensor", 10)).toBe("unknown");
  });
});

// --------------------------------------------------------------------------

describe("snapToCandle", () => {
  const H = 3600;
  const times = [0, H, 2 * H, 3 * H].map((t) => t + sec(T0));

  it("finds the containing candle", () => {
    expect(snapToCandle(times, sec(T0) + H + 1800, H)).toBe(sec(T0) + H);
    expect(snapToCandle(times, sec(T0) + 2 * H, H)).toBe(sec(T0) + 2 * H);
  });

  it("drops times outside the loaded range", () => {
    expect(snapToCandle(times, sec(T0) - 1, H)).toBeNull();
    expect(snapToCandle(times, sec(T0) + 3 * H + 1800, H)).toBe(sec(T0) + 3 * H);
    expect(snapToCandle(times, sec(T0) + 4 * H, H)).toBeNull();
    expect(snapToCandle([], sec(T0), H)).toBeNull();
  });
});

describe("clusterMarkers", () => {
  const H = 3600;
  const base = sec(Date.parse("2026-09-21T00:00:00Z"));
  const candleTimes = Array.from({ length: 12 }, (_, i) => base + i * H);
  const bar = (h: number) => new Date((base + h * H) * 1000).toISOString();

  const swing = (id: number, over: Partial<SwingRow>): SwingRow => ({
    id, ts: bar(0), symbol: "GC=F", direction: "LONG", grade: "A", confidence: 80, status: "STOPPED",
    lots: 0.02, thesis: null, doctrine_bias: null, doctrine_risk_multiplier: null, entry: 1, stop: 1,
    tp1: 1, tp2: 1, bar_time: null, close_bar_time: null, outcome_r: null, mae_r: null, mfe_r: null, ...over,
  });

  it("stacks a clone batch into one entry marker with a count", () => {
    // The 2026-09-21 03:00 batch: 25 LONG entries on one bar, all stopped on
    // two later bars.
    const rows = Array.from({ length: 25 }, (_, i) =>
      swing(5320 + i, { bar_time: bar(3), close_bar_time: bar(i < 20 ? 6 : 9), status: "STOPPED" }),
    );
    const markers = clusterMarkers(rows, [], candleTimes, "1h");
    expect(markers).toEqual([
      { time: base + 3 * H, kind: "entry", book: "swing", direction: "LONG", tone: null, count: 25, ids: rows.map((r) => r.id) },
      { time: base + 6 * H, kind: "exit", book: "swing", direction: "LONG", tone: "stop", count: 20, ids: rows.slice(0, 20).map((r) => r.id) },
      { time: base + 9 * H, kind: "exit", book: "swing", direction: "LONG", tone: "stop", count: 5, ids: rows.slice(20).map((r) => r.id) },
    ]);
  });

  it("separates directions and outcomes on the same bar", () => {
    const rows = [
      swing(1, { direction: "SHORT", bar_time: bar(1), close_bar_time: bar(4), status: "TP2" }),
      swing(2, { direction: "SHORT", bar_time: bar(1), close_bar_time: bar(4), status: "BE" }),
      swing(3, { direction: "LONG", bar_time: bar(1), close_bar_time: bar(4), status: "STOPPED" }),
    ];
    const markers = clusterMarkers(rows, [], candleTimes, "1h");
    expect(markers.filter((m) => m.kind === "entry").map((m) => [m.direction, m.count])).toEqual([
      ["SHORT", 2],
      ["LONG", 1],
    ]);
    expect(markers.filter((m) => m.kind === "exit").map((m) => m.tone).sort()).toEqual(["be", "stop", "tp"]);
  });

  it("never marks an exit for a signal that expired unfilled or is still open", () => {
    const rows = [
      swing(1, { status: "EXPIRED", bar_time: null, close_bar_time: bar(5) }),
      swing(2, { status: "OPEN", bar_time: bar(2), close_bar_time: null }),
    ];
    const markers = clusterMarkers(rows, [], candleTimes, "1h");
    expect(markers).toHaveLength(1);
    expect(markers[0]).toMatchObject({ kind: "entry", ids: [2] });
  });

  it("merges H1 entries into their H4 candle", () => {
    const h4Times = [base, base + 4 * H, base + 8 * H];
    const rows = [swing(1, { bar_time: bar(4) }), swing(2, { bar_time: bar(6) }), swing(3, { bar_time: bar(9) })];
    const markers = clusterMarkers(rows, [], h4Times, "4h");
    expect(markers.map((m) => [m.time, m.count])).toEqual([
      [base + 4 * H, 2],
      [base + 8 * H, 1],
    ]);
  });

  it("marks router trades as their own book, toned by realised P&L", () => {
    const router: RouterRow[] = [
      {
        id: 9, client_order_id: "NEXUS-1", source: "POD", pod: "S1_FIXFADE", direction: "SHORT", lots: 0.01,
        entry_px: 1, stop_px: 1, tp1_px: 1, tp2_px: 1, state: "CLOSED", opened_at: bar(2),
        closed_at: bar(3), realized_pnl_usd: -4.2, close_reason: "STOP", created_at: bar(2),
      },
    ];
    const markers = clusterMarkers([], router, candleTimes, "1h");
    expect(markers.map((m) => [m.book, m.kind, m.tone])).toEqual([
      ["router", "entry", null],
      ["router", "exit", "stop"],
    ]);
  });
});
