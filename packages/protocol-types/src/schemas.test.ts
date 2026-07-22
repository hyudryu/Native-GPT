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
    expect(messages.$defs).toHaveProperty("run.activity");
    expect(messages.$defs).toHaveProperty("run.text_delta");
    expect(messages.$defs).toHaveProperty("run.completed");
    expect(messages.$defs).toHaveProperty("run.failed");
    expect(messages.$defs).toHaveProperty("run.cancelled");
  });

  it("defines tool-call streaming events", () => {
    const messages = schema("messages.json") as {
      $defs: Record<string, unknown>;
    };
    expect(messages.$defs).toHaveProperty("run.tool_call");
    expect(messages.$defs).toHaveProperty("run.tool_result");
  });

  it("defines the human-in-the-loop approval flow", () => {
    const messages = schema("messages.json") as {
      $defs: Record<
        string,
        { properties?: Record<string, unknown>; required?: string[] }
      >;
    };
    for (const name of [
      "run.approval_needed",
      "run.approve",
      "run.approve.ok",
      "run.approval_resolved",
    ]) {
      expect(messages.$defs).toHaveProperty(name);
    }
    expect(messages.$defs["run.approval_needed"]?.required).toEqual([
      "run_id",
      "approval_id",
      "tool",
      "input",
      "prompt",
    ]);
    expect(messages.$defs["run.approve"]?.required).toEqual(["approval_id", "approved"]);
    expect(messages.$defs["run.approval_resolved"]?.required).toEqual([
      "run_id",
      "approval_id",
      "approved",
    ]);
  });

  it("threads optional tls_verify through endpoint/model/run payloads", () => {
    const messages = schema("messages.json") as {
      $defs: Record<
        string,
        { properties?: Record<string, { type?: string }>; required?: string[] }
      >;
    };
    for (const name of ["endpoint.test", "models.list", "run.start"]) {
      const definition = messages.$defs[name];
      expect(definition.properties?.tls_verify?.type).toBe("boolean");
      // Absent must stay valid: secure-by-default is applied by the receivers.
      expect(definition.required ?? []).not.toContain("tls_verify");
    }
  });
});
