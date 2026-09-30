import type { ReactNode } from "react";

export function Card({
  title,
  hint,
  right,
  className = "",
  children,
}: {
  title: string;
  hint?: string;
  right?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section className={`flex min-w-0 flex-col rounded-lg border border-line bg-panel ${className}`}>
      <header className="flex items-center gap-2 border-b border-line px-3.5 py-2.5">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">{title}</h2>
        {hint && <span className="truncate text-[11px] text-faint">{hint}</span>}
        {right && <div className="ml-auto flex shrink-0 items-center gap-2">{right}</div>}
      </header>
      <div className="min-w-0 flex-1 p-3.5">{children}</div>
    </section>
  );
}

/** The explicit "nothing here" state every panel must have — never a fake number. */
export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-3 text-center text-xs text-faint">{children}</p>;
}

export function Loading() {
  return <p className="py-3 text-center text-xs text-faint">Waiting for the first answer from the API…</p>;
}

export function Stat({ label, value, tone = "" }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-[0.12em] text-faint">{label}</div>
      <div className={`num mt-0.5 truncate text-sm text-ink ${tone}`}>{value}</div>
    </div>
  );
}
