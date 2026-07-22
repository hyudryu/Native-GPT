import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authFetch } from "./auth";

export interface KnowledgeSource { id: string; title: string; source_type: "paste" | "file" | "url"; source_uri?: string | null; chunk_count: number; created_at: string; }
export interface KnowledgeMatch { chunk_id: string; source_id: string; source_title: string; position: number; content: string; score: number; }
export interface ToolInfo { id: string; name: string; description: string; version: string; trusted: boolean; enabled: boolean; folder: string; risk?: "read" | "write" | "execute" | "external_side_effect" | null; requires_approval?: boolean | null; network?: "none" | "outbound" | null; timeout_seconds?: number | null; }
export interface ModelAnalytics { provider_name: string; model_id: string; runs: number; successful_runs: number; input_tokens: number; output_tokens: number; total_tokens: number; average_tokens_per_second: number; average_run_duration_ms: number; }
export interface AnalyticsResponse { totals: Omit<ModelAnalytics, "provider_name" | "model_id" | "average_run_duration_ms">; models: ModelAnalytics[]; }
export interface UpdateCheck { current_version: string; latest_version?: string | null; update_available: boolean; release_url: string; release_name?: string | null; release_notes?: string | null; published_at?: string | null; message: string; }

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  const response = await authFetch(path, { ...init, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { error?: { message?: string } } | null;
    throw new Error(body?.error?.message ?? `Request failed (${response.status})`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function useKnowledge() { return useQuery({ queryKey: ["knowledge"], queryFn: () => request<{ sources: KnowledgeSource[]; stats: { source_count: number; chunk_count: number } }>("/api/knowledge") }); }
export function useIngestKnowledge() { const client = useQueryClient(); return useMutation({ mutationFn: (input: { title: string; source_type: "paste" | "file" | "url"; source_uri?: string; content?: string; content_b64?: string }) => request("/api/knowledge", { method: "POST", body: JSON.stringify(input) }), onSuccess: () => client.invalidateQueries({ queryKey: ["knowledge"] }) }); }
export function useDeleteKnowledge() { const client = useQueryClient(); return useMutation({ mutationFn: (id: string) => request(`/api/knowledge/${encodeURIComponent(id)}`, { method: "DELETE" }), onSuccess: () => client.invalidateQueries({ queryKey: ["knowledge"] }) }); }
export function useKnowledgeSearch(query: string) { return useQuery({ queryKey: ["knowledge-search", query], queryFn: () => request<{ query: string; matches: KnowledgeMatch[] }>(`/api/knowledge/search?q=${encodeURIComponent(query)}`), enabled: query.trim().length > 1 }); }
export function useAnalytics() { return useQuery({ queryKey: ["analytics"], queryFn: () => request<AnalyticsResponse>("/api/analytics/models") }); }
export function useTools() { return useQuery({ queryKey: ["tools"], queryFn: () => request<{ tools: ToolInfo[] }>("/api/tools") }); }
export function useUpdateTool() { const client = useQueryClient(); return useMutation({ mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => request(`/api/tools/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ enabled }) }), onSuccess: () => client.invalidateQueries({ queryKey: ["tools"] }) }); }
export function useCheckUpdates() { return useMutation({ mutationFn: () => request<UpdateCheck>("/api/updates/check") }); }
