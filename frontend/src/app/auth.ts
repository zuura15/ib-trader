/**
 * Browser side of ib-api access-key auth (see ib_trader/api/auth.py).
 *
 * The browser never stores the key. `login()` posts it once; ib-api answers
 * with an HttpOnly session cookie that the browser then attaches to every
 * fetch, WebSocket and EventSource request on its own. Burrow (the Android
 * tailnet app) sends a Bearer header from its proxy instead, so the session
 * probe reports it as authenticated and the login screen never shows.
 */

export interface SessionState {
  auth_required: boolean;
  authenticated: boolean;
}

/** Fired on `window` when any /api request comes back 401. */
export const AUTH_REQUIRED_EVENT = 'ibt:auth-required';

const API_BASE = import.meta.env.VITE_API_URL || '/api';
const AUTH_PREFIX = `${API_BASE}/auth/`;

/** Ask ib-api whether this browser is signed in. Throws on network failure. */
export async function getSession(): Promise<SessionState> {
  const res = await fetch(`${API_BASE}/auth/session`, { credentials: 'include' });
  if (!res.ok) throw new Error(`session check failed: HTTP ${res.status}`);
  return res.json();
}

/** Exchange the access key for a session cookie. Resolves false on a wrong key. */
export async function login(key: string): Promise<boolean> {
  const res = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key }),
  });
  if (res.status === 401) return false;
  if (!res.ok) throw new Error(`login failed: HTTP ${res.status}`);
  return true;
}

/** Clear the session cookie and return to the login screen. */
export async function logout(): Promise<void> {
  await fetch(`${API_BASE}/auth/logout`, { method: 'POST', credentials: 'include' });
  window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
}

function requestPath(input: RequestInfo | URL): string {
  const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  try {
    return new URL(raw, window.location.href).pathname;
  } catch {
    return raw;
  }
}

let installed = false;

/**
 * Watch every fetch for a 401 from ib-api (expired cookie, rotated key) and
 * announce it, so the gate can swap the app for the login screen. Covers the
 * many call sites that use `fetch` directly instead of `api/client.ts`.
 */
export function installAuthInterceptor(): void {
  if (installed) return;
  installed = true;
  const original = window.fetch.bind(window);
  const apiPath = new URL(API_BASE, window.location.href).pathname;
  const authPath = new URL(AUTH_PREFIX, window.location.href).pathname;
  window.fetch = async (input, init) => {
    const res = await original(input, init);
    if (res.status === 401) {
      const path = requestPath(input);
      if (path.startsWith(apiPath) && !path.startsWith(authPath)) {
        window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
      }
    }
    return res;
  };
}
