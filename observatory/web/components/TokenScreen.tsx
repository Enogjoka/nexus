"use client";

import { KeyRound, Loader2, ShieldCheck } from "lucide-react";
import { useState, type FormEvent } from "react";

import { ApiError, AuthError, apiGet } from "@/lib/api";
import { saveToken } from "@/lib/token";

export default function TokenScreen({ rejected, onAccepted }: { rejected: boolean; onAccepted: () => void }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(rejected ? "The stored token was rejected. Enter the current one." : null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const token = value.trim();
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      await apiGet("/api/v1/health", token);
      await apiGet("/api/v1/state", token); // proves the token, not just the server
      saveToken(token);
      onAccepted();
    } catch (err) {
      if (err instanceof AuthError) setError("Token rejected by the observatory.");
      else if (err instanceof ApiError && err.status === null) setError("Observatory API unreachable.");
      else setError(err instanceof Error ? err.message : "Could not verify the token.");
    } finally {
      setBusy(false);
      setValue("");
    }
  };

  return (
    <main className="flex min-h-dvh items-center justify-center px-5">
      <form onSubmit={submit} className="w-full max-w-sm rounded-xl border border-line bg-panel p-6 shadow-2xl">
        <div className="mb-5 flex items-center gap-3">
          <span className="flex size-10 items-center justify-center rounded-lg bg-panel2 text-ring2">
            <KeyRound className="size-5" aria-hidden />
          </span>
          <div>
            <h1 className="text-base font-semibold tracking-tight">NEXUS Observatory</h1>
            <p className="text-xs text-muted">Read-only. Enter the observatory token.</p>
          </div>
        </div>

        <label htmlFor="token" className="mb-1.5 block text-xs font-medium text-muted">
          Access token
        </label>
        <input
          id="token"
          type="password"
          autoComplete="off"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="num w-full rounded-md border border-line2 bg-bg px-3 py-2.5 text-sm outline-none focus:border-ring2"
          placeholder="OBSERVATORY_TOKEN"
        />
        {error && <p className="mt-2 text-xs text-loss">{error}</p>}

        <button
          type="submit"
          disabled={busy || !value.trim()}
          className="mt-4 flex w-full items-center justify-center gap-2 rounded-md bg-ring2/90 px-3 py-2.5 text-sm font-semibold text-bg transition hover:bg-ring2 disabled:opacity-40"
        >
          {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <ShieldCheck className="size-4" aria-hidden />}
          Connect
        </button>

        <p className="mt-4 text-[11px] leading-relaxed text-faint">
          The token is kept only in this browser&apos;s local storage and sent to this server as a bearer
          header. Use &ldquo;forget token&rdquo; to remove it from this device.
        </p>
      </form>
    </main>
  );
}
