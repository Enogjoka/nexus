"use client";

import { Activity, CandlestickChart, GitBranch, HeartPulse, LayoutDashboard, LineChart, Orbit, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

interface Item {
  href: string;
  label: string;
  icon: LucideIcon;
  soon?: boolean;
}

const ITEMS: Item[] = [
  { href: "/", label: "Command", icon: LayoutDashboard },
  { href: "/agents", label: "Agents", icon: Orbit },
  { href: "/trades", label: "Trades", icon: CandlestickChart },
  { href: "#", label: "Trace", icon: GitBranch, soon: true },
  { href: "#", label: "Performance", icon: LineChart, soon: true },
  { href: "#", label: "Health", icon: HeartPulse, soon: true },
];

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export default function Nav({ variant }: { variant: "side" | "bottom" }) {
  const pathname = usePathname() ?? "/";

  if (variant === "side") {
    return (
      <aside className="sticky top-0 hidden h-dvh w-52 shrink-0 flex-col border-r border-line bg-panel md:flex">
        <div className="flex items-center gap-2.5 px-4 py-4">
          <Activity className="size-5 text-ring2" aria-hidden />
          <div className="leading-tight">
            <div className="text-sm font-semibold tracking-wide">NEXUS</div>
            <div className="text-[10px] uppercase tracking-[0.18em] text-faint">Observatory</div>
          </div>
        </div>
        <nav className="flex flex-col gap-0.5 px-2" aria-label="Main">
          {ITEMS.map((item) =>
            item.soon ? (
              <span
                key={item.label}
                aria-disabled="true"
                className="flex cursor-not-allowed items-center gap-2.5 rounded-md px-3 py-2 text-sm text-faint/70"
              >
                <item.icon className="size-4" aria-hidden />
                {item.label}
                <span className="ml-auto rounded border border-line px-1.5 py-px text-[9px] uppercase tracking-wider">
                  soon
                </span>
              </span>
            ) : (
              <Link
                key={item.label}
                href={item.href}
                className={`flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition ${
                  isActive(pathname, item.href) ? "bg-panel2 text-ink" : "text-muted hover:bg-panel2/60 hover:text-ink"
                }`}
              >
                <item.icon className="size-4" aria-hidden />
                {item.label}
              </Link>
            ),
          )}
        </nav>
        <p className="mt-auto px-4 py-4 text-[10px] leading-relaxed text-faint">All times UTC · read-only</p>
      </aside>
    );
  }

  const live = ITEMS.filter((i) => !i.soon);
  return (
    <nav
      aria-label="Main"
      className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-4 border-t border-line bg-panel/95 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden"
    >
      {live.map((item) => {
        const active = isActive(pathname, item.href);
        return (
          <Link
            key={item.label}
            href={item.href}
            className={`flex flex-col items-center gap-1 py-2.5 text-[11px] ${active ? "text-ink" : "text-muted"}`}
          >
            <item.icon className={`size-5 ${active ? "text-ring2" : ""}`} aria-hidden />
            {item.label}
          </Link>
        );
      })}
      <span aria-disabled="true" className="flex flex-col items-center gap-1 py-2.5 text-[11px] text-faint/60">
        <GitBranch className="size-5" aria-hidden />
        Soon
      </span>
    </nav>
  );
}
