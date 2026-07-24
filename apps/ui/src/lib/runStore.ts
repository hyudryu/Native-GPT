import { create } from "zustand";
import type { QueryClient } from "@tanstack/react-query";
import type { Envelope } from "@agentgpt/protocol-types";
import { socket } from "./ws";
import type { RunRef } from "./dataApi";

export type Activity = { message: string; source?: string };

export type ToolCallStatus = "pending" | "ok" | "error";

export type ToolCallEntry = {
  callId: string;
  tool: string;
  input?: unknown;
  status: ToolCallStatus;
  summary?: string;
  data?: unknown;
  error?: { code: string; message: string } | null;
};

/** A pending human-in-the-loop approval prompt for a gated tool call. */
export type ApprovalPrompt = {
  approvalId: string;
  tool: string;
  input?: unknown;
  prompt: string;
};

/** Ephemeral live-run state for one conversation. */
export interface LiveRun {
  activeRun: RunRef | null;
  streamText: string;
  activity: Activity;
  streamError: string | null;
  toolCalls: ToolCallEntry[];
  approval: ApprovalPrompt | null;
}

const THINKING: Activity = { message: "Thinking through the request" };

/** Stable empty slice so selectors never allocate a new object per render. */
export const EMPTY_LIVE_RUN: LiveRun = {
  activeRun: null,
  streamText: "",
  activity: THINKING,
  streamError: null,
  toolCalls: [],
  approval: null,
};

interface RunStoreState {
  /** conversationId -> live run slice. */
  byConversation: Record<string, LiveRun>;
  /** run_id AND request_id -> conversationId, populated when a run starts. */
  runIndex: Record<string, string>;
  /**
   * Start (or confirm) a run for a conversation. Resets the slice unless the
   * same run is already live (e.g. seeded by run.started before the HTTP
   * response arrived) so early streamed text is never wiped.
   */
  startRun: (conversationId: string, run: RunRef) => void;
  /** User pressed Stop — mirrors the old cancel onSuccess handler. */
  stopRun: (conversationId: string) => void;
  /** Optimistically clear a pending approval after the user decides. */
  clearApproval: (conversationId: string) => void;
  /** Drop a conversation's slice and any runIndex entries pointing at it. */
  resetConversation: (conversationId: string) => void;
}

export const useRunStore = create<RunStoreState>()((set, get) => ({
  byConversation: {},
  runIndex: {},

  startRun: (conversationId, run) => {
    const existing = get().byConversation[conversationId];
    if (existing?.activeRun?.id === run.id) {
      // Same run already live (seeded by run.started) — just make sure both
      // index entries exist and keep accumulated stream/tool state.
      set((state) => ({
        runIndex: {
          ...state.runIndex,
          [run.id]: conversationId,
          [run.request_id]: conversationId,
        },
      }));
      return;
    }
    set((state) => ({
      byConversation: {
        ...state.byConversation,
        [conversationId]: {
          activeRun: run,
          streamText: "",
          activity: THINKING,
          streamError: null,
          toolCalls: [],
          approval: null,
        },
      },
      runIndex: {
        ...state.runIndex,
        [run.id]: conversationId,
        [run.request_id]: conversationId,
      },
    }));
    // Replay any run.* envelopes that arrived before this run was registered
    // (a fast model can emit deltas between the WS event and the HTTP
    // response, mirroring the old socket replay-buffer behavior).
    flushPending(conversationId, run);
  },

  stopRun: (conversationId) => {
    updateSlice(conversationId, (slice) => ({
      ...slice,
      activeRun: null,
      streamError: "Response stopped.",
    }));
    cleanupIndex(conversationId);
    // Partial assistant text is persisted on cancel — refetch.
    void invalidateMessages(conversationId).then(() => clearStreamText(conversationId));
  },

  clearApproval: (conversationId) => {
    updateSlice(conversationId, (slice) => ({ ...slice, approval: null }));
  },

  resetConversation: (conversationId) => {
    set((state) => {
      const byConversation = { ...state.byConversation };
      delete byConversation[conversationId];
      const runIndex = Object.fromEntries(
        Object.entries(state.runIndex).filter(([, id]) => id !== conversationId),
      );
      return { byConversation, runIndex };
    });
  },
}));

function updateSlice(
  conversationId: string,
  updater: (slice: LiveRun) => LiveRun,
): void {
  useRunStore.setState((state) => {
    const slice = state.byConversation[conversationId];
    if (!slice) return state;
    return {
      byConversation: { ...state.byConversation, [conversationId]: updater(slice) },
    };
  });
}

function cleanupIndex(conversationId: string): void {
  useRunStore.setState((state) => ({
    runIndex: Object.fromEntries(
      Object.entries(state.runIndex).filter(([, id]) => id !== conversationId),
    ),
  }));
}

function clearStreamText(conversationId: string): void {
  updateSlice(conversationId, (slice) => ({ ...slice, streamText: "" }));
}

// ── Query invalidation (wired up by initRunRouter) ──────────────────────────

let queryClient: QueryClient | null = null;

function invalidateMessages(conversationId: string): Promise<unknown> {
  if (!queryClient) return Promise.resolve();
  return queryClient.invalidateQueries({
    queryKey: ["conversations", conversationId, "messages"],
  });
}

// ── Global WS router ────────────────────────────────────────────────────────

const RUN_EVENT_TYPES = [
  "run.started",
  "run.activity",
  "run.text_delta",
  "run.tool_call",
  "run.tool_result",
  "run.approval_needed",
  "run.approval_resolved",
  "run.completed",
  "run.failed",
] as const;

/**
 * Envelopes that arrived before their run was registered in runIndex (e.g.
 * deltas emitted before the HTTP start response reached the UI). Bounded like
 * the socket's own replay buffer; flushed by startRun.
 */
const pendingEnvelopes: Envelope[] = [];

function bufferEnvelope(envelope: Envelope): void {
  pendingEnvelopes.push(envelope);
  if (pendingEnvelopes.length > 100) pendingEnvelopes.shift();
}

function flushPending(conversationId: string, run: RunRef): void {
  const matched: Envelope[] = [];
  for (let i = pendingEnvelopes.length - 1; i >= 0; i -= 1) {
    const envelope = pendingEnvelopes[i]!;
    const payload = envelope.payload as Record<string, unknown>;
    if (envelope.request_id === run.request_id || payload.run_id === run.id) {
      pendingEnvelopes.splice(i, 1);
      matched.unshift(envelope);
    }
  }
  for (const envelope of matched) {
    dispatchRunEvent(conversationId, envelope, envelope.payload as Record<string, unknown>);
  }
}

/**
 * Resolve an incoming run.* envelope to its conversation. Prefers the
 * runIndex (run_id, then request_id); falls back to the payload's
 * conversation_id when the slice already has the matching active run.
 */
function resolveConversation(
  envelope: Envelope,
  payload: Record<string, unknown>,
): string | null {
  const state = useRunStore.getState();
  const runId = typeof payload.run_id === "string" ? payload.run_id : null;
  if (runId && state.runIndex[runId]) return state.runIndex[runId]!;
  const byRequest = state.runIndex[envelope.request_id];
  if (byRequest) return byRequest;

  const convId =
    typeof payload.conversation_id === "string" ? payload.conversation_id : null;
  if (!convId) return null;
  const slice = state.byConversation[convId];
  if (!slice?.activeRun) return null;
  // Confirm the event belongs to the conversation's active run before
  // adopting it, mirroring the old matches() check.
  if (runId !== slice.activeRun.id && envelope.request_id !== slice.activeRun.request_id) {
    return null;
  }
  useRunStore.setState((current) => ({
    runIndex: {
      ...current.runIndex,
      ...(runId ? { [runId]: convId } : {}),
      [envelope.request_id]: convId,
    },
  }));
  return convId;
}

function routeRunEvent(envelope: Envelope): void {
  const payload = envelope.payload as Record<string, unknown>;

  if (envelope.type === "run.started") {
    // run.started carries conversation_id directly — seed the slice/index so
    // events emitted before the HTTP response lands are routed correctly.
    const convId =
      typeof payload.conversation_id === "string" ? payload.conversation_id : null;
    const runId = typeof payload.run_id === "string" ? payload.run_id : null;
    if (!convId || !runId) return;
    useRunStore
      .getState()
      .startRun(convId, { id: runId, request_id: envelope.request_id });
    return;
  }

  const conversationId = resolveConversation(envelope, payload);
  if (!conversationId) {
    // Unknown run — likely not registered yet; buffer for startRun replay.
    bufferEnvelope(envelope);
    return;
  }
  dispatchRunEvent(conversationId, envelope, payload);
}

function dispatchRunEvent(
  conversationId: string,
  envelope: Envelope,
  payload: Record<string, unknown>,
): void {
  switch (envelope.type) {
    case "run.text_delta": {
      if (typeof payload.text !== "string") return;
      const text = payload.text;
      updateSlice(conversationId, (slice) =>
        slice.activeRun ? { ...slice, streamText: slice.streamText + text } : slice,
      );
      return;
    }
    case "run.activity": {
      if (typeof payload.message !== "string") return;
      const activity: Activity = {
        message: payload.message,
        ...(typeof payload.source === "string" ? { source: payload.source } : {}),
      };
      updateSlice(conversationId, (slice) => ({ ...slice, activity }));
      return;
    }
    case "run.tool_call": {
      if (typeof payload.call_id !== "string") return;
      const callId = payload.call_id;
      const tool = typeof payload.tool === "string" ? payload.tool : "tool";
      const input = payload.input;
      updateSlice(conversationId, (slice) => {
        if (slice.toolCalls.some((entry) => entry.callId === callId)) return slice;
        return {
          ...slice,
          toolCalls: [
            ...slice.toolCalls,
            { callId, tool, input, status: "pending" as ToolCallStatus },
          ],
        };
      });
      return;
    }
    case "run.tool_result": {
      if (typeof payload.call_id !== "string") return;
      const callId = payload.call_id;
      const ok = payload.ok === true;
      const summary = typeof payload.summary === "string" ? payload.summary : undefined;
      const data = payload.data;
      const error = (payload.error ?? null) as { code: string; message: string } | null;
      updateSlice(conversationId, (slice) => ({
        ...slice,
        toolCalls: slice.toolCalls.map((entry) =>
          entry.callId === callId
            ? { ...entry, status: ok ? "ok" : ("error" as ToolCallStatus), summary, data, error }
            : entry,
        ),
      }));
      return;
    }
    case "run.approval_needed": {
      if (typeof payload.approval_id !== "string") return;
      const next: ApprovalPrompt = {
        approvalId: payload.approval_id,
        tool: typeof payload.tool === "string" ? payload.tool : "tool",
        input: payload.input,
        prompt:
          typeof payload.prompt === "string" ? payload.prompt : "Approve this tool call?",
      };
      updateSlice(conversationId, (slice) => {
        // Strands runs tools sequentially, so a second prompt means the first
        // was orphaned (e.g. its resolution event was missed) — replace it.
        if (slice.approval) {
          console.warn(`replacing unresolved approval ${slice.approval.approvalId}`);
        }
        return { ...slice, approval: next };
      });
      return;
    }
    case "run.approval_resolved": {
      if (typeof payload.approval_id !== "string") return;
      const approvalId = payload.approval_id;
      updateSlice(conversationId, (slice) =>
        slice.approval && slice.approval.approvalId === approvalId
          ? { ...slice, approval: null }
          : slice,
      );
      return;
    }
    case "run.completed": {
      updateSlice(conversationId, (slice) => ({
        ...slice,
        activeRun: null,
        approval: null,
      }));
      cleanupIndex(conversationId);
      void invalidateMessages(conversationId).then(() => clearStreamText(conversationId));
      return;
    }
    case "run.failed": {
      const error = payload.error as { message?: string } | undefined;
      updateSlice(conversationId, (slice) => ({
        ...slice,
        streamError: error?.message ?? "The response failed.",
        activeRun: null,
        approval: null,
      }));
      cleanupIndex(conversationId);
      // The host persists any partial assistant text on failure/cancel — refetch.
      void invalidateMessages(conversationId).then(() => clearStreamText(conversationId));
      return;
    }
    default:
      return;
  }
}

let routerStarted = false;

/**
 * Subscribe the global run.* event router exactly once, at app startup.
 * Replaces ChatPage's per-conversation WS effect: every run.* envelope is
 * routed to the conversation that owns the run, regardless of which page is
 * on screen. The socket replays its bounded envelope buffer to this single
 * early subscriber, so events emitted before init are not lost.
 */
export function initRunRouter(client: QueryClient): void {
  if (routerStarted) return;
  routerStarted = true;
  queryClient = client;
  for (const type of RUN_EVENT_TYPES) {
    socket.on(type, routeRunEvent);
  }
}
