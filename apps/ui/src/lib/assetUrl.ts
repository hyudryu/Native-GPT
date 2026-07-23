import type { RemoteHost } from "./remoteHosts";

/**
 * Helpers for rendering tool-result assets inline in chat.
 *
 * Since the bridge became an MCP server (design spec
 * docs/superpowers/specs/2026-07-22-bridge-mcp-server-design.md), generation
 * tools return bridge-direct asset URLs (`https://<host>:8443/assets/<token>`).
 * The webview cannot load those: the bridge requires a bearer token and may
 * present a self-signed certificate. The desktop server proxies asset bytes
 * at `/api/remote-hosts/{host_id}/assets/{token}` (auth + TLS handled
 * server-side), so bridge URLs are rewritten to that same-origin route.
 * Legacy desktop-relative URLs (`/api/assets/{id}`) render as-is.
 */

/**
 * Extract the asset payload (`asset_url`, `kind`, …) from a tool result's
 * `data`, handling both shapes:
 * - legacy local tools: fields directly on `data`
 * - MCP tools: `data.structured` is the tool's `{ok, summary, data, error}`
 *   dict, so the payload lives at `data.structured.data`
 */
export function assetPayload(data: unknown): Record<string, unknown> | null {
  if (typeof data !== "object" || data === null) return null;
  const d = data as Record<string, unknown>;
  if (typeof d.asset_url === "string" && d.asset_url) return d;
  const structured = d.structured;
  if (typeof structured === "object" && structured !== null) {
    const inner = (structured as Record<string, unknown>).data;
    if (typeof inner === "object" && inner !== null) {
      const payload = inner as Record<string, unknown>;
      if (typeof payload.asset_url === "string" && payload.asset_url) return payload;
    }
  }
  return null;
}

/**
 * Rewrite a tool-result asset URL into one the webview can load. Bridge
 * asset URLs whose prefix matches a configured host's `base_url` are routed
 * through the desktop's same-origin proxy; anything else is returned
 * unchanged (desktop-relative URLs, unknown remotes).
 */
export function renderableAssetUrl(
  assetUrl: string,
  hosts: RemoteHost[] | undefined,
): string {
  if (assetUrl.startsWith("/")) return assetUrl;
  for (const host of hosts ?? []) {
    const prefix = `${host.base_url.replace(/\/+$/, "")}/assets/`;
    if (!assetUrl.startsWith(prefix)) continue;
    const token = assetUrl.slice(prefix.length);
    // Asset tokens are single path segments; anything else isn't ours to proxy.
    if (token && !token.includes("/") && !token.includes("?") && !token.includes("#")) {
      return `/api/remote-hosts/${encodeURIComponent(host.id)}/assets/${encodeURIComponent(token)}`;
    }
  }
  return assetUrl;
}
