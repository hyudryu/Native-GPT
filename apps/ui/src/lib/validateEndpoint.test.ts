import { describe, it, expect } from "vitest";
import {
  hasErrors,
  toEndpointPayload,
  validateEndpointForm,
  DEFAULT_TIMEOUT_SECONDS,
  type EndpointFormValues,
} from "./validateEndpoint";

function values(overrides: Partial<EndpointFormValues> = {}): EndpointFormValues {
  return {
    name: "llama.cpp",
    base_url: "http://127.0.0.1:8080/v1",
    api_key: "",
    clear_key: false,
    timeout_seconds: "15",
    thinking_off_params: "",
    thinking_high_params: "",
    ...overrides,
  };
}

describe("validateEndpointForm", () => {
  it("accepts a valid form", () => {
    expect(hasErrors(validateEndpointForm(values()))).toBe(false);
  });

  it("requires a name", () => {
    const errors = validateEndpointForm(values({ name: "   " }));
    expect(errors.name).toBeDefined();
  });

  it("requires a base URL", () => {
    expect(validateEndpointForm(values({ base_url: "" })).base_url).toBeDefined();
  });

  it("rejects non-URL and non-http(s) base URLs", () => {
    expect(
      validateEndpointForm(values({ base_url: "not a url" })).base_url,
    ).toBeDefined();
    expect(
      validateEndpointForm(values({ base_url: "ftp://example.com" })).base_url,
    ).toBeDefined();
  });

  it("accepts https URLs", () => {
    expect(
      hasErrors(validateEndpointForm(values({ base_url: "https://100.64.1.2:8080/v1" }))),
    ).toBe(false);
  });

  it("validates timeout range and integrality", () => {
    expect(
      validateEndpointForm(values({ timeout_seconds: "0" })).timeout_seconds,
    ).toBeDefined();
    expect(
      validateEndpointForm(values({ timeout_seconds: "301" })).timeout_seconds,
    ).toBeDefined();
    expect(
      validateEndpointForm(values({ timeout_seconds: "1.5" })).timeout_seconds,
    ).toBeDefined();
    expect(
      validateEndpointForm(values({ timeout_seconds: "abc" })).timeout_seconds,
    ).toBeDefined();
    expect(
      validateEndpointForm(values({ timeout_seconds: "30" })).timeout_seconds,
    ).toBeUndefined();
  });

  it("empty timeout falls back to the default (valid)", () => {
    expect(
      hasErrors(validateEndpointForm(values({ timeout_seconds: "" }))),
    ).toBe(false);
  });

  it("accepts JSON-object thinking params overrides", () => {
    const errors = validateEndpointForm(
      values({
        thinking_off_params: '{"reasoning_effort": "none"}',
        thinking_high_params: '{"thinking": {"type": "enabled"}}',
      }),
    );
    expect(errors.thinking_off_params).toBeUndefined();
    expect(errors.thinking_high_params).toBeUndefined();
  });

  it("rejects malformed JSON thinking params", () => {
    const errors = validateEndpointForm(
      values({ thinking_off_params: "{not json", thinking_high_params: "{" }),
    );
    expect(errors.thinking_off_params).toBeDefined();
    expect(errors.thinking_high_params).toBeDefined();
  });

  it("rejects non-object JSON thinking params", () => {
    const errors = validateEndpointForm(
      values({ thinking_off_params: '["a"]', thinking_high_params: '"high"' }),
    );
    expect(errors.thinking_off_params).toBeDefined();
    expect(errors.thinking_high_params).toBeDefined();
  });
});

describe("toEndpointPayload", () => {
  it("trims, strips trailing slashes, and applies defaults", () => {
    expect(
      toEndpointPayload(
        values({ name: "  Ollama ", base_url: "http://host:11434/v1/", timeout_seconds: "" }),
      ),
    ).toEqual({
      name: "Ollama",
      base_url: "http://host:11434/v1",
      timeout_seconds: DEFAULT_TIMEOUT_SECONDS,
      thinking_off_params: null,
      thinking_high_params: null,
    });
  });

  it("uses the provided timeout", () => {
    expect(toEndpointPayload(values({ timeout_seconds: "42" })).timeout_seconds).toBe(42);
  });

  it("parses thinking params overrides into objects", () => {
    expect(
      toEndpointPayload(
        values({
          thinking_off_params: '{\n  "reasoning_effort": "none"\n}',
          thinking_high_params: '{"reasoning_effort": "high"}',
        }),
      ),
    ).toEqual({
      name: "llama.cpp",
      base_url: "http://127.0.0.1:8080/v1",
      timeout_seconds: 15,
      thinking_off_params: { reasoning_effort: "none" },
      thinking_high_params: { reasoning_effort: "high" },
    });
  });
});
