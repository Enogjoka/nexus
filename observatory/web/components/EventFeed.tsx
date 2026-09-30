"use client";

import { useMemo, useState } from "react";

import { useData } from "@/lib/data";
import { RINGS, RING_LIST, SEVERITY_ORDER, severityAtLeast } from "@/lib/rings";
import type { Ring, Severity } from "@/lib/types";

import { Card } from "./Card";
import { EventList } from "./EventList";

const SHOW = 100;

export default function EventFeed({ className = "" }: { className?: string }) {
  const { events, arrivals, socket } = useData();
  const [ring, setRing] = useState<Ring | "all">("all");
  const [floor, setFloor] = useState<Severity | "all">("info");

  const shown = useMemo(
    () =>
      events
        .filter((e) => (ring === "all" || e.ring === ring) && severityAtLeast(e.severity, floor))
        .slice(-SHOW)
        .reverse(),
    [events, ring, floor],
  );
  const freshIds = useMemo(() => new Set(arrivals.slice(-10).map((a) => a.event.id)), [arrivals]);

  return (
    <Card
      title="Live event feed"
      hint={`${socket} · ${events.length} buffered`}
      className={className}
      right={
        <select
          value={floor}
          onChange={(e) => setFloor(e.target.value as Severity | "all")}
          aria-label="Minimum severity"
          className="rounded border border-line2 bg-bg px-1.5 py-0.5 text-[11px] text-muted"
        >
          <option value="all">all severities</option>
          {SEVERITY_ORDER.map((s) => (
            <option key={s} value={s}>
              {s}+
            </option>
          ))}
        </select>
      }
    >
      <div className="mb-2 flex flex-wrap gap-1.5" role="group" aria-label="Ring filter">
        <button
          type="button"
          onClick={() => setRing("all")}
          className={`rounded border px-2 py-0.5 text-[11px] ${ring === "all" ? "border-ink/60 text-ink" : "border-line text-muted"}`}
        >
          All rings
        </button>
        {RING_LIST.map((r) => (
          <button
            key={r}
            type="button"
            onClick={() => setRing(r)}
            style={{ borderColor: ring === r ? RINGS[r].color : undefined, color: RINGS[r].color }}
            className={`rounded border px-2 py-0.5 text-[11px] ${ring === r ? "bg-panel2" : "border-line opacity-80"}`}
            title={RINGS[r].title}
          >
            {RINGS[r].name}
          </button>
        ))}
      </div>
      <div className="scroll-thin max-h-[520px] overflow-auto">
        <EventList events={shown} freshIds={freshIds} empty="No events match these filters." />
      </div>
    </Card>
  );
}
