"use client";

import { AlertTriangle } from "lucide-react";

import { useNow } from "@/lib/clock";
import { useData } from "@/lib/data";
import { computeStaleness } from "@/lib/logic";

export default function StaleBanner() {
  const { state, serverOffsetMs } = useData();
  const now = useNow();
  if (!now) return null;

  const staleness = computeStaleness({
    nowMs: now + serverOffsetMs,
    lastApiOkMs: state.lastOkMs === null ? null : state.lastOkMs + serverOffsetMs,
    apiError: state.error !== null,
    heartbeatTs: state.data?.heartbeat?.ts ?? null,
  });
  if (!staleness.stale) return null;

  return (
    <div role="alert" className="flex items-center gap-2 border-b border-loss/60 bg-loss/90 px-3 py-2 text-xs font-semibold text-white md:px-5">
      <AlertTriangle className="size-4 shrink-0" aria-hidden />
      <span className="tracking-wider">STALE</span>
      <span className="font-normal">{staleness.message}</span>
    </div>
  );
}
