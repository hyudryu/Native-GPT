import { describe, expect, it } from "vitest";
import { appsRegistry } from "./appsRegistry";

describe("appsRegistry", () => {
  it("is alphabetized and has unique IDs", () => {
    const names = appsRegistry.map((app) => app.name);
    expect(names).toEqual([...names].sort((left, right) => left.localeCompare(right)));
    expect(new Set(appsRegistry.map((app) => app.id)).size).toBe(appsRegistry.length);
  });

  it("uses the canonical repository URL", () => {
    expect(appsRegistry.find((app) => app.id === "github")).toMatchObject({
      href: "https://github.com/hyudryu/Native-GPT",
      external: true,
    });
  });
});
