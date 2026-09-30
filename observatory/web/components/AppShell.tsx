"use client";

import { useCallback, useState, useSyncExternalStore, type ReactNode } from "react";

import { DataProvider } from "@/lib/data";
import { forgetToken, getToken, subscribeToken } from "@/lib/token";

import Nav from "./Nav";
import StaleBanner from "./StaleBanner";
import StatusBar from "./StatusBar";
import TokenScreen from "./TokenScreen";

// The static shell renders with no data at all. Only after mount, on this
// device, is a stored token read; without one the token screen is shown.

export default function AppShell({ children }: { children: ReactNode }) {
  const token = useSyncExternalStore(subscribeToken, getToken, () => undefined);
  const [rejected, setRejected] = useState(false);

  const onAuthFail = useCallback(() => {
    setRejected(true);
    forgetToken();
  }, []);

  if (token === undefined) {
    // Pre-render and the first client frame: no storage has been read yet.
    return <div className="min-h-dvh bg-bg" aria-busy="true" />;
  }
  if (token === null) {
    return <TokenScreen rejected={rejected} onAccepted={() => setRejected(false)} />;
  }

  return (
    <DataProvider token={token} onAuthFail={onAuthFail}>
      <div className="flex min-h-dvh">
        <Nav variant="side" />
        <div className="flex min-w-0 flex-1 flex-col">
          <StatusBar />
          <StaleBanner />
          <main className="flex-1 px-3 pt-3 pb-[calc(4.5rem+env(safe-area-inset-bottom))] md:px-5 md:pb-6">
            {children}
          </main>
        </div>
      </div>
      <Nav variant="bottom" />
    </DataProvider>
  );
}
