"use client";

import { fmtClock, fmtStamp } from "@/lib/logic";
import { RINGS, SEVERITY_STYLE } from "@/lib/rings";
import type { ObsEvent } from "@/lib/types";

import { Empty } from "./Card";

export function EventRow({ event, fresh = false }: { event: ObsEvent; fresh?: boolean }) {
  const ring = RINGS[event.ring] ?? RINGS[0];
  return (
    <li className={`group flex gap-2.5 py-1.5 ${fresh ? "bg-panel2/70" : ""}`}>
      <span className="mt-0.5 w-0.5 shrink-0 self-stretch rounded" style={{ background: ring.color }} aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-[10px] text-faint">
          <span className="num" title={event.ts ?? undefined}>
            {fmtStamp(event.ts)}
            <span className="hidden sm:inline">:{fmtClock(event.ts).slice(-2)}</span>
          </span>
          <span style={{ color: ring.color }} className="uppercase tracking-wider">
            {event.agent}
          </span>
          <span className={`rounded border px-1 py-px text-[9px] uppercase tracking-wider ${SEVERITY_STYLE[event.severity]}`}>
            {event.severity}
          </span>
        </div>
        <p className="mt-0.5 break-words text-xs leading-snug text-ink/90">{event.summary}</p>
      </div>
    </li>
  );
}

export function EventList({
  events,
  empty = "No events yet.",
  freshIds,
}: {
  events: ObsEvent[];
  empty?: string;
  freshIds?: Set<number>;
}) {
  if (events.length === 0) return <Empty>{empty}</Empty>;
  return (
    <ul className="divide-y divide-line/60">
      {events.map((e) => (
        <EventRow key={e.id} event={e} fresh={freshIds?.has(e.id)} />
      ))}
    </ul>
  );
}
