"use client";

import { Cpu, KeyRound, LogOut, Radio, ShieldQuestion } from "lucide-react";
import type { ReactNode } from "react";

import { useNow } from "@/lib/clock";
import { useData } from "@/lib/data";
import { aiLight, type AiLevel } from "@/lib/logic";
import { forgetToken } from "@/lib/token";

const LIGHT: Record<AiLevel, string> = {
  green: "bg-profit shadow-[0_0_8px] shadow-profit/70",
  amber: "bg-warn shadow-[0_0_8px] shadow-warn/70",
  red: "bg-loss shadow-[0_0_8px] shadow-loss/70 animate-blink",
  unknown: "bg-faint",
};

function Chip({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className="flex shrink-0 items-center gap-1.5 rounded-md border border-line bg-panel2/60 px-2 py-1 text-[11px] text-muted"
    >
      {children}
    </span>
  );
}

export default function StatusBar() {
  const { state, socket, serverOffsetMs } = useData();
  const now = useNow();
  const nowServer = now + serverOffsetMs;
  const s = state.data;
  const heartbeat = s?.heartbeat ?? null;

  const liveStage = heartbeat?.stage ?? null;
  const bootStage = s?.stage.last_stage_event?.effective_stage ?? null;
  const stage = liveStage ?? bootStage;
  const light = now ? aiLight(heartbeat !== null, heartbeat?.model_ok_at, nowServer) : null;

  const onForget = () => {
    if (window.confirm("Forget the observatory token on this device?")) forgetToken();
  };

  return (
    <header className="sticky top-0 z-20 border-b border-line bg-bg/90 pt-[env(safe-area-inset-top)] backdrop-blur">
      <div className="scroll-thin flex items-center gap-2 overflow-x-auto px-3 py-2 md:px-5">
        <span
          title={liveStage ? "stage from the live heartbeat" : "stage from the last boot record (no heartbeat)"}
          className="shrink-0 rounded-md border border-ring2/50 bg-ring2/10 px-2 py-1 text-[11px] font-semibold tracking-[0.12em] text-ring2"
        >
          {stage ?? "STAGE ?"}
          {!liveStage && stage && <span className="ml-1 font-normal tracking-normal text-faint">(last boot)</span>}
        </span>

        <Chip title={light?.label}>
          <span className={`size-2 rounded-full ${light ? LIGHT[light.level] : "bg-faint"}`} aria-hidden />
          <span className="text-ink">AI</span>
          <span className="hidden sm:inline">{light ? light.label : "…"}</span>
        </Chip>

        <Chip title="agents reported by the supervisor heartbeat">
          <Cpu className="size-3.5" aria-hidden />
          {heartbeat ? (
            <span className="num">
              <span className="text-ink">{heartbeat.alive ?? "?"}</span> alive ·{" "}
              <span className={heartbeat.dead ? "text-loss" : "text-ink"}>{heartbeat.dead ?? "?"}</span> dead
            </span>
          ) : (
            <span>agents unknown</span>
          )}
        </Chip>

        <Chip title={s?.kill_switch.note ?? "The kill switch is a file on the trading host."}>
          <ShieldQuestion className="size-3.5" aria-hidden />
          kill switch: <span className="text-ink">unknown — host file</span>
        </Chip>

        <Chip title={`live event feed: ${socket}`}>
          <Radio className={`size-3.5 ${socket === "open" ? "text-profit" : "text-warn"}`} aria-hidden />
          <span className="hidden sm:inline">feed {socket}</span>
        </Chip>

        <button
          type="button"
          onClick={onForget}
          className="ml-auto flex shrink-0 items-center gap-1.5 rounded-md px-2 py-1 text-[11px] text-faint hover:bg-panel2 hover:text-ink"
          title="Remove the token from this device"
        >
          <KeyRound className="size-3.5 md:hidden" aria-hidden />
          <LogOut className="hidden size-3.5 md:block" aria-hidden />
          <span className="hidden md:inline">forget token</span>
        </button>
      </div>
    </header>
  );
}
