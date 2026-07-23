import { describe, expect, it } from "vitest";
import { assetPayload, renderableAssetUrl } from "./assetUrl";
import type { RemoteHost } from "./remoteHosts";

const host: RemoteHost = {
  id: "host-1",
  name: "GX10",
  base_url: "https://gx10:8443",
  tls_verify: false,
  has_token: true,
  status: "reachable",
  last_checked_at: null,
  workloads: null,
  created_at: "2026-07-22T00:00:00Z",
  updated_at: "2026-07-22T00:00:00Z",
};

describe("assetPayload", () => {
  it("returns legacy flat data as-is", () => {
    const data = { asset_url: "/api/assets/abc", kind: "image" };
    expect(assetPayload(data)).toBe(data);
  });

  it("unwraps MCP structured tool results", () => {
    const payload = {
      asset_url: "https://gx10:8443/assets/tok123",
      kind: "audio",
      mime_type: "audio/mpeg",
    };
    const data = {
      content: [{ text: '{"ok":true}' }],
      structured: { ok: true, summary: "done", data: payload, error: null },
    };
    expect(assetPayload(data)).toBe(payload);
  });

  it("returns null when there is no asset_url", () => {
    expect(assetPayload(null)).toBeNull();
    expect(assetPayload("x")).toBeNull();
    expect(assetPayload({ content: [] })).toBeNull();
    expect(assetPayload({ structured: { data: { kind: "image" } } })).toBeNull();
  });
});

describe("renderableAssetUrl", () => {
  it("keeps desktop-relative URLs as-is", () => {
    expect(renderableAssetUrl("/api/assets/abc", [host])).toBe("/api/assets/abc");
  });

  it("rewrites a matching bridge asset URL to the proxy route", () => {
    expect(renderableAssetUrl("https://gx10:8443/assets/tok123", [host])).toBe(
      "/api/remote-hosts/host-1/assets/tok123",
    );
  });

  it("tolerates a trailing slash on the host base_url", () => {
    const h = { ...host, base_url: "https://gx10:8443/" };
    expect(renderableAssetUrl("https://gx10:8443/assets/tok123", [h])).toBe(
      "/api/remote-hosts/host-1/assets/tok123",
    );
  });

  it("url-encodes host id and token", () => {
    const h = { ...host, id: "host/with space" };
    expect(renderableAssetUrl("https://gx10:8443/assets/tok%20x", [h])).toBe(
      "/api/remote-hosts/host%2Fwith%20space/assets/tok%2520x",
    );
  });

  it("does not rewrite when the host list is empty or no prefix matches", () => {
    const url = "https://other:9999/assets/tok123";
    expect(renderableAssetUrl(url, [])).toBe(url);
    expect(renderableAssetUrl(url, undefined)).toBe(url);
    expect(renderableAssetUrl(url, [host])).toBe(url);
  });

  it("does not proxy multi-segment or query-bearing tokens", () => {
    const nested = "https://gx10:8443/assets/a/b";
    expect(renderableAssetUrl(nested, [host])).toBe(nested);
    const query = "https://gx10:8443/assets/tok?x=1";
    expect(renderableAssetUrl(query, [host])).toBe(query);
  });
});
