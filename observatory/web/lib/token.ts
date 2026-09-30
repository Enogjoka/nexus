// The API token lives only in this browser's localStorage. It is never part of
// the build, never in a URL the user sees, and never logged.

const KEY = "nexus.observatory.token";
const listeners = new Set<() => void>();

function read(): string | null {
  try {
    const value = window.localStorage.getItem(KEY);
    return value && value.trim() ? value : null;
  } catch {
    return null; // private mode or storage disabled: behave as "no token"
  }
}

export function getToken(): string | null {
  return typeof window === "undefined" ? null : read();
}

export function saveToken(token: string): void {
  try {
    window.localStorage.setItem(KEY, token.trim());
  } finally {
    listeners.forEach((fn) => fn());
  }
}

export function forgetToken(): void {
  try {
    window.localStorage.removeItem(KEY);
  } finally {
    listeners.forEach((fn) => fn());
  }
}

/** For useSyncExternalStore: re-render when the token changes (this tab or another). */
export function subscribeToken(onChange: () => void): () => void {
  listeners.add(onChange);
  const onStorage = (e: StorageEvent) => {
    if (e.key === KEY) onChange();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(onChange);
    window.removeEventListener("storage", onStorage);
  };
}
