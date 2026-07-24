import { describe, expect, it } from "vitest";
import {
  DEFAULT_SEARCH_TEMPLATE,
  navigationTarget,
  securityKind,
} from "./addressBar";

describe("navigationTarget", () => {
  it("passes through URLs that already have a scheme", () => {
    expect(navigationTarget("https://example.com/a?b=c")).toBe(
      "https://example.com/a?b=c",
    );
    expect(navigationTarget("http://example.com")).toBe("http://example.com");
  });

  it("prepends https:// to domain-like input", () => {
    expect(navigationTarget("example.com")).toBe("https://example.com");
    expect(navigationTarget("example.com/path?q=1")).toBe(
      "https://example.com/path?q=1",
    );
    expect(navigationTarget("sub.example.co.uk")).toBe(
      "https://sub.example.co.uk",
    );
  });

  it("uses http:// for localhost and private hosts", () => {
    expect(navigationTarget("localhost:3000/app")).toBe(
      "http://localhost:3000/app",
    );
    expect(navigationTarget("127.0.0.1:8787")).toBe("http://127.0.0.1:8787");
    expect(navigationTarget("192.168.1.10")).toBe("http://192.168.1.10");
  });

  it("treats phrases with spaces as search queries", () => {
    expect(navigationTarget("how to cook rice")).toBe(
      `https://www.google.com/search?q=${encodeURIComponent("how to cook rice")}`,
    );
  });

  it("treats single words without dots as search queries", () => {
    expect(navigationTarget("nativegpt")).toBe(
      `https://www.google.com/search?q=nativegpt`,
    );
  });

  it("honors a custom search template", () => {
    expect(navigationTarget("cats", "https://www.bing.com/search?q={q}")).toBe(
      "https://www.bing.com/search?q=cats",
    );
  });

  it("returns an empty string for empty input", () => {
    expect(navigationTarget("   ")).toBe("");
  });

  it("uses the default template constant", () => {
    expect(DEFAULT_SEARCH_TEMPLATE).toContain("{q}");
  });
});

describe("securityKind", () => {
  it("classifies https as secure", () => {
    expect(securityKind("https://example.com")).toBe("secure");
  });

  it("classifies public http as insecure", () => {
    expect(securityKind("http://example.com")).toBe("insecure");
  });

  it("classifies local http as local", () => {
    expect(securityKind("http://localhost:3000")).toBe("local");
    expect(securityKind("http://127.0.0.1")).toBe("local");
  });

  it("classifies blank/internal pages as internal", () => {
    expect(securityKind("")).toBe("internal");
    expect(securityKind("about:blank")).toBe("internal");
  });
});
