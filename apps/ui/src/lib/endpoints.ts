import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { authFetch } from "./auth";

/**
 * REST client + react-query hooks for the host's endpoint/model API.
 * All requests go through authFetch (bearer token; localhost exempt server-side).
 */

export interface Endpoint {
  id: string;
  name: string;
  base_url: string;
  timeout_seconds: number;
  tls_verify: boolean;
  has_api_key: boolean;
  default_model_id: string | null;
  last_test_status: "ok" | "failed" | null;
  last_tested_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ModelInfo {
  id: string;
  hidden: boolean;
  source: "discovered" | "manual";
  capabilities?: string[];
}

export interface TestResult {
  ok: boolean;
  latency_ms?: number;
  models?: ModelInfo[];
  fetched_at?: string;
  error?: { code: string; message: string };
}

export class ApiError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await authFetch(path, { ...init, headers });
  if (!res.ok) {
    let code = `http_${res.status}`;
    let message = `Request failed with status ${res.status}`;
    try {
      const body = (await res.json()) as {
        error?: { code?: string; message?: string };
      };
      if (body.error?.code) code = body.error.code;
      if (body.error?.message) message = body.error.message;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(code, message, res.status);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---- plain API functions ----

export async function listEndpoints(): Promise<Endpoint[]> {
  const data = await apiFetch<{ endpoints: Endpoint[] }>("/api/endpoints");
  return data.endpoints;
}

export interface CreateEndpointInput {
  name: string;
  base_url: string;
  api_key?: string;
  timeout_seconds?: number;
  tls_verify?: boolean;
}

export function createEndpoint(input: CreateEndpointInput): Promise<Endpoint> {
  return apiFetch<{ endpoint: Endpoint }>("/api/endpoints", {
    method: "POST",
    body: JSON.stringify(input),
  }).then((d) => d.endpoint);
}

export interface UpdateEndpointInput {
  name?: string;
  base_url?: string;
  /** string = set, null = clear, absent = keep. */
  api_key?: string | null;
  timeout_seconds?: number;
  tls_verify?: boolean;
  default_model_id?: string | null;
}

export function updateEndpoint(
  id: string,
  input: UpdateEndpointInput,
): Promise<Endpoint> {
  return apiFetch<{ endpoint: Endpoint }>(
    `/api/endpoints/${encodeURIComponent(id)}`,
    { method: "PATCH", body: JSON.stringify(input) },
  ).then((d) => d.endpoint);
}

export function deleteEndpoint(id: string): Promise<void> {
  return apiFetch<void>(`/api/endpoints/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function testEndpoint(id: string): Promise<TestResult> {
  return apiFetch<TestResult>(
    `/api/endpoints/${encodeURIComponent(id)}/test`,
    { method: "POST" },
  );
}

export interface ModelsResponse {
  models: ModelInfo[];
  fetched_at: string;
}

export function listModels(id: string, refresh = false): Promise<ModelsResponse> {
  const query = refresh ? "?refresh=true" : "";
  return apiFetch<ModelsResponse>(
    `/api/endpoints/${encodeURIComponent(id)}/models${query}`,
  );
}

export function addModel(id: string, modelId: string): Promise<ModelInfo> {
  return apiFetch<{ model: ModelInfo }>(
    `/api/endpoints/${encodeURIComponent(id)}/models`,
    { method: "POST", body: JSON.stringify({ model_id: modelId }) },
  ).then((d) => d.model);
}

export function updateModel(
  id: string,
  modelId: string,
  input: { hidden: boolean },
): Promise<ModelInfo> {
  return apiFetch<{ model: ModelInfo }>(
    `/api/endpoints/${encodeURIComponent(id)}/models/${encodeURIComponent(modelId)}`,
    { method: "PATCH", body: JSON.stringify(input) },
  ).then((d) => d.model);
}

export function setAllModelsHidden(
  id: string,
  input: { hidden: boolean },
): Promise<ModelInfo[]> {
  return apiFetch<{ models: ModelInfo[] }>(
    `/api/endpoints/${encodeURIComponent(id)}/models/hidden`,
    { method: "POST", body: JSON.stringify(input) },
  ).then((d) => d.models);
}

// ---- react-query hooks ----

const endpointsKey = ["endpoints"] as const;
const modelsKey = (id: string) => ["endpoints", id, "models"] as const;

export function useEndpoints() {
  return useQuery({
    queryKey: endpointsKey,
    queryFn: listEndpoints,
    staleTime: 15_000,
  });
}

export function useCreateEndpoint() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: createEndpoint,
    onSuccess: () => qc.invalidateQueries({ queryKey: endpointsKey }),
  });
}

export function useUpdateEndpoint() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, input }: { id: string; input: UpdateEndpointInput }) =>
      updateEndpoint(id, input),
    onSuccess: () => qc.invalidateQueries({ queryKey: endpointsKey }),
  });
}

export function useDeleteEndpoint() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: deleteEndpoint,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: endpointsKey });
      void qc.invalidateQueries({ queryKey: ["models", "enabled"] });
    },
  });
}

export function useTestEndpoint() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: testEndpoint,
    onSuccess: (data, id) => {
      if (data.models) {
        qc.setQueryData(modelsKey(id), {
          models: data.models,
          fetched_at: data.fetched_at ?? new Date().toISOString(),
        });
        void qc.invalidateQueries({ queryKey: ["models", "enabled"] });
      }
    },
    onSettled: () => qc.invalidateQueries({ queryKey: endpointsKey }),
  });
}

/** Cached model list for an endpoint; only fetched while `enabled` (panel expanded). */
export function useModels(id: string, enabled: boolean) {
  return useQuery({
    queryKey: modelsKey(id),
    queryFn: () => listModels(id, false),
    enabled,
    staleTime: 60_000,
  });
}

/** "Discover models" — forces a refresh against the endpoint. */
export function useDiscoverModels(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => listModels(id, true),
    onSuccess: (data) => {
      qc.setQueryData(modelsKey(id), data);
      void qc.invalidateQueries({ queryKey: ["models", "enabled"] });
    },
  });
}

export function useAddModel(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (modelId: string) => addModel(id, modelId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: modelsKey(id) });
      void qc.invalidateQueries({ queryKey: ["models", "enabled"] });
    },
  });
}

export function useUpdateModel(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ modelId, hidden }: { modelId: string; hidden: boolean }) =>
      updateModel(id, modelId, { hidden }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: modelsKey(id) });
      void qc.invalidateQueries({ queryKey: ["models", "enabled"] });
    },
  });
}

export function useSetAllModelsHidden(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (hidden: boolean) => setAllModelsHidden(id, { hidden }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: modelsKey(id) });
      void qc.invalidateQueries({ queryKey: ["models", "enabled"] });
    },
    onError: (error) => {
      console.error("Failed to toggle model visibility:", error);
    },
  });
}
