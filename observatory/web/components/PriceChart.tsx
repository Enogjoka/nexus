"use client";

import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";

import { clusterMarkers, fmtStamp, openRouter, openSwing, parseMs, type MarkerCluster, type Timeframe } from "@/lib/logic";
import type { Candle, RouterRow, SwingRow } from "@/lib/types";

const COLORS = {
  panel: "#0e1219",
  grid: "#161c26",
  text: "#8f98a8",
  up: "#1f8a52",
  upWick: "#2fb36d",
  down: "#a8323a",
  downWick: "#d9434d",
  entry: "#e6e9ef",
  router: "#38bdf8",
  stop: "#ef4444",
  be: "#9ca3af",
  tp: "#22c55e",
};

const TONE_COLOR = { stop: COLORS.stop, be: COLORS.be, tp: COLORS.tp, flat: COLORS.be } as const;

function toMarker(c: MarkerCluster): SeriesMarker<Time> {
  const long = c.direction === "LONG";
  const text = c.count > 1 ? `×${c.count}` : "";
  if (c.kind === "entry") {
    return {
      time: c.time as UTCTimestamp,
      position: long ? "belowBar" : "aboveBar",
      shape: c.book === "router" ? "square" : long ? "arrowUp" : "arrowDown",
      color: c.book === "router" ? COLORS.router : COLORS.entry,
      text,
      size: c.count > 1 ? 1.4 : 1,
    };
  }
  return {
    time: c.time as UTCTimestamp,
    position: long ? "aboveBar" : "belowBar",
    shape: "circle",
    color: TONE_COLOR[c.tone ?? "flat"],
    text,
  };
}

function describe(c: MarkerCluster): string {
  const who = c.book === "router" ? "router" : "swing";
  const what =
    c.kind === "entry"
      ? `${c.count} ${c.direction} ${who} entr${c.count === 1 ? "y" : "ies"}`
      : `${c.count} ${who} exit${c.count === 1 ? "" : "s"} (${c.tone === "tp" ? "take profit" : c.tone === "be" ? "break-even" : c.tone === "stop" ? "stopped" : "flat"})`;
  const ids = c.ids.slice(0, 6).map((id) => `#${id}`).join(" ");
  return `${what} · ${ids}${c.ids.length > 6 ? " …" : ""}`;
}

export default function PriceChart({
  tf,
  candles,
  swing,
  router,
}: {
  tf: Timeframe;
  candles: Candle[];
  swing: SwingRow[];
  router: RouterRow[];
}) {
  const box = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const linesRef = useRef<IPriceLine[]>([]);
  const fittedFor = useRef<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);

  const bars = useMemo(() => {
    const out: { time: UTCTimestamp; open: number; high: number; low: number; close: number }[] = [];
    for (const c of candles) {
      const ms = parseMs(c.ts);
      if (ms === null) continue;
      const time = Math.floor(ms / 1000) as UTCTimestamp;
      if (out.length && time <= out[out.length - 1].time) continue; // strictly ascending
      out.push({ time, open: c.open, high: c.high, low: c.low, close: c.close });
    }
    return out;
  }, [candles]);

  const clusters = useMemo(
    () => clusterMarkers(swing, router, bars.map((b) => b.time), tf),
    [swing, router, bars, tf],
  );

  // Build the chart once.
  useEffect(() => {
    if (!box.current) return;
    const chart = createChart(box.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: COLORS.panel },
        textColor: COLORS.text,
        fontFamily: '"JetBrains Mono Variable", ui-monospace, monospace',
        fontSize: 11,
      },
      grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
      rightPriceScale: { borderColor: "#1d2531" },
      timeScale: { borderColor: "#1d2531", timeVisible: true, secondsVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: COLORS.up,
      downColor: COLORS.down,
      borderUpColor: COLORS.upWick,
      borderDownColor: COLORS.downWick,
      wickUpColor: COLORS.upWick,
      wickDownColor: COLORS.downWick,
      priceLineVisible: false,
    });
    chartRef.current = chart;
    seriesRef.current = series;
    markersRef.current = createSeriesMarkers(series, []);
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      markersRef.current = null;
      linesRef.current = [];
    };
  }, []);

  // Data, markers and open-position price lines.
  useEffect(() => {
    const series = seriesRef.current;
    const chart = chartRef.current;
    if (!series || !chart) return;
    series.setData(bars);
    markersRef.current?.setMarkers(clusters.map(toMarker));

    for (const line of linesRef.current) series.removePriceLine(line);
    linesRef.current = [];
    const addLine = (price: number | null, color: string, title: string) => {
      if (price === null || !Number.isFinite(price)) return;
      linesRef.current.push(
        series.createPriceLine({ price, color, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title }),
      );
    };
    for (const r of openSwing(swing)) {
      addLine(r.stop, COLORS.stop, `#${r.id} SL`);
      addLine(r.tp1, COLORS.tp, `#${r.id} TP1`);
      addLine(r.tp2, COLORS.tp, `#${r.id} TP2`);
    }
    for (const r of openRouter(router)) {
      const tag = r.client_order_id ?? `#${r.id}`;
      addLine(r.stop_px, COLORS.stop, `${tag} SL`);
      addLine(r.tp1_px, COLORS.tp, `${tag} TP1`);
      addLine(r.tp2_px, COLORS.tp, `${tag} TP2`);
    }

    if (bars.length && fittedFor.current !== tf) {
      chart.timeScale().fitContent();
      fittedFor.current = tf;
    }
  }, [bars, clusters, swing, router, tf]);

  // Explain clusters under the crosshair: the count badge alone is not enough.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const onMove = (param: { time?: Time }) => {
      if (param.time === undefined) {
        setHover(null);
        return;
      }
      const at = clusters.filter((c) => c.time === param.time);
      setHover(at.length ? at.map(describe).join("  |  ") : null);
    };
    chart.subscribeCrosshairMove(onMove);
    return () => chart.unsubscribeCrosshairMove(onMove);
  }, [clusters]);

  const firstBar = bars.length ? new Date(bars[0].time * 1000).toISOString() : null;

  return (
    <div className="relative">
      <div ref={box} className="h-[340px] w-full md:h-[440px]" />
      {bars.length === 0 && (
        <div className="absolute inset-0 flex items-center justify-center text-xs text-faint">
          No candles for this timeframe.
        </div>
      )}
      <div className="mt-2 min-h-[1.25rem] text-[11px] text-muted">
        {hover ?? (
          <span className="text-faint">
            ▲▼ entries (×n = clone cluster on one bar) · ● exits: red stopped, grey break-even, green take-profit ·
            ■ router book · dashed lines = open-position stop / targets
            {firstBar ? ` · from ${fmtStamp(firstBar)} UTC` : ""}
          </span>
        )}
      </div>
    </div>
  );
}
