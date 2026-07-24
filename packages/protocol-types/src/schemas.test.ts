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

  it("threads optional factory_mode through the run.start payload", () => {
    const messages = schema("messages.json") as {
      $defs: Record<
        string,
        { properties?: Record<string, { type?: string }>; required?: string[] }
      >;
    };
    const definition = messages.$defs["run.start"];
    expect(definition.properties?.factory_mode?.type).toBe("boolean");
    expect(definition.required ?? []).not.toContain("factory_mode");
  });

  it("does not carry browser stream events (they use a dedicated WebSocket)", () => {
    const messages = schema("messages.json") as {
      $defs: Record<string, unknown>;
    };
    const envelope = schema("envelope.json") as { properties?: Record<string, unknown> };
    for (const name of Object.keys(messages.$defs)) {
      expect(name.startsWith("browser.")).toBe(false);
      expect(name.startsWith("input.")).toBe(false);
    }
    expect(envelope).toHaveProperty("$schema");
  });
});

describe("browser stream schema", () => {
  const SERVER_EVENTS = [
    "browser.state",
    "browser.tab.created",
    "browser.tab.updated",
    "browser.tab.closed",
    "browser.navigation",
    "browser.task.started",
    "browser.task.activity",
    "browser.task.finished",
    "browser.task.failed",
    "browser.file_chooser",
    "browser.download",
    "browser.crashed",
  ];
  const CLIENT_COMMANDS = [
    "input.mouse",
    "input.wheel",
    "input.key",
    "input.text",
    "viewport.resize",
    "frame.ack",
    "tab.activate",
  ];

  it("is a valid JSON object with $defs", () => {
    expect(schema("browser-stream.json")).toHaveProperty("$defs");
  });

  it("defines every server event and client command from spec §9.3", () => {
    const stream = schema("browser-stream.json") as {
      $defs: Record<string, { properties?: Record<string, { const?: string }> }>;
    };
    for (const name of [...SERVER_EVENTS, ...CLIENT_COMMANDS]) {
      expect(stream.$defs).toHaveProperty(name);
      // Tagged-union shape: each message pins its own `type` const.
      expect(stream.$defs[name]?.properties?.type?.const).toBe(name);
    }
  });

  it("mirrors the Rust serde field names for client commands", () => {
    const stream = schema("browser-stream.json") as {
      $defs: Record<
        string,
        { properties?: Record<string, unknown>; required?: string[] }
      >;
    };
    // Variant fields of ClientCommand are NOT renamed: snake_case on the wire.
    expect(stream.$defs["frame.ack"]?.required).toEqual(["type", "frame_id"]);
    expect(stream.$defs["tab.activate"]?.required).toEqual(["type", "tab_id"]);
    // Payload structs use camelCase (rename_all = "camelCase").
    for (const name of ["input.mouse", "input.wheel", "input.key", "input.text", "viewport.resize"]) {
      expect(stream.$defs[name]?.required).toEqual(["type", "payload"]);
    }
    const mouse = stream.$defs.MouseInput as {
      properties?: Record<string, unknown>;
      required?: string[];
    };
    expect(mouse.required).toEqual(["kind", "x", "y"]);
    expect(mouse.properties).toHaveProperty("clickCount");
    expect(mouse.properties).not.toHaveProperty("click_count");
    const viewport = stream.$defs.ViewportSize as { properties?: Record<string, unknown> };
    expect(viewport.properties).toHaveProperty("deviceScaleFactor");
  });

  it("mirrors the BrowserState snapshot required fields", () => {
    const stream = schema("browser-stream.json") as {
      $defs: Record<string, { required?: string[]; properties?: Record<string, unknown> }>;
    };
    const state = stream.$defs.BrowserState;
    expect(state?.required).toEqual([
      "installed",
      "installStatus",
      "processStatus",
      "profileId",
      "panelMode",
      "panelWidth",
      "connected",
      "tabs",
      "manualControlEnabled",
      "remoteViewerCount",
      "pendingApprovals",
    ]);
    const event = stream.$defs["browser.state"] as {
      properties?: { payload?: { $ref?: string } };
    };
    expect(event.properties?.payload?.$ref).toBe("#/$defs/BrowserState");
  });
});
