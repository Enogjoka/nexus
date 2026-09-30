// Same-origin calls to the O2 API. The page is served by the API itself, so
// no host is ever configured and nothing leaves that origin.

export class AuthError extends Error {
  constructor() {
    super("token rejected");
    this.name = "AuthError";
  }
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number | null) {
    super(message);
    this.name = "ApiError";
  }
}

const TIMEOUT_MS = 8000;

export async function apiGet<T>(path: string, token: string): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(path, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
      signal: controller.signal,
    });
  } catch {
    throw new ApiError("API unreachable", null);
  } finally {
    clearTimeout(timer);
  }
  if (response.status === 401) throw new AuthError();
  if (!response.ok) throw new ApiError(`API answered ${response.status}`, response.status);
  return (await response.json()) as T;
}

export function eventsSocketUrl(token: string, sinceId: number): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams({ since_id: String(sinceId), token });
  return `${scheme}://${window.location.host}/ws/events?${params.toString()}`;
}
