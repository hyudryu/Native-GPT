import { describe, it, expect } from "vitest";
import { describeEndpointStatus, relativeTime } from "./relTime";

const NOW = new Date("2026-07-20T12:00:00Z");
const secondsAgo = (s: number) =>
  new Date(NOW.getTime() - s * 1000).toISOString();

describe("relativeTime", () => {
  it("returns 'just now' for recent timestamps", () => {
    expect(relativeTime(secondsAgo(0), NOW)).toBe("just now");
    expect(relativeTime(secondsAgo(44), NOW)).toBe("just now");
  });
  it("treats future timestamps as 'just now'", () => {
    expect(relativeTime(secondsAgo(-60), NOW)).toBe("just now");
  });
  it("minutes", () => {
    expect(relativeTime(secondsAgo(60), NOW)).toBe("1 min ago");
    expect(relativeTime(secondsAgo(5 * 60), NOW)).toBe("5 min ago");
    expect(relativeTime(secondsAgo(59 * 60), NOW)).toBe("59 min ago");
  });
  it("hours", () => {
    expect(relativeTime(secondsAgo(60 * 60), NOW)).toBe("1 hr ago");
    expect(relativeTime(secondsAgo(3 * 3600), NOW)).toBe("3 hr ago");
  });
  it("days", () => {
    expect(relativeTime(secondsAgo(24 * 3600), NOW)).toBe("1 day ago");
    expect(relativeTime(secondsAgo(4 * 86400), NOW)).toBe("4 days ago");
  });
  it("falls back to a date for old timestamps", () => {
    const old = secondsAgo(90 * 86400);
    expect(relativeTime(old, NOW)).toBe(new Date(old).toLocaleDateString());
  });
  it("handles unparseable input", () => {
    expect(relativeTime("not-a-date", NOW)).toBe("unknown");
  });
});

describe("describeEndpointStatus", () => {
  it("never tested", () => {
    expect(describeEndpointStatus(null, null, NOW)).toEqual({
      tone: "muted",
      label: "Not tested",
    });
  });
  it("ok with relative time", () => {
    expect(describeEndpointStatus("ok", secondsAgo(5 * 60), NOW)).toEqual({
      tone: "ok",
      label: "OK · 5 min ago",
    });
  });
  it("failed with relative time", () => {
    expect(describeEndpointStatus("failed", secondsAgo(3600), NOW)).toEqual({
      tone: "danger",
      label: "Failed · 1 hr ago",
    });
  });
  it("status without a timestamp is treated as untested", () => {
    expect(describeEndpointStatus("ok", null, NOW).tone).toBe("muted");
  });
});
