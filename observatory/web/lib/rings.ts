import type { Ring, Severity } from "./types";

// One colour per ring, used everywhere: feed stripes, map bands, badges.
// Chosen away from the semantic colours (profit green, loss red, warn amber).
export const RINGS: Record<Ring, { name: string; title: string; color: string; text: string; bg: string; border: string }> = {
  0: { name: "Ring 0", title: "Kernel & governance", color: "#f472b6", text: "text-ring0", bg: "bg-ring0", border: "border-ring0" },
  1: { name: "Ring 1", title: "Execution", color: "#38bdf8", text: "text-ring1", bg: "bg-ring1", border: "border-ring1" },
  2: { name: "Ring 2", title: "Intelligence", color: "#a78bfa", text: "text-ring2", bg: "bg-ring2", border: "border-ring2" },
  3: { name: "Ring 3", title: "Sensors", color: "#2dd4bf", text: "text-ring3", bg: "bg-ring3", border: "border-ring3" },
};

export const RING_LIST: Ring[] = [0, 1, 2, 3];

export const SEVERITY_ORDER: Severity[] = ["debug", "info", "warn", "error", "critical"];

export const SEVERITY_STYLE: Record<Severity, string> = {
  debug: "text-faint border-line",
  info: "text-muted border-line",
  warn: "text-warn border-warn/40",
  error: "text-loss border-loss/50",
  critical: "text-white bg-loss/80 border-loss",
};

export function severityAtLeast(severity: Severity, floor: Severity | "all"): boolean {
  if (floor === "all") return true;
  return SEVERITY_ORDER.indexOf(severity) >= SEVERITY_ORDER.indexOf(floor);
}
