import { describe, expect, it } from "vitest";
import { appsRegistry } from "./appsRegistry";

describe("appsRegistry", () => {
  it("is alphabetized and has unique IDs", () => {
    const names = appsRegistry.map((app) => app.name);
    expect(names).toEqual([...names].sort((left, right) => left.localeCompare(right)));
    expect(new Set(appsRegistry.map((app) => app.id)).size).toBe(appsRegistry.length);
  });

  it("does not carry an external repository link (moved to Updates)", () => {
    // The GitHub repo link now lives on the Updates page, not the Apps menu.
    expect(appsRegistry.find((app) => app.id === "github")).toBeUndefined();
    expect(appsRegistry.every((app) => !app.external)).toBe(true);
  });
});
