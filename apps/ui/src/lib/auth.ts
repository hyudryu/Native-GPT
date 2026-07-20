/**
 * Bearer-token auth for non-localhost access.
 *
 * The phone pairing URL looks like `http://100.x.y.z:PORT/?token=<hex>`.
 * On first load we lift the token out of the query string, persist it to
 * localStorage, and scrub the URL so the token never leaks via history,
 * screenshots, or copied links. Every subsequent fetch sends it as an
 * Authorization header; the WebSocket sends it as a query param (see ws.ts).
 */

export const TOKEN_STORAGE_KEY = "agentgpt.token";

export function getToken(win: Window = window): string | null {
  try {
    return win.localStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function clearToken(win: Window = window): void {
  try {
    win.localStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    /* storage unavailable */
  }
}

/**
 * Reads `?token=` from the current URL, persists it, and removes it from the
 * address bar via history.replaceState. Returns the effective token (the
 * freshly-persisted one, or whatever was already stored).
 */
export function initAuth(win: Window = window): string | null {
  try {
    const url = new URL(win.location.href);
    const fromQuery = url.searchParams.get("token");
    if (fromQuery) {
      win.localStorage.setItem(TOKEN_STORAGE_KEY, fromQuery);
      url.searchParams.delete("token");
      win.history.replaceState(
        win.history.state,
        "",
        `${url.pathname}${url.search}${url.hash}`,
      );
    }
  } catch {
    /* storage/history unavailable — fall through to whatever is stored */
  }
  return getToken(win);
}

/** fetch() that attaches `Authorization: Bearer <token>` when a token exists. */
export async function authFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return fetch(input, { ...init, headers });
}
