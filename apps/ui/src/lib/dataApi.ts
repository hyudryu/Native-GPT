import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authFetch } from "./auth";

export interface Project {
  id: string;
  name: string;
  description?: string | null;
  instructions?: string | null;
  endpoint_id?: string | null;
  model_id?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface Conversation {
  id: string;
  project_id?: string | null;
  title: string;
  instructions?: string | null;
  provider_id?: string | null;
  endpoint_id?: string | null;
  model_id?: string | null;
  archived_at?: string | null;
  /** Write-only: PATCH accepts `archived: bool` (server maps to archived_at). */
  archived?: boolean;
  created_at?: string;
  updated_at?: string;
  /** Message count, populated by the list endpoint. Absent on get/create. */
  message_count?: number;
}

export interface Message {
  id: string;
  conversation_id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  /** Accepted defensively for older persisted payloads. */
  content_json?: unknown;
  status?: string;
  created_at?: string;
  /**
   * JSON-serialized tool-call trace persisted on assistant messages produced
   * by runs that emitted any tool_call/tool_result events. Absent on
   * user/system messages and on assistant messages from tool-less runs.
   * See `parseToolEvents` for safe parsing.
   */
  tool_events_json?: string | null;
}

export interface EnabledModel {
  provider_id: string;
  provider_name: string;
  model_id: string;
}

export interface SearchResult {
  conversation_id: string;
  title: string;
  snippet: string;
  project_id?: string | null;
}

export interface RunRef {
  id: string;
  request_id: string;
}

class DataApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "DataApiError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await authFetch(path, { ...init, headers });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as {
        error?: { message?: string };
        message?: string;
      };
      message = body.error?.message ?? body.message ?? message;
    } catch {
      // Keep the status-based fallback for empty/non-JSON bodies.
    }
    throw new DataApiError(response.status, message);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function messageText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) =>
        typeof part === "string"
          ? part
          : part && typeof part === "object" && "text" in part
            ? String((part as { text: unknown }).text ?? "")
            : "",
      )
      .join("");
  }
  if (content && typeof content === "object") {
    const object = content as Record<string, unknown>;
    if (typeof object.text === "string") return object.text;
    if (typeof object.content === "string") return object.content;
    if (Array.isArray(object.content)) return messageText(object.content);
  }
  return "";
}

/**
 * Shape of a single persisted tool event. Mirrors the JSON written by the
 * Rust host's chat persistence task (one entry per `run.tool_call` /
 * `run.tool_result` event). Pair a `call` with its `result` by `call_id`.
 */
export type PersistedToolEvent =
  | {
      kind: "call";
      sequence?: number;
      call_id: string;
      tool: string;
      input?: unknown;
    }
  | {
      kind: "result";
      sequence?: number;
      call_id: string;
      tool?: string;
      ok: boolean;
      summary?: string;
      data?: unknown;
      error?: { code: string; message: string } | null;
      retryable?: boolean;
    };

/**
 * Safely parse a message's `tool_events_json` column. Returns an empty array
 * for missing/null/malformed payloads — never throws. Callers can render the
 * result without additional defensive checks.
 */
export function parseToolEvents(raw: string | null | undefined): PersistedToolEvent[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  return parsed.filter(
    (
      item,
    ): item is {
      kind: string;
      call_id?: unknown;
      tool?: unknown;
      input?: unknown;
      ok?: unknown;
      summary?: unknown;
      data?: unknown;
      error?: unknown;
      retryable?: unknown;
      sequence?: unknown;
    } =>
      typeof item === "object" &&
      item !== null &&
      typeof (item as { kind?: unknown }).kind === "string" &&
      typeof (item as { call_id?: unknown }).call_id === "string",
  ) as PersistedToolEvent[];
}

export function modelOptionValue(model: Pick<EnabledModel, "provider_id" | "model_id">): string {
  return `${encodeURIComponent(model.provider_id)}:${encodeURIComponent(model.model_id)}`;
}

export function parseModelOptionValue(value: string): {
  provider_id: string;
  model_id: string;
} | null {
  const separator = value.indexOf(":");
  if (separator < 0) return null;
  try {
    return {
      provider_id: decodeURIComponent(value.slice(0, separator)),
      model_id: decodeURIComponent(value.slice(separator + 1)),
    };
  } catch {
    return null;
  }
}

export async function listProjects(): Promise<Project[]> {
  return (await request<{ projects: Project[] }>("/api/projects")).projects;
}

export async function createProject(input: Pick<Project, "name"> & Partial<Project>): Promise<Project> {
  return (
    await request<{ project: Project }>("/api/projects", {
      method: "POST",
      body: JSON.stringify(input),
    })
  ).project;
}

export async function updateProject(id: string, input: Partial<Project>): Promise<Project> {
  return (
    await request<{ project: Project }>(`/api/projects/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(input),
    })
  ).project;
}

export function deleteProject(id: string): Promise<void> {
  return request(`/api/projects/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function getProject(id: string): Promise<Project> {
  return (
    await request<{ project: Project }>(`/api/projects/${encodeURIComponent(id)}`)
  ).project;
}

export async function listConversations(): Promise<Conversation[]> {
  return (await request<{ conversations: Conversation[] }>("/api/conversations"))
    .conversations;
}

export async function listProjectConversations(projectId: string): Promise<Conversation[]> {
  return (
    await request<{ conversations: Conversation[] }>(
      `/api/conversations?project_id=${encodeURIComponent(projectId)}`,
    )
  ).conversations;
}

export async function listArchivedConversations(): Promise<Conversation[]> {
  return (
    await request<{ conversations: Conversation[] }>("/api/conversations?archived=true")
  ).conversations;
}

export async function getConversation(id: string): Promise<Conversation> {
  return (
    await request<{ conversation: Conversation }>(
      `/api/conversations/${encodeURIComponent(id)}`,
    )
  ).conversation;
}

export async function createConversation(
  input: Partial<Conversation> & Pick<Conversation, "title">,
): Promise<Conversation> {
  return (
    await request<{ conversation: Conversation }>("/api/conversations", {
      method: "POST",
      body: JSON.stringify(input),
    })
  ).conversation;
}

export async function updateConversation(
  id: string,
  input: Partial<Conversation>,
): Promise<Conversation> {
  return (
    await request<{ conversation: Conversation }>(
      `/api/conversations/${encodeURIComponent(id)}`,
      { method: "PATCH", body: JSON.stringify(input) },
    )
  ).conversation;
}

export function deleteConversation(id: string): Promise<void> {
  return request(`/api/conversations/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function listMessages(conversationId: string): Promise<Message[]> {
  return (
    await request<{ messages: Message[] }>(
      `/api/conversations/${encodeURIComponent(conversationId)}/messages`,
    )
  ).messages;
}

export async function sendMessage(
  conversationId: string,
  input: { content: string; endpoint_id?: string; model_id?: string; factory_mode?: boolean; factory_revision?: string },
): Promise<{ message: Message; run: RunRef }> {
  return request(
    `/api/conversations/${encodeURIComponent(conversationId)}/messages`,
    { method: "POST", body: JSON.stringify(input) },
  );
}

export function cancelRun(id: string): Promise<void> {
  return request(`/api/runs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
}

export async function listEnabledModels(): Promise<EnabledModel[]> {
  return (await request<{ models: EnabledModel[] }>("/api/models?enabled=true")).models;
}

export async function searchConversations(query: string): Promise<SearchResult[]> {
  const data = await request<{
    results?: SearchResult[];
    conversations?: Conversation[];
  }>(
    `/api/search?q=${encodeURIComponent(query)}`,
  );
  return (
    data.results ??
    data.conversations?.map((conversation) => ({
      conversation_id: conversation.id,
      title: conversation.title,
      snippet: "",
      project_id: conversation.project_id,
    })) ??
    []
  );
}

const projectsKey = ["projects"] as const;
const projectKey = (id: string) => ["projects", id] as const;
const conversationsKey = ["conversations"] as const;
const conversationKey = (id: string) => ["conversations", id] as const;
const messagesKey = (id: string) => ["conversations", id, "messages"] as const;

export function useProjects() {
  return useQuery({ queryKey: projectsKey, queryFn: listProjects, staleTime: 30_000 });
}

export function useProject(id: string | undefined) {
  return useQuery({
    queryKey: projectKey(id ?? ""),
    queryFn: () => getProject(id!),
    enabled: Boolean(id),
  });
}

export function useCreateProject() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: createProject,
    onSuccess: () => client.invalidateQueries({ queryKey: projectsKey }),
  });
}

export function useUpdateProject() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, input }: { id: string; input: Partial<Project> }) =>
      updateProject(id, input),
    onSuccess: () => client.invalidateQueries({ queryKey: projectsKey }),
  });
}

export function useDeleteProject() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: deleteProject,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: projectsKey });
      void client.invalidateQueries({ queryKey: conversationsKey });
    },
  });
}

export function useConversations() {
  return useQuery({
    queryKey: conversationsKey,
    queryFn: listConversations,
    staleTime: 10_000,
  });
}

export function useArchivedConversations() {
  return useQuery({
    // Nested under conversationsKey so archive/unarchive/delete mutations
    // (which invalidate the "conversations" prefix) refresh this list too.
    queryKey: [...conversationsKey, "archived"],
    queryFn: listArchivedConversations,
    staleTime: 10_000,
  });
}

/**
 * Conversations scoped to a single project. Nested under the conversations
 * prefix so cross-cutting mutations (create/archive/delete) refresh this list.
 */
export function useProjectConversations(projectId: string | undefined) {
  return useQuery({
    queryKey: [...conversationsKey, "project", projectId ?? ""],
    queryFn: () => listProjectConversations(projectId!),
    enabled: Boolean(projectId),
    staleTime: 10_000,
  });
}

export function useConversation(id: string | undefined) {
  return useQuery({
    queryKey: conversationKey(id ?? ""),
    queryFn: () => getConversation(id!),
    enabled: Boolean(id),
  });
}

export function useCreateConversation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: createConversation,
    onSuccess: () => client.invalidateQueries({ queryKey: conversationsKey }),
  });
}

export function useUpdateConversation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, input }: { id: string; input: Partial<Conversation> }) =>
      updateConversation(id, input),
    onSuccess: (conversation) => {
      client.setQueryData(conversationKey(conversation.id), conversation);
      void client.invalidateQueries({ queryKey: conversationsKey });
    },
  });
}

export function useDeleteConversation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: deleteConversation,
    onSuccess: () => client.invalidateQueries({ queryKey: conversationsKey }),
  });
}

export function useMessages(conversationId: string | undefined) {
  return useQuery({
    queryKey: messagesKey(conversationId ?? ""),
    queryFn: () => listMessages(conversationId!),
    enabled: Boolean(conversationId),
  });
}

export function useSendMessage(conversationId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: { content: string; endpoint_id?: string; model_id?: string }) =>
      sendMessage(conversationId, input),
    // Optimistically append the user message so it shows the instant they hit
    // send, instead of waiting for the POST to return. The placeholder is
    // swapped for the real persisted message in `onSuccess`, and rolled back in
    // `onError` (paired with ChatPage restoring the draft text on failure).
    onMutate: async (input) => {
      await client.cancelQueries({ queryKey: messagesKey(conversationId) });
      const previous = client.getQueryData<Message[]>(messagesKey(conversationId));
      const optimisticId = `__optimistic__:${input.content}:${Date.now()}`;
      const optimistic: Message = {
        id: optimisticId,
        conversation_id: conversationId,
        role: "user",
        content: input.content,
        created_at: new Date().toISOString(),
      };
      client.setQueryData<Message[]>(messagesKey(conversationId), (current = []) => [
        ...current,
        optimistic,
      ]);
      return { previous, optimisticId };
    },
    onSuccess: ({ message }, _input, ctx) => {
      client.setQueryData<Message[]>(messagesKey(conversationId), (current = []) => {
        // Drop the optimistic placeholder, then dedup-append the real message.
        const withoutOptimistic = ctx?.optimisticId
          ? current.filter((item) => item.id !== ctx.optimisticId)
          : current;
        if (withoutOptimistic.some((item) => item.id === message.id)) return withoutOptimistic;
        return [...withoutOptimistic, message];
      });
      void client.invalidateQueries({ queryKey: conversationsKey });
    },
    onError: (_err, _input, ctx) => {
      if (ctx) client.setQueryData<Message[]>(messagesKey(conversationId), ctx.previous);
    },
  });
}

export function useCancelRun() {
  return useMutation({ mutationFn: cancelRun });
}

export function useEnabledModels() {
  return useQuery({
    queryKey: ["models", "enabled"],
    queryFn: listEnabledModels,
    staleTime: 30_000,
  });
}

export function useSearch(query: string) {
  const normalized = query.trim();
  return useQuery({
    queryKey: ["search", normalized],
    queryFn: () => searchConversations(normalized),
    enabled: normalized.length >= 2,
  });
}

export { DataApiError };
