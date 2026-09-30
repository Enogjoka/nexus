"use client";

import { useMemo, useState } from "react";

import { useNow } from "@/lib/clock";
import { useData } from "@/lib/data";
import { HEARTBEAT_STALE_S, ago, fmtDuration, parseMs, sensorLevel, type SensorLevel } from "@/lib/logic";
import { RINGS, RING_LIST } from "@/lib/rings";
import type { ObsEvent, Ring } from "@/lib/types";

import { Card, Empty } from "./Card";
import { EventList } from "./EventList";

// Geometry: Ring 0 innermost, Ring 3 outermost. Node angles are in degrees,
// 0 = east, clockwise (SVG y grows downward), laid out so the decision flow
// reads from the sensor arc at the top, around the right side, into the core.
const SIZE = 760;
const C = SIZE / 2;
const BAND: Record<Ring, [number, number]> = { 0: [0, 96], 1: [96, 186], 2: [186, 278], 3: [278, 370] };
const NODE_R: Record<Ring, number> = { 0: 56, 1: 142, 2: 232, 3: 324 };

interface NodeDef {
  id: string;
  label: string;
  ring: Ring;
  angle: number;
  sensor?: boolean;
  note?: string;
}

const NODES: NodeDef[] = [
  { id: "candles", label: "Price feed", ring: 3, angle: -158, sensor: true },
  { id: "state_vectors", label: "State vector", ring: 3, angle: -130, sensor: true },
  { id: "macro_observations", label: "FRED macro", ring: 3, angle: -102, sensor: true },
  { id: "cot_reports", label: "COT", ring: 3, angle: -74, sensor: true },
  { id: "econ_events", label: "Calendar", ring: 3, angle: -46, sensor: true },
  { id: "news_articles", label: "News", ring: 3, angle: -18, sensor: true },
  { id: "supervisor", label: "Supervisor", ring: 3, angle: 150 },
  { id: "doctrine", label: "Doctrine", ring: 2, angle: -90 },
  { id: "analysis", label: "Analyst", ring: 2, angle: -22 },
  { id: "validator", label: "Validator", ring: 2, angle: 38 },
  { id: "paper_engine", label: "Paper engine", ring: 1, angle: 62, note: "no kernel link (F-02)" },
  { id: "router", label: "Router", ring: 1, angle: 118 },
  { id: "kernel", label: "Kernel", ring: 0, angle: 105 },
  { id: "stage", label: "Stage", ring: 0, angle: -35 },
  { id: "commands", label: "Commands", ring: 0, angle: -145 },
];

const SENSOR_IDS = NODES.filter((n) => n.sensor).map((n) => n.id);

// The decision flow. Each edge is [from, to]; an event from `to` animates along it.
const FLOW: [string, string][] = [
  ...SENSOR_IDS.map((s) => [s, "doctrine"] as [string, string]),
  ["doctrine", "analysis"],
  ["analysis", "validator"],
  ["validator", "paper_engine"],
  ["validator", "router"],
  ["router", "kernel"],
];

// Which edge an arriving event travels along, by the agent that emitted it.
const INBOUND: Record<string, [string, string]> = {
  doctrine: ["state_vectors", "doctrine"],
  analysis: ["doctrine", "analysis"],
  validator: ["analysis", "validator"],
  paper_engine: ["validator", "paper_engine"],
  router: ["validator", "router"],
  kernel: ["router", "kernel"],
};

const PULSE_WINDOW_S = 60;
const TRAVEL_MS = 1400;

function pos(node: NodeDef): { x: number; y: number } {
  const a = (node.angle * Math.PI) / 180;
  const r = NODE_R[node.ring];
  return { x: C + r * Math.cos(a), y: C + r * Math.sin(a) };
}

const BY_ID = Object.fromEntries(NODES.map((n) => [n.id, n])) as Record<string, NodeDef>;

type NodeStatus = "error" | "stale" | "ok" | "unknown";

interface NodeState {
  status: NodeStatus;
  pulse: boolean;
  last: ObsEvent | null;
  detail: string;
}

const STATUS_STROKE: Record<NodeStatus, string> = {
  error: "var(--color-loss)",
  stale: "var(--color-warn)",
  ok: "",
  unknown: "var(--color-faint)",
};

export default function RingMap() {
  const { state, events, arrivals, serverOffsetMs } = useData();
  const now = useNow();
  const nowServer = now + serverOffsetMs;
  const [selected, setSelected] = useState<string | null>(null);

  const heartbeat = state.data?.heartbeat ?? null;
  const freshness = state.data?.freshness ?? null;

  const lastByAgent = useMemo(() => {
    const map = new Map<string, ObsEvent>();
    for (const e of events) map.set(e.agent, e); // events are ascending by id
    return map;
  }, [events]);

  const nodeState = (node: NodeDef): NodeState => {
    if (node.sensor) {
      const age = freshness?.[node.id]?.age_now_s ?? null;
      const level: SensorLevel = sensorLevel(node.id, age);
      return {
        status: level === "stale" ? "stale" : level === "fresh" ? "ok" : "unknown",
        pulse: false,
        last: null,
        detail:
          freshness === null
            ? "No heartbeat: freshness unknown"
            : age === null
              ? "Not measured by the heartbeat"
              : `Freshest data ${fmtDuration(age)} old${level === "stale" ? " — stale" : ""}`,
      };
    }
    const last = lastByAgent.get(node.id) ?? null;
    const lastMs = parseMs(last?.ts);
    const pulse = now > 0 && lastMs !== null && nowServer - lastMs <= PULSE_WINDOW_S * 1000;
    let status: NodeStatus = last ? "ok" : "unknown";
    if (last && (last.severity === "error" || last.severity === "critical")) status = "error";
    if (node.id === "supervisor") {
      const hbMs = parseMs(heartbeat?.ts);
      if (hbMs === null) status = "unknown";
      else if (nowServer - hbMs > HEARTBEAT_STALE_S * 1000) status = "stale";
      else if (heartbeat?.dead) status = "error";
      else status = "ok";
    }
    const detail = last
      ? `Last event ${now ? ago(last.ts, nowServer) : ""}: ${last.severity}`
      : node.id === "supervisor" && heartbeat
        ? `Heartbeat ${now ? ago(heartbeat.ts, nowServer) : ""}`
        : "No events seen in the buffer";
    return { status, pulse, last, detail };
  };

  const travelling = arrivals.filter((a) => now > 0 && now - a.receivedMs < TRAVEL_MS + 1000 && INBOUND[a.event.agent]);

  const selectedNode = selected ? BY_ID[selected] : null;
  const selectedEvents = selected
    ? events.filter((e) => e.agent === selected).slice(-20).reverse()
    : [];

  return (
    <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_360px]">
      <Card title="Agent rings" hint="tap a node" className="min-w-0">
        <svg
          viewBox={`0 0 ${SIZE} ${SIZE}`}
          className="mx-auto block w-full max-w-[680px] select-none"
          role="img"
          aria-label="NEXUS agents arranged by ring"
        >
          {/* bands, outermost first */}
          {[...RING_LIST].reverse().map((ring) => (
            <g key={ring}>
              <circle cx={C} cy={C} r={BAND[ring][1]} fill={RINGS[ring].color} fillOpacity={0.045 + ring * 0.005} />
              <circle cx={C} cy={C} r={BAND[ring][1]} fill="none" stroke={RINGS[ring].color} strokeOpacity={0.35} strokeWidth={1} />
              <text
                x={C}
                y={C - BAND[ring][1] + 16}
                textAnchor="middle"
                className="fill-current text-[11px] tracking-[0.2em] uppercase"
                style={{ fill: RINGS[ring].color, opacity: 0.75 }}
              >
                {RINGS[ring].name} · {RINGS[ring].title}
              </text>
            </g>
          ))}

          {/* flow */}
          {FLOW.map(([from, to]) => {
            const a = pos(BY_ID[from]);
            const b = pos(BY_ID[to]);
            return (
              <line
                key={`${from}-${to}`}
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                stroke="var(--color-line2)"
                strokeWidth={BY_ID[from].sensor ? 1 : 1.6}
                strokeDasharray={BY_ID[from].sensor ? "3 5" : undefined}
              />
            );
          })}

          {/* events travelling along their inbound edge */}
          {travelling.map(({ event }) => {
            const [from, to] = INBOUND[event.agent];
            const a = pos(BY_ID[from]);
            const b = pos(BY_ID[to]);
            const color = event.severity === "error" || event.severity === "critical" ? "var(--color-loss)" : RINGS[event.ring]?.color;
            return (
              <circle key={event.id} r={5} fill={color}>
                <animateMotion dur={`${TRAVEL_MS}ms`} fill="freeze" path={`M${a.x},${a.y} L${b.x},${b.y}`} />
                <animate attributeName="opacity" values="1;1;0" dur={`${TRAVEL_MS + 400}ms`} fill="freeze" />
              </circle>
            );
          })}

          {/* nodes */}
          {NODES.map((node) => {
            const { x, y } = pos(node);
            const s = nodeState(node);
            const ringColor = RINGS[node.ring].color;
            const stroke = STATUS_STROKE[s.status] || ringColor;
            const isSel = selected === node.id;
            return (
              <g
                key={node.id}
                role="button"
                tabIndex={0}
                aria-label={`${node.label}: ${s.detail}`}
                onClick={() => setSelected(isSel ? null : node.id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") setSelected(isSel ? null : node.id);
                }}
                className="cursor-pointer outline-none"
              >
                {s.pulse && (
                  <circle
                    cx={x}
                    cy={y}
                    r={17}
                    fill="none"
                    stroke={ringColor}
                    strokeWidth={2}
                    className="animate-node-pulse"
                    style={{ transformBox: "fill-box", transformOrigin: "center" }}
                  />
                )}
                <circle cx={x} cy={y} r={isSel ? 21 : 17} fill="var(--color-panel)" stroke={stroke} strokeWidth={isSel ? 3 : 2}
                  strokeDasharray={s.status === "unknown" ? "4 3" : undefined} />
                <circle
                  cx={x}
                  cy={y}
                  r={6}
                  fill={s.status === "error" ? "var(--color-loss)" : s.status === "stale" ? "var(--color-warn)" : s.status === "unknown" ? "var(--color-faint)" : ringColor}
                />
                <text x={x} y={y + 33} textAnchor="middle" className="text-[12px] font-medium" style={{ fill: "var(--color-ink)" }}>
                  {node.label}
                </text>
                {node.note && (
                  <text x={x} y={y + 47} textAnchor="middle" className="text-[10px]" style={{ fill: "var(--color-warn)" }}>
                    {node.note}
                  </text>
                )}
              </g>
            );
          })}
        </svg>

        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted">
          <span className="flex items-center gap-1.5"><span className="size-2 rounded-full bg-ring2" />ok (ring colour)</span>
          <span className="flex items-center gap-1.5"><span className="size-2 rounded-full bg-warn" />stale data</span>
          <span className="flex items-center gap-1.5"><span className="size-2 rounded-full bg-loss" />last event error / critical</span>
          <span className="flex items-center gap-1.5"><span className="size-2 rounded-full bg-faint" />no data seen</span>
          <span>pulse = event in the last {PULSE_WINDOW_S} s</span>
        </div>
      </Card>

      <Card
        title={selectedNode ? selectedNode.label : "Node detail"}
        hint={selectedNode ? `${RINGS[selectedNode.ring].name} · ${RINGS[selectedNode.ring].title}` : undefined}
      >
        {!selectedNode ? (
          <Empty>Tap a node to see its last 20 events.</Empty>
        ) : (
          <div className="space-y-3">
            <p className="text-xs text-muted">{nodeState(selectedNode).detail}</p>
            {selectedNode.sensor ? (
              <p className="text-[11px] leading-relaxed text-faint">
                Sensors write data, not events. Their state comes only from the supervisor heartbeat&apos;s
                freshness map (age of each sensor&apos;s newest row).
              </p>
            ) : (
              <EventList events={selectedEvents} empty="No events from this agent in the buffer." />
            )}
          </div>
        )}
      </Card>
    </div>
  );
}
