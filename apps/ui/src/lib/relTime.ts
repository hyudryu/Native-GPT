/**
 * Small pure helpers for endpoint status display — unit-tested, no React.
 */

export function relativeTime(iso: string, now: Date = new Date()): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "unknown";
  const diffMs = now.getTime() - then;
  if (diffMs < 0) return "just now";

  const seconds = Math.floor(diffMs / 1000);
  if (seconds < 45) return "just now";

  const minutes = Math.floor(seconds / 60);
  if (minutes < 1.5) return "1 min ago";
  if (minutes < 60) return `${minutes} min ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 1.5) return "1 hr ago";
  if (hours < 24) return `${hours} hr ago`;

  const days = Math.floor(hours / 24);
  if (days < 1.5) return "1 day ago";
  if (days < 30) return `${days} days ago`;

  return new Date(then).toLocaleDateString();
}

export type EndpointTestStatus = "ok" | "failed" | null;
export type StatusTone = "ok" | "danger" | "muted";

export interface EndpointStatusDescription {
  tone: StatusTone;
  label: string;
}

/** Colored-dot + label summary of an endpoint's last connection test. */
export function describeEndpointStatus(
  status: EndpointTestStatus,
  testedAt: string | null,
  now: Date = new Date(),
): EndpointStatusDescription {
  if (status === null || testedAt === null) {
    return { tone: "muted", label: "Not tested" };
  }
  const when = relativeTime(testedAt, now);
  if (status === "ok") {
    return { tone: "ok", label: `OK · ${when}` };
  }
  return { tone: "danger", label: `Failed · ${when}` };
}
