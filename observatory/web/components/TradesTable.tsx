"use client";

import { ChevronDown } from "lucide-react";
import { Fragment, useState } from "react";

import { fmtStamp, formatPrice, formatR, rTone } from "@/lib/logic";
import type { RouterRow, SwingRow, TradesResponse } from "@/lib/types";

import { Card, Empty, Loading } from "./Card";

const TONE = { profit: "text-profit", loss: "text-loss", flat: "text-flat", none: "text-faint" } as const;

const STATUS_STYLE: Record<string, string> = {
  PENDING: "text-muted",
  OPEN: "text-ring1",
  STOPPED: "text-loss",
  BE: "text-flat",
  TP2: "text-profit",
  EXPIRED: "text-faint",
};

function SwingDetail({ row }: { row: SwingRow }) {
  return (
    <div className="grid gap-3 bg-panel2/50 px-3 py-3 text-xs md:grid-cols-[2fr_1fr]">
      <div>
        <div className="mb-1 text-[10px] uppercase tracking-[0.12em] text-faint">Thesis (first 300 chars)</div>
        <p className="leading-relaxed text-ink/90">{row.thesis?.trim() || "No thesis recorded."}</p>
      </div>
      <dl className="num grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
        <dt className="text-faint">TP1 / TP2</dt>
        <dd className="text-right">{formatPrice(row.tp1)} / {formatPrice(row.tp2)}</dd>
        <dt className="text-faint" title="H1 bar on which the paper engine saw the fill — not a wall-clock fill time (F-39)">Fill bar time</dt>
        <dd className="text-right">{fmtStamp(row.bar_time)}</dd>
        <dt className="text-faint" title="H1 bar on which the close was seen (F-39)">Close bar time</dt>
        <dd className="text-right">{fmtStamp(row.close_bar_time)}</dd>
        <dt className="text-faint">MAE / MFE</dt>
        <dd className="text-right">{formatR(row.mae_r)} / {formatR(row.mfe_r)}</dd>
        <dt className="text-faint">Confidence</dt>
        <dd className="text-right">{row.confidence ?? "—"}</dd>
        <dt className="text-faint">Doctrine at signal</dt>
        <dd className="text-right">
          {row.doctrine_bias ? `${row.doctrine_bias} ×${row.doctrine_risk_multiplier ?? "—"}` : "not recorded"}
        </dd>
      </dl>
    </div>
  );
}

function SwingTable({ rows }: { rows: SwingRow[] }) {
  const [open, setOpen] = useState<number | null>(null);
  if (rows.length === 0) return <Empty>No swing signals in this window.</Empty>;
  return (
    <div className="scroll-thin max-h-[480px] overflow-auto">
      <table className="w-full min-w-[560px] text-xs">
        <thead className="sticky top-0 bg-panel text-[10px] uppercase tracking-wider text-faint">
          <tr>
            <th className="py-1.5 text-left font-normal">Signal time</th>
            <th className="text-left font-normal">Dir</th>
            <th className="text-left font-normal">Grade</th>
            <th className="text-right font-normal">Entry</th>
            <th className="text-right font-normal">Stop</th>
            <th className="text-right font-normal">R</th>
            <th className="pr-1 text-right font-normal">Status</th>
            <th className="w-5" />
          </tr>
        </thead>
        <tbody className="num">
          {rows.map((r) => {
            const expanded = open === r.id;
            return (
              <Fragment key={r.id}>
                <tr
                  onClick={() => setOpen(expanded ? null : r.id)}
                  className={`cursor-pointer border-t border-line/60 hover:bg-panel2/60 ${expanded ? "bg-panel2/60" : ""}`}
                >
                  <td className="py-1.5 text-muted">
                    {fmtStamp(r.ts)} <span className="text-faint">#{r.id}</span>
                  </td>
                  <td className={r.direction === "LONG" ? "text-profit" : "text-loss"}>{r.direction}</td>
                  <td>{r.grade ?? "—"}</td>
                  <td className="text-right">{formatPrice(r.entry)}</td>
                  <td className="text-right text-muted">{formatPrice(r.stop)}</td>
                  <td className={`text-right ${TONE[rTone(r.outcome_r)]}`}>{formatR(r.outcome_r)}</td>
                  <td className={`pr-1 text-right ${STATUS_STYLE[r.status] ?? "text-muted"}`}>{r.status}</td>
                  <td>
                    <ChevronDown className={`size-3.5 text-faint transition ${expanded ? "rotate-180" : ""}`} aria-hidden />
                  </td>
                </tr>
                {expanded && (
                  <tr>
                    <td colSpan={8} className="p-0">
                      <SwingDetail row={r} />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RouterTable({ rows }: { rows: RouterRow[] }) {
  if (rows.length === 0) return <Empty>No router positions in this window.</Empty>;
  return (
    <div className="scroll-thin overflow-auto">
      <table className="w-full min-w-[560px] text-xs">
        <thead className="text-[10px] uppercase tracking-wider text-faint">
          <tr>
            <th className="py-1.5 text-left font-normal">Opened</th>
            <th className="text-left font-normal">Dir</th>
            <th className="text-left font-normal">Pod</th>
            <th className="text-right font-normal">Entry</th>
            <th className="text-right font-normal">Stop</th>
            <th className="text-right font-normal">P&amp;L $</th>
            <th className="text-right font-normal">State</th>
          </tr>
        </thead>
        <tbody className="num">
          {rows.map((r) => (
            <tr key={r.id} className="border-t border-line/60">
              <td className="py-1.5 text-muted">{fmtStamp(r.opened_at ?? r.created_at)}</td>
              <td className={r.direction === "LONG" ? "text-profit" : "text-loss"}>{r.direction ?? "—"}</td>
              <td className="text-muted">{r.pod ?? r.source ?? "—"}</td>
              <td className="text-right">{formatPrice(r.entry_px)}</td>
              <td className="text-right text-muted">{formatPrice(r.stop_px)}</td>
              <td className={`text-right ${TONE[rTone(r.realized_pnl_usd)]}`}>
                {r.realized_pnl_usd === null ? "—" : r.realized_pnl_usd.toFixed(2)}
              </td>
              <td className="text-right text-muted">{r.state ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function TradesTable({ trades }: { trades: TradesResponse | null }) {
  if (!trades) {
    return (
      <Card title="Trades">
        <Loading />
      </Card>
    );
  }
  return (
    <div className="grid gap-3">
      <Card
        title="Swing book"
        hint={`paper engine · ${trades.swing.count} signals · ${trades.window_days} d · tap a row for its thesis`}
      >
        <SwingTable rows={trades.swing.rows} />
        {trades.swing.truncated && <p className="mt-2 text-[11px] text-warn">Showing the newest 1,000 only.</p>}
      </Card>
      <Card title="Router book" hint={`pods via router → kernel · ${trades.router.count} positions`}>
        <RouterTable rows={trades.router.rows} />
      </Card>
    </div>
  );
}
