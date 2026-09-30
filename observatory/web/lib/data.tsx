"use client";

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { ApiError, AuthError, apiGet, eventsSocketUrl } from "./api";
import { parseMs } from "./logic";
import type { EventsResponse, ObsEvent, PerfResponse, StateResponse, TradesResponse } from "./types";

// REST is polled every 10 s; the event feed is a WebSocket with exponential
// backoff. Nothing is cached across page loads: a reload starts from nothing,
// so stale trading data can never be shown as live.

export const POLL_MS = 10_000;
const EVENTS_KEEP = 2000;
const EVENTS_PAGE = 500;
const BACKOFF_START_MS = 1000;
const BACKOFF_MAX_MS = 30_000;

export interface Polled<T> {
  data: T | null;
  lastOkMs: number | null;
  error: string | null;
}

const empty = <T,>(): Polled<T> => ({ data: null, lastOkMs: null, error: null });

export type SocketState = "connecting" | "open" | "retrying";

export interface Arrival {
  event: ObsEvent;
  receivedMs: number;
}

interface DataValue {
  token: string;
  onAuthFail: () => void;
  state: Polled<StateResponse>;
  trades: Polled<TradesResponse>;
  perfDay: Polled<PerfResponse>;
  perfWeek: Polled<PerfResponse>;
  events: ObsEvent[];
  arrivals: Arrival[];
  socket: SocketState;
  /** server clock minus this device's clock, from the last /state answer. */
  serverOffsetMs: number;
}

const DataContext = createContext<DataValue | null>(null);

export function useData(): DataValue {
  const value = useContext(DataContext);
  if (!value) throw new Error("useData outside DataProvider");
  return value;
}

/** Poll one GET endpoint while mounted; refresh at once when the tab returns. */
export function usePoll<T>(
  path: string | null,
  token: string,
  intervalMs: number,
  onAuthFail: () => void,
): Polled<T> {
  const [result, setResult] = useState<Polled<T>>(empty<T>);

  useEffect(() => {
    if (!path) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async () => {
      clearTimeout(timer);
      try {
        const data = await apiGet<T>(path, token);
        if (alive) setResult({ data, lastOkMs: Date.now(), error: null });
      } catch (err) {
        if (!alive) return;
        if (err instanceof AuthError) {
          onAuthFail();
          return;
        }
        const message = err instanceof ApiError ? err.message : "request failed";
        setResult((prev) => ({ ...prev, error: message }));
      }
      if (alive) timer = setTimeout(tick, intervalMs);
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") void tick();
    };

    void tick();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [path, token, intervalMs, onAuthFail]);

  return result;
}

function mergeEvents(current: ObsEvent[], incoming: ObsEvent[]): ObsEvent[] {
  if (incoming.length === 0) return current;
  const lastId = current.length ? current[current.length - 1].id : 0;
  const fresh = incoming.filter((e) => e.id > lastId);
  if (fresh.length === 0) return current;
  const merged = current.concat(fresh);
  return merged.length > EVENTS_KEEP ? merged.slice(merged.length - EVENTS_KEEP) : merged;
}

function useEventStream(token: string, onAuthFail: () => void) {
  const [events, setEvents] = useState<ObsEvent[]>([]);
  const [arrivals, setArrivals] = useState<Arrival[]>([]);
  const [socket, setSocket] = useState<SocketState>("connecting");
  const epochRef = useRef<string | null>(null);
  const lastIdRef = useRef(0);

  useEffect(() => {
    let closed = false;
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let backoff = BACKOFF_START_MS;

    const scheduleRetry = () => {
      if (closed) return;
      setSocket("retrying");
      retry = setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
    };

    // Catch up over REST (detecting a projector restart by its epoch), then
    // follow the WebSocket from the last id seen.
    const connect = async () => {
      if (closed) return;
      try {
        const head = await apiGet<EventsResponse>("/api/v1/events?since_id=0&limit=1", token);
        const restarted = epochRef.current !== null && epochRef.current !== head.epoch;
        const known = restarted ? 0 : lastIdRef.current;
        const from = head.last_event_id - known > EVENTS_PAGE || known === 0
          ? Math.max(0, head.last_event_id - EVENTS_PAGE)
          : known;
        const page = await apiGet<EventsResponse>(`/api/v1/events?since_id=${from}&limit=${EVENTS_PAGE}`, token);
        if (closed) return;
        epochRef.current = page.epoch;
        setEvents((prev) => mergeEvents(restarted ? [] : prev, page.events));
        lastIdRef.current = Math.max(restarted ? 0 : lastIdRef.current, page.last_event_id);
      } catch (err) {
        if (err instanceof AuthError) {
          onAuthFail();
          return;
        }
        scheduleRetry();
        return;
      }

      ws = new WebSocket(eventsSocketUrl(token, lastIdRef.current));
      ws.onopen = () => {
        backoff = BACKOFF_START_MS;
        setSocket("open");
      };
      ws.onmessage = (message) => {
        let event: ObsEvent;
        try {
          event = JSON.parse(String(message.data)) as ObsEvent;
        } catch {
          return;
        }
        if (typeof event.id !== "number" || event.id <= lastIdRef.current) return;
        lastIdRef.current = event.id;
        setEvents((prev) => mergeEvents(prev, [event]));
        setArrivals((prev) => [...prev.slice(-99), { event, receivedMs: Date.now() }]);
      };
      ws.onerror = () => ws?.close();
      ws.onclose = () => {
        ws = null;
        scheduleRetry();
      };
    };

    void connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
    };
  }, [token, onAuthFail]);

  return { events, arrivals, socket };
}

export function DataProvider({
  token,
  onAuthFail,
  children,
}: {
  token: string;
  onAuthFail: () => void;
  children: ReactNode;
}) {
  const state = usePoll<StateResponse>("/api/v1/state", token, POLL_MS, onAuthFail);
  const trades = usePoll<TradesResponse>("/api/v1/trades?days=7", token, POLL_MS, onAuthFail);
  const perfDay = usePoll<PerfResponse>("/api/v1/perf?window=day", token, POLL_MS, onAuthFail);
  const perfWeek = usePoll<PerfResponse>("/api/v1/perf?window=week", token, POLL_MS, onAuthFail);
  const { events, arrivals, socket } = useEventStream(token, onAuthFail);

  const asOf = parseMs(state.data?.as_of);
  const serverOffsetMs = asOf !== null && state.lastOkMs !== null ? asOf - state.lastOkMs : 0;

  return (
    <DataContext.Provider
      value={{ token, onAuthFail, state, trades, perfDay, perfWeek, events, arrivals, socket, serverOffsetMs }}
    >
      {children}
    </DataContext.Provider>
  );
}
