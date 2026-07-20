import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

function schema(name: string): Record<string, unknown> {
  return JSON.parse(
    readFileSync(new URL(`../schemas/${name}`, import.meta.url), "utf8"),
  ) as Record<string, unknown>;
}

describe("protocol schemas", () => {
  it("are valid JSON objects", () => {
    expect(schema("envelope.json")).toHaveProperty("$schema");
    expect(schema("messages.json")).toHaveProperty("$defs");
  });

  it("defines streaming run acknowledgements and terminal events", () => {
    const messages = schema("messages.json") as {
      $defs: Record<string, unknown>;
    };
    expect(messages.$defs).toHaveProperty("run.started");
    expect(messages.$defs).toHaveProperty("run.text_delta");
    expect(messages.$defs).toHaveProperty("run.completed");
    expect(messages.$defs).toHaveProperty("run.failed");
    expect(messages.$defs).toHaveProperty("run.cancelled");
  });
});
