import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { authFetch } from "./auth";

/**
 * REST client + react-query hooks for the host's remote-hosts API.
 * Mirrors lib/endpoints.ts. Tokens are stored in the keychain server-side;
 * only has_token is surfaced to the UI.
 */

export interface WorkloadInfo {
  state: string;
  healthy: boolean;
  version?: string;
  description?: string;
}

export interface RemoteHost {
  id: string;
  name: string;
  base_url: string;
  tls_verify: boolean;
  has_token: boolean;
  status: "reachable" | "unreachable" | null;
  last_checked_at: string | null;
  workloads: Record<string, WorkloadInfo> | null;
  created_at: string;
  updated_at: string;
}

export interface TestResult {
  ok: boolean;
  latency_ms?: number;
  version?: string;
  workloads?: Record<string, WorkloadInfo>;
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

export async function listRemoteHosts(): Promise<RemoteHost[]> {
  const data = await apiFetch<{ hosts: RemoteHost[] }>("/api/remote-hosts");
  return data.hosts;
}

export interface CreateRemoteHostInput {
  name: string;
  base_url: string;
  token?: string;
  tls_verify?: boolean;
}

export function createRemoteHost(input: CreateRemoteHostInput): Promise<RemoteHost> {
  return apiFetch<{ host: RemoteHost }>("/api/remote-hosts", {
    method: "POST",
    body: JSON.stringify(input),
  }).then((d) => d.host);
}

export interface UpdateRemoteHostInput {
  name?: string;
  base_url?: string;
  /** string = set, null = clear, absent = keep. */
  token?: string | null;
  tls_verify?: boolean;
}

export function updateRemoteHost(
  id: string,
  input: UpdateRemoteHostInput,
): Promise<RemoteHost> {
  return apiFetch<{ host: RemoteHost }>(
    `/api/remote-hosts/${encodeURIComponent(id)}`,
    { method: "PATCH", body: JSON.stringify(input) },
  ).then((d) => d.host);
}

export function deleteRemoteHost(id: string): Promise<void> {
  return apiFetch<void>(`/api/remote-hosts/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function testRemoteHost(id: string): Promise<TestResult> {
  return apiFetch<TestResult>(
    `/api/remote-hosts/${encodeURIComponent(id)}/test`,
    { method: "POST" },
  );
}

// ---- react-query hooks ----

const remoteHostsKey = ["remote-hosts"] as const;

export function useRemoteHosts() {
  return useQuery({
    queryKey: remoteHostsKey,
    queryFn: listRemoteHosts,
    staleTime: 15_000,
  });
}

export function useCreateRemoteHost() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: createRemoteHost,
    onSuccess: () => qc.invalidateQueries({ queryKey: remoteHostsKey }),
  });
}

export function useUpdateRemoteHost() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, input }: { id: string; input: UpdateRemoteHostInput }) =>
      updateRemoteHost(id, input),
    onSuccess: () => qc.invalidateQueries({ queryKey: remoteHostsKey }),
  });
}

export function useDeleteRemoteHost() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: deleteRemoteHost,
    onSuccess: () => qc.invalidateQueries({ queryKey: remoteHostsKey }),
  });
}

export function useTestRemoteHost() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: testRemoteHost,
    onSettled: () => qc.invalidateQueries({ queryKey: remoteHostsKey }),
  });
}
