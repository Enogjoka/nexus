"use client";

import { AlertOctagon, Clock3 } from "lucide-react";

import { useNow } from "@/lib/clock";
import { useData } from "@/lib/data";
import {
  ago,
  expiry,
  fmtStamp,
  formatPct,
  formatPrice,
  formatR,
  openRouter,
  openSwing,
  pendingSwing,
  rTone,
} from "@/lib/logic";
import type { Doctrine, PerfBlock, PerfResponse } from "@/lib/types";

import { Card, Empty, Loading, Stat } from "./Card";
import { EventList } from "./EventList";

const TONE = { profit: "text-profit", loss: "text-loss", flat: "text-flat", none: "text-faint" } as const;

const SOURCE_LABEL: Record<string, string> = {
  FABLE: "AI strategist (Fable)",
  PARSE_FALLBACK: "fallback: unparseable or failed call",
  EXPIRY_FALLBACK: "fallback: previous doctrine expired",
};

const BIAS_STYLE: Record<string, string> = {
  FLAT: "text-muted",
  BOTH: "text-ring2",
  LONG_ONLY: "text-profit",
  SHORT_ONLY: "text-loss",
};

// --------------------------------------------------------------------------
// doctrine
// --------------------------------------------------------------------------

export function DoctrineCard({ className = "" }: { className?: string }) {
  const { state, serverOffsetMs } = useData();
  const now = useNow();
  const d: Doctrine | null = state.data?.doctrine ?? null;

  if (!state.data) {
    return (
      <Card title="Doctrine" className={className}>
        <Loading />
      </Card>
    );
  }
  if (!d) {
    return (
      <Card title="Doctrine" className={className}>
        <Empty>No doctrine has been issued.</Empty>
      </Card>
    );
  }

  const exp = now ? expiry(d.expires_at, now + serverOffsetMs) : null;

  return (
    <Card
      title="Doctrine"
      hint={`issued ${fmtStamp(d.ts)} UTC`}
      className={className}
      right={
        exp && (
          <span
            className={`num flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] ${
              exp.expired ? "bg-warn/15 text-warn" : "bg-panel2 text-ink"
            }`}
          >
            <Clock3 className="size-3" aria-hidden />
            {exp.text}
          </span>
        )
      }
    >
      {d.api_failed ? (
        <div className="mb-3 flex items-start gap-2.5 rounded-md border border-loss/40 bg-loss/10 p-3">
          <AlertOctagon className="mt-0.5 size-5 shrink-0 text-loss" aria-hidden />
          <div>
            <div className="text-sm font-semibold text-loss">AI unavailable</div>
            <p className="text-xs text-muted">
              The model call failed; this is a FLAT safety fallback, not a model decision.
              {now ? ` Since ${ago(d.ts, now + serverOffsetMs)}.` : ""}
            </p>
          </div>
        </div>
      ) : (
        <div className="mb-3 flex items-baseline gap-3">
          <span className={`text-2xl font-semibold tracking-tight ${BIAS_STYLE[d.bias] ?? "text-ink"}`}>{d.bias}</span>
          <span className="text-xs text-muted">{SOURCE_LABEL[d.source] ?? d.source}</span>
        </div>
      )}

      <div className="grid grid-cols-3 gap-3">
        <Stat label="Conviction" value={d.conviction === null ? "—" : `${d.conviction}/10`} />
        <Stat label="Risk ×" value={d.risk_multiplier === null ? "—" : d.risk_multiplier.toFixed(2)} />
        <Stat
          label="Swing allowed"
          value={d.swing_signals_allowed === null ? "—" : d.swing_signals_allowed ? "yes" : "no"}
          tone={d.swing_signals_allowed ? "text-profit" : "text-muted"}
        />
        <Stat label="Pods" value={d.enabled_pods.length ? d.enabled_pods.join(", ") : "none"} />
        <Stat label="Horizon" value={d.review_horizon_min === null ? "—" : `${d.review_horizon_min} min`} />
        <Stat label="Source" value={d.source} />
      </div>
      {d.no_trade_reason && (
        <p className="mt-3 border-t border-line pt-2 text-xs text-muted">
          <span className="text-faint">Reason: </span>
          {d.no_trade_reason}
        </p>
      )}
    </Card>
  );
}

// --------------------------------------------------------------------------
// performance: rows AND decisions
// --------------------------------------------------------------------------

function PerfColumn({ block, kind }: { block: PerfBlock; kind: "rows" | "decisions" }) {
  const [lo, hi] = block.wilson95;
  return (
    <div className="min-w-0 rounded-md border border-line bg-panel2/40 p-2.5">
      <div className="mb-1.5 flex items-baseline justify-between gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">{kind}</span>
        <span className="num text-[11px] text-faint">n = {block.n}</span>
      </div>
      <div className={`num text-lg font-semibold ${TONE[rTone(block.r_sum)]}`}>{formatR(block.r_sum)}</div>
      <div className="num mt-0.5 text-[11px] text-muted">
        win {formatPct(block.win_rate, 0)}
        {lo !== null && hi !== null && (
          <span className="text-faint">
            {" "}
            · 95% {formatPct(lo, 0)}–{formatPct(hi, 0)}
          </span>
        )}
      </div>
    </div>
  );
}

function PerfWindow({ title, perf }: { title: string; perf: PerfResponse | null }) {
  if (!perf) return <Loading />;
  const swing = perf.swing;
  return (
    <div className="min-w-0">
      <div className="mb-2 flex items-baseline justify-between">
        <h3 className="text-xs font-semibold text-ink">{title}</h3>
        <span className="text-[10px] text-faint">swing book · by close bar time</span>
      </div>
      {swing.rows.n === 0 ? (
        <Empty>No closed swing trades in this window.</Empty>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2">
            <PerfColumn block={swing.rows} kind="rows" />
            <PerfColumn block={swing.decisions} kind="decisions" />
          </div>
          {swing.note && <p className="mt-2 text-[11px] text-warn/90">{swing.note}</p>}
        </>
      )}
      <p className="mt-2 text-[11px] text-faint">
        Router book: {perf.router.n === 0 ? "no closed positions" : `${perf.router.n} closed · $${perf.router.pnl_usd.toFixed(2)}`}
        {swing.expired_unfilled ? ` · ${swing.expired_unfilled} signals expired unfilled` : ""}
      </p>
    </div>
  );
}

export function PerfCard({ className = "" }: { className?: string }) {
  const { perfDay, perfWeek } = useData();
  return (
    <Card title="Performance" hint="R · rows are not independent; decisions are" className={className}>
      <div className="grid gap-4 sm:grid-cols-2">
        <PerfWindow title="Last 24 h" perf={perfDay.data} />
        <PerfWindow title="Last 7 days" perf={perfWeek.data} />
      </div>
    </Card>
  );
}

// --------------------------------------------------------------------------
// open positions: both books
// --------------------------------------------------------------------------

export function PositionsCard({ className = "" }: { className?: string }) {
  const { trades } = useData();
  const t = trades.data;
  if (!t) {
    return (
      <Card title="Open positions" className={className}>
        <Loading />
      </Card>
    );
  }
  const swing = openSwing(t.swing.rows);
  const pending = pendingSwing(t.swing.rows);
  const router = openRouter(t.router.rows);

  return (
    <Card title="Open positions" hint="last 7 days of signals" className={className}>
      <div className="space-y-3">
        <div>
          <h3 className="mb-1.5 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-ring1">
            Swing book <span className="font-normal normal-case tracking-normal text-faint">paper engine</span>
          </h3>
          {swing.length === 0 ? (
            <Empty>No open swing positions.</Empty>
          ) : (
            <table className="w-full text-xs">
              <thead className="text-[10px] uppercase tracking-wider text-faint">
                <tr>
                  <th className="py-1 text-left font-normal">#</th>
                  <th className="text-left font-normal">Dir</th>
                  <th className="text-right font-normal">Entry</th>
                  <th className="text-right font-normal">Stop</th>
                  <th className="text-right font-normal">TP2</th>
                  <th className="text-right font-normal">Bar time</th>
                </tr>
              </thead>
              <tbody className="num">
                {swing.map((r) => (
                  <tr key={r.id} className="border-t border-line/60">
                    <td className="py-1 text-faint">{r.id}</td>
                    <td className={r.direction === "LONG" ? "text-profit" : "text-loss"}>{r.direction}</td>
                    <td className="text-right">{formatPrice(r.entry)}</td>
                    <td className="text-right text-loss/90">{formatPrice(r.stop)}</td>
                    <td className="text-right text-profit/90">{formatPrice(r.tp2)}</td>
                    <td className="text-right text-muted">{fmtStamp(r.bar_time)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {pending.length > 0 && (
            <p className="mt-1 text-[11px] text-faint">
              {pending.length} pending signal{pending.length === 1 ? "" : "s"} waiting for entry (not positions).
            </p>
          )}
        </div>
        <div>
          <h3 className="mb-1.5 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-ring1">
            Router book <span className="font-normal normal-case tracking-normal text-faint">pods via kernel</span>
          </h3>
          {router.length === 0 ? (
            <Empty>No open router positions.</Empty>
          ) : (
            <ul className="num space-y-1 text-xs">
              {router.map((r) => (
                <li key={r.id} className="flex justify-between gap-2 border-t border-line/60 pt-1">
                  <span className="truncate text-muted">{r.client_order_id}</span>
                  <span className={r.direction === "LONG" ? "text-profit" : "text-loss"}>{r.direction}</span>
                  <span>{formatPrice(r.entry_px)}</span>
                  <span className="text-faint">{r.state}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Card>
  );
}

// --------------------------------------------------------------------------
// latest events
// --------------------------------------------------------------------------

export function LatestEventsCard({ className = "" }: { className?: string }) {
  const { events, socket } = useData();
  const latest = events.slice(-10).reverse();
  return (
    <Card title="Latest events" hint={`feed ${socket}`} className={className}>
      <EventList events={latest} empty="No events received yet." />
    </Card>
  );
}
