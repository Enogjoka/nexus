"use client";

import { useState } from "react";

import { Card } from "@/components/Card";
import EventFeed from "@/components/EventFeed";
import PriceChart from "@/components/PriceChart";
import TradesTable from "@/components/TradesTable";
import { POLL_MS, useData, usePoll } from "@/lib/data";
import type { Timeframe } from "@/lib/logic";
import type { CandlesResponse, TradesResponse } from "@/lib/types";

// Chart window per timeframe. The trades API serves at most 90 days.
const WINDOW_DAYS: Record<Timeframe, number> = { "1h": 14, "4h": 60, "1d": 90 };
const TF_LABEL: Record<Timeframe, string> = { "1h": "H1", "4h": "H4", "1d": "D1" };
const CANDLE_POLL_MS = 60_000;

export default function TradesPage() {
  const { token, onAuthFail } = useData();
  const [tf, setTf] = useState<Timeframe>("1h");
  const days = WINDOW_DAYS[tf];

  const candles = usePoll<CandlesResponse>(`/api/v1/candles?tf=${tf}&days=${days}`, token, CANDLE_POLL_MS, onAuthFail);
  const trades = usePoll<TradesResponse>(`/api/v1/trades?days=${days}`, token, POLL_MS, onAuthFail);
  const chartReady = candles.data?.tf === tf;

  return (
    <div className="mx-auto grid max-w-7xl gap-3 xl:grid-cols-[minmax(0,1fr)_380px]">
      <div className="grid min-w-0 gap-3">
        <Card
          title={`${candles.data?.symbol ?? "GC=F"} · ${TF_LABEL[tf]}`}
          hint={`last ${days} days · UTC`}
          right={
            <div className="flex overflow-hidden rounded-md border border-line2" role="group" aria-label="Timeframe">
              {(Object.keys(TF_LABEL) as Timeframe[]).map((key) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => setTf(key)}
                  className={`px-2.5 py-1 text-[11px] font-medium ${tf === key ? "bg-panel2 text-ink" : "text-muted hover:text-ink"}`}
                >
                  {TF_LABEL[key]}
                </button>
              ))}
            </div>
          }
        >
          {candles.error && !candles.data ? (
            <p className="py-10 text-center text-xs text-loss">Candles unavailable: {candles.error}</p>
          ) : (
            <PriceChart
              tf={tf}
              candles={chartReady ? candles.data!.candles : []}
              swing={trades.data?.swing.rows ?? []}
              router={trades.data?.router.rows ?? []}
            />
          )}
        </Card>
        <TradesTable trades={trades.data} />
      </div>
      <EventFeed className="xl:sticky xl:top-16 xl:self-start" />
    </div>
  );
}
