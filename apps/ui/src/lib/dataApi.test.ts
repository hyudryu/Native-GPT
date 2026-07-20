import { describe, expect, it } from "vitest";
import { messageText, modelOptionValue, parseModelOptionValue } from "./dataApi";

describe("messageText", () => {
  it("reads the supported persisted content shapes", () => {
    expect(messageText("hello")).toBe("hello");
    expect(messageText({ text: "hello" })).toBe("hello");
    expect(messageText({ content: [{ text: "hel" }, { text: "lo" }] })).toBe("hello");
  });

  it("returns an empty string for unknown content", () => {
    expect(messageText({ image: "only" })).toBe("");
  });
});

describe("model option identity", () => {
  it("round-trips provider and model ids containing separators", () => {
    const encoded = modelOptionValue({ provider_id: "cloud:west", model_id: "org/model:v2" });
    expect(parseModelOptionValue(encoded)).toEqual({
      provider_id: "cloud:west",
      model_id: "org/model:v2",
    });
  });

  it("rejects malformed option values", () => {
    expect(parseModelOptionValue("missing-separator")).toBeNull();
    expect(parseModelOptionValue("bad:%E0%A4%A")).toBeNull();
  });
});
