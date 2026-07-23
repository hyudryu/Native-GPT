/**
 * Address-field logic (spec §2.4): accept URLs, treat anything else as a
 * search query through the configured search-engine template.
 */

export const DEFAULT_SEARCH_TEMPLATE = "https://www.google.com/search?q={q}";

const SCHEME_RE = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//;
/** localhost / loopback / RFC-1918 hosts get http://, everything else https://. */
const LOCAL_HOST_RE =
  /^(localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[::1\]|10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(:\d+)?([/?#]|$)/i;
/** Something that looks like a host[:port][/path] rather than a search phrase. */
const HOSTISH_RE =
  /^[^\s/?#]+\.[^\s/?#]{2,}(:\d+)?([/?#]\S*)?$|^[^\s/?#]+:\d+([/?#]\S*)?$/;

/**
 * Turn raw address-bar text into a navigation URL.
 * Returns an empty string for empty input.
 */
export function navigationTarget(
  raw: string,
  searchTemplate: string = DEFAULT_SEARCH_TEMPLATE,
): string {
  const text = raw.trim();
  if (!text) return "";
  if (SCHEME_RE.test(text)) return text;
  if (LOCAL_HOST_RE.test(text)) return `http://${text}`;
  if (!/\s/.test(text) && HOSTISH_RE.test(text)) return `https://${text}`;
  const encoded = encodeURIComponent(text);
  return searchTemplate.includes("{q}")
    ? searchTemplate.replace("{q}", encoded)
    : `${searchTemplate}${encoded}`;
}

export type SecurityKind = "secure" | "insecure" | "local" | "internal";

/** Icon variant for the address field's security indicator. */
export function securityKind(url: string): SecurityKind {
  const lower = url.trim().toLowerCase();
  if (!lower || lower === "about:blank") return "internal";
  if (lower.startsWith("https://")) return "secure";
  if (lower.startsWith("http://")) {
    const host = lower.slice("http://".length);
    return LOCAL_HOST_RE.test(host) ? "local" : "insecure";
  }
  return "internal";
}
