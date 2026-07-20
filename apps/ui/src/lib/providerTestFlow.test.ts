import { describe, expect, it, vi } from "vitest";
import type { Endpoint } from "./endpoints";
import { persistProviderForTest } from "./providerTestFlow";

const endpoint: Endpoint = {
  id: "provider-1",
  name: "Local provider",
  base_url: "http://127.0.0.1:8080/v1",
  timeout_seconds: 15,
  tls_verify: true,
  has_api_key: false,
  default_model_id: null,
  last_test_status: null,
  last_tested_at: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const input = {
  name: "Local provider",
  base_url: "http://127.0.0.1:8080/v1",
};

describe("persistProviderForTest", () => {
  it("creates a provider when the draft has no persisted ID", async () => {
    const create = vi.fn().mockResolvedValue(endpoint);
    const update = vi.fn();

    await expect(
      persistProviderForTest(null, input, { create, update }),
    ).resolves.toEqual(endpoint);
    expect(create).toHaveBeenCalledWith(input);
    expect(update).not.toHaveBeenCalled();
  });

  it("updates the same provider on repeated tests", async () => {
    const create = vi.fn();
    const update = vi.fn().mockResolvedValue(endpoint);

    await expect(
      persistProviderForTest(endpoint, input, { create, update }),
    ).resolves.toEqual(endpoint);
    expect(update).toHaveBeenCalledWith(endpoint.id, input);
    expect(create).not.toHaveBeenCalled();
  });
});
