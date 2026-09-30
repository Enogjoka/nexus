"use client";

import { useSyncExternalStore } from "react";

// One shared 1 Hz clock for countdowns and ages. A store (not Date.now() in
// render) keeps rendering pure; the server snapshot is 0 so the static build
// never bakes a time into the HTML.

let now = 0;
let timer: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

function subscribe(onTick: () => void): () => void {
  listeners.add(onTick);
  if (timer === null) {
    now = Date.now();
    timer = setInterval(() => {
      now = Date.now();
      listeners.forEach((fn) => fn());
    }, 1000);
  }
  return () => {
    listeners.delete(onTick);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

export function useNow(): number {
  return useSyncExternalStore(
    subscribe,
    () => now,
    () => 0,
  );
}
