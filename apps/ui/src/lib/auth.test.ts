import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import {
  initAuth,
  getToken,
  clearToken,
  authFetch,
  TOKEN_STORAGE_KEY,
} from "./auth";

function setUrl(url: string) {
  window.history.replaceState(null, "", url);
}

beforeEach(() => {
  window.localStorage.clear();
  setUrl("/");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("initAuth", () => {
  it("persists ?token= to localStorage and scrubs the URL", () => {
    setUrl("/?token=deadbeefcafe#/");
    const token = initAuth();
    expect(token).toBe("deadbeefcafe");
    expect(window.localStorage.getItem(TOKEN_STORAGE_KEY)).toBe("deadbeefcafe");
    expect(window.location.href).not.toContain("deadbeef");
    expect(window.location.search).toBe("");
  });

  it("keeps other query params and the hash route when scrubbing", () => {
    setUrl("/?foo=1&token=abc123#/settings");
    initAuth();
    expect(window.location.search).toBe("?foo=1");
    expect(window.location.hash).toBe("#/settings");
  });

  it("returns the stored token when the URL has none", () => {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, "stored-token");
    setUrl("/");
    expect(initAuth()).toBe("stored-token");
  });

  it("returns null when there is no token anywhere", () => {
    expect(initAuth()).toBeNull();
  });

  it("a fresh pairing token overrides a stale stored one", () => {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, "old");
    setUrl("/?token=new");
    expect(initAuth()).toBe("new");
    expect(getToken()).toBe("new");
  });
});

describe("getToken / clearToken", () => {
  it("round-trips", () => {
    expect(getToken()).toBeNull();
    window.localStorage.setItem(TOKEN_STORAGE_KEY, "t");
    expect(getToken()).toBe("t");
    clearToken();
    expect(getToken()).toBeNull();
  });
});

describe("authFetch", () => {
  it("adds the Authorization header when a token exists", async () => {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, "sekrit");
    const spy = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", spy);

    await authFetch("/api/health");
    const [, init] = spy.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Authorization")).toBe(
      "Bearer sekrit",
    );
  });

  it("sends no Authorization header without a token", async () => {
    const spy = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", spy);

    await authFetch("/api/health");
    const [, init] = spy.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Authorization")).toBeNull();
  });

  it("does not clobber an explicit Authorization header", async () => {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, "sekrit");
    const spy = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", spy);

    await authFetch("/api/health", {
      headers: { Authorization: "Bearer other" },
    });
    const [, init] = spy.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer other");
  });
});
