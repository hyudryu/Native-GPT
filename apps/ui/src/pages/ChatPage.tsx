import { useEffect, useLayoutEffect, useRef, useState, useMemo } from "react";
import { useLocation, useNavigate, useParams } from "react-router";
import { Select } from "@base-ui-components/react/select";
import {
  Check,
  ChevronDown,
  CircleStop,
  LoaderCircle,
  Plus,
  SendHorizontal,
  ShieldAlert,
  X,
} from "lucide-react";
import { PROTOCOL_VERSION } from "@agentgpt/protocol-types";
import {
  messageText,
  modelOptionValue,
  parseModelOptionValue,
  parseToolEvents,
  useCancelRun,
  useConversation,
  useCreateConversation,
  useEnabledModels,
  useMessages,
  useProjects,
  useSendMessage,
  useUpdateConversation,
  type EnabledModel,
  type PersistedToolEvent,
} from "../lib/dataApi";
import { socket } from "../lib/ws";
import {
  EMPTY_LIVE_RUN,
  useRunStore,
  type Activity,
  type ApprovalPrompt,
  type ToolCallEntry,
} from "../lib/runStore";
import { MarkdownMessage, PlainMessage } from "../components/MarkdownMessage";

/**
 * If a tool result includes generated asset data (from comfyui_generate or
 * openvoice_tts), render the asset inline — an <img> for images/video posters,
 * an <audio> player for audio.
 */
function AssetPreview({ data }: { data: unknown }) {
  if (typeof data !== "object" || data === null) return null;
  const d = data as Record<string, unknown>;
  const assetUrl = d.asset_url;
  const kind = d.kind;
  if (typeof assetUrl !== "string" || !assetUrl) return null;
  if (kind === "audio") {
    return (
      <div className="mt-2">
        <audio controls src={assetUrl} className="w-full" />
      </div>
    );
  }
  if (kind === "image" || kind === "video") {
    return (
      <div className="mt-2">
        {kind === "image" ? (
          <img
            src={assetUrl}
            alt={(d.prompt as string) ?? "Generated image"}
            className="max-h-80 w-full rounded-xl border border-border object-contain"
          />
        ) : (
          <video controls src={assetUrl} className="max-h-80 w-full rounded-xl border border-border" />
        )}
      </div>
    );
  }
  return null;
}

/**
 * Convert a persisted tool-events trace into the same shape the live
 * `<ToolCalls>` renderer consumes. Calls and results are paired by `call_id`;
 * a call with no matching result stays `pending` (e.g. an interrupted run).
 */
function toolEntriesFromPersisted(events: PersistedToolEvent[]): ToolCallEntry[] {
  const byCallId = new Map<string, ToolCallEntry>();
  // Pass 1: seed from call events so order follows the original call stream.
  for (const event of events) {
    if (event.kind !== "call") continue;
    if (byCallId.has(event.call_id)) continue;
    byCallId.set(event.call_id, {
      callId: event.call_id,
      tool: event.tool,
      input: event.input,
      status: "pending",
    });
  }
  // Pass 2: attach results.
  for (const event of events) {
    if (event.kind !== "result") continue;
    const existing = byCallId.get(event.call_id);
    if (existing) {
      existing.status = event.ok ? "ok" : "error";
      existing.summary = event.summary;
      existing.data = event.data;
      existing.error = event.error ?? undefined;
      if (!existing.tool && event.tool) existing.tool = event.tool;
    } else {
      // Result without a preceding call event (trace was truncated mid-stream).
      byCallId.set(event.call_id, {
        callId: event.call_id,
        tool: event.tool ?? "tool",
        status: event.ok ? "ok" : "error",
        summary: event.summary,
        data: event.data,
        error: event.error ?? undefined,
      });
    }
  }
  return [...byCallId.values()];
}

function ToolCalls({ entries }: { entries: ToolCallEntry[] }) {
  const [openId, setOpenId] = useState<string | null>(null);
  if (entries.length === 0) return null;
  return (
    <ul aria-label="Tool calls" className="mr-auto flex w-full max-w-2xl flex-col gap-1.5">
      {entries.map((entry) => {
        const open = openId === entry.callId;
        const Icon = entry.status === "pending" ? LoaderCircle : entry.status === "ok" ? Check : X;
        const iconClass =
          entry.status === "pending"
            ? "animate-spin text-fg-muted"
            : entry.status === "ok"
              ? "text-success"
              : "text-danger";
        return (
          <li key={entry.callId} className="rounded-lg border border-border bg-surface-1 text-sm">
            <button
              type="button"
              onClick={() => setOpenId(open ? null : entry.callId)}
              aria-expanded={open}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left"
            >
              <Icon className={`size-3.5 shrink-0 ${iconClass}`} aria-hidden />
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-fg">{entry.tool}</span>
              {entry.summary && entry.status !== "pending" && (
                <span className="min-w-0 flex-[2] truncate text-xs text-fg-muted">{entry.summary}</span>
              )}
              <ChevronDown
                className={`size-3.5 shrink-0 text-fg-subtle transition-transform ${open ? "rotate-180" : ""}`}
                aria-hidden
              />
            </button>
            {open && (
              <div className="space-y-2 border-t border-border px-3 py-2 font-mono text-xs leading-relaxed text-fg-muted">
                {entry.input !== undefined && (
                  <div>
                    <p className="text-fg-subtle">input</p>
                    <pre className="mt-0.5 overflow-x-auto rounded bg-surface-2 p-2 text-fg">{JSON.stringify(entry.input, null, 2)}</pre>
                  </div>
                )}
                {entry.data !== undefined && (
                  <div>
                    <p className="text-fg-subtle">output</p>
                    <AssetPreview data={entry.data} />
                    <pre className="mt-0.5 overflow-x-auto rounded bg-surface-2 p-2 text-fg">{JSON.stringify(entry.data, null, 2)}</pre>
                  </div>
                )}
                {entry.error && (
                  <div>
                    <p className="text-fg-subtle">error</p>
                    <pre className="mt-0.5 overflow-x-auto rounded bg-danger-subtle p-2 text-danger">{JSON.stringify(entry.error, null, 2)}</pre>
                  </div>
                )}
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function AgentActivity({ activity }: { activity: Activity }) {
  const source = activity.source ?? "Agent";

  return (
    <section
      aria-label="Agent activity"
      aria-live="polite"
      className="mr-auto w-full max-w-2xl px-1 py-2 text-sm"
    >
      <p className="text-base leading-relaxed text-fg">{activity.message}</p>
      <div className="mt-3 flex items-center gap-3 text-fg-muted">
        <LoaderCircle
          className="size-5 shrink-0 animate-spin"
          aria-hidden
        />
        <span className="min-w-0 truncate text-sm">{source}</span>
      </div>
    </section>
  );
}

function ApprovalBanner({
  approval,
  onDecide,
}: {
  approval: ApprovalPrompt;
  onDecide: (approved: boolean) => void;
}) {
  return (
    <section
      aria-label="Approval required"
      className="mr-auto w-full max-w-2xl rounded-xl border border-warning bg-warning-subtle p-4 text-sm"
    >
      <div className="flex items-center gap-2 font-medium text-fg">
        <ShieldAlert className="size-4 shrink-0 text-warning" aria-hidden />
        <span>
          Approve <span className="font-mono">{approval.tool}</span>?
        </span>
      </div>
      <p className="mt-2 text-fg-muted">
        The agent wants to run a tool that requires your approval. Review the
        input before deciding.
      </p>
      {approval.input !== undefined && (
        <pre className="mt-2 max-h-48 overflow-auto rounded-lg bg-surface-1 p-2 font-mono text-xs leading-relaxed text-fg">
          {JSON.stringify(approval.input, null, 2)}
        </pre>
      )}
      <div className="mt-3 flex gap-2">
        <button
          type="button"
          onClick={() => onDecide(true)}
          className="min-h-11 rounded-xl bg-accent px-4 text-sm font-medium text-accent-contrast hover:bg-accent-hover"
        >
          Approve
        </button>
        <button
          type="button"
          onClick={() => onDecide(false)}
          className="min-h-11 rounded-xl border border-border px-4 text-sm text-danger hover:bg-danger-subtle"
        >
          Deny
        </button>
      </div>
    </section>
  );
}

function ModelPicker({
  models,
  value,
  onChange,
  disabled = false,
}: {
  models: EnabledModel[];
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const groups = useMemo(() => {
    const result = new Map<string, EnabledModel[]>();
    for (const model of models) {
      const current = result.get(model.provider_name) ?? [];
      current.push(model);
      result.set(model.provider_name, current);
    }
    return result;
  }, [models]);

  const labels = useMemo(() => {
    const result = new Map<string, string>();
    for (const model of models) result.set(modelOptionValue(model), model.model_id);
    return result;
  }, [models]);

  return (
    <div className="flex min-w-0 items-center gap-2 text-xs text-fg-muted">
      <span className="shrink-0">Model</span>
      <Select.Root
        value={value}
        onValueChange={(next) => next !== null && onChange(next)}
        disabled={disabled || models.length === 0}
      >
        <Select.Trigger
          aria-label="Conversation model"
          className="flex min-h-11 min-w-0 max-w-64 items-center gap-2 rounded-xl border border-border bg-surface-1 px-3 font-mono text-xs text-fg transition-colors duration-150 hover:bg-surface-2 disabled:opacity-60"
        >
          <Select.Value className="min-w-0 flex-1 truncate text-left">
            {(current: string) =>
              models.length === 0 ? "No enabled models" : (labels.get(current) ?? "Select model")
            }
          </Select.Value>
          <Select.Icon className="shrink-0 text-fg-subtle">
            <ChevronDown className="size-4" aria-hidden />
          </Select.Icon>
        </Select.Trigger>
        <Select.Portal>
          <Select.Positioner side="top" align="start" sideOffset={6} alignItemWithTrigger={false} className="z-50">
            <Select.Popup className="max-h-72 min-w-56 overflow-y-auto rounded-xl border border-border bg-surface-3 p-1 shadow-lg">
              {[...groups.entries()].map(([provider, entries]) => (
                <Select.Group key={provider}>
                  <Select.GroupLabel className="px-3 py-1.5 text-[10px] font-medium uppercase tracking-wide text-fg-subtle">
                    {provider}
                  </Select.GroupLabel>
                  {entries.map((model) => (
                    <Select.Item
                      key={modelOptionValue(model)}
                      value={modelOptionValue(model)}
                      className="flex min-h-9 cursor-pointer items-center gap-2 rounded-lg px-3 font-mono text-xs text-fg-muted data-[highlighted]:bg-surface-2 data-[highlighted]:text-fg"
                    >
                      <Select.ItemIndicator className="inline-flex w-4 shrink-0 items-center text-accent">
                        <Check className="size-3.5" aria-hidden />
                      </Select.ItemIndicator>
                      <Select.ItemText className="truncate">{model.model_id}</Select.ItemText>
                    </Select.Item>
                  ))}
                </Select.Group>
              ))}
            </Select.Popup>
          </Select.Positioner>
        </Select.Portal>
      </Select.Root>
    </div>
  );
}

function NewConversation() {
  const navigate = useNavigate();
  const models = useEnabledModels();
  const create = useCreateConversation();
  const [draft, setDraft] = useState("");
  const [selectedModel, setSelectedModel] = useState("");

  useEffect(() => {
    const available = models.data ?? [];
    if (available.length === 0) {
      setSelectedModel("");
      return;
    }
    setSelectedModel((current) =>
      available.some((m) => modelOptionValue(m) === current)
        ? current
        : modelOptionValue(available[0]!),
    );
  }, [models.data]);

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const content = draft.trim();
    const model = parseModelOptionValue(selectedModel);
    if (!content || create.isPending || !model) return;
    const title =
      content.length > 48 ? `${content.slice(0, 48).trimEnd()}…` : content;
    create.mutate(
      { title, endpoint_id: model.provider_id, model_id: model.model_id },
      {
        onSuccess: (conversation) =>
          navigate(`/conversations/${conversation.id}`, {
            state: { firstMessage: content, modelValue: selectedModel },
          }),
      },
    );
  };

  return (
    <div className="flex h-full flex-col items-center justify-center gap-8 px-4 py-10">
      <h1 className="text-center text-2xl font-semibold tracking-tight text-fg">
        Where should we begin?
      </h1>
      <form
        aria-label="New conversation composer"
        onSubmit={submit}
        className="mx-auto flex w-full max-w-2xl flex-col gap-1 rounded-2xl border border-border bg-surface-1 p-2 shadow-md"
      >
        <textarea
          rows={2}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
          autoFocus
          placeholder="Ask Native GPT"
          aria-label="Message"
          className="max-h-40 min-h-11 flex-1 resize-none rounded-xl bg-transparent px-3 py-2.5 text-base text-fg no-focus-ring placeholder:text-fg-subtle"
        />
        <div className="flex items-center gap-1">
          <button
            type="button"
            disabled
            title="Attachments (coming soon)"
            aria-label="Attachments (coming soon)"
            className="inline-flex min-h-11 min-w-11 cursor-not-allowed items-center justify-center rounded-xl text-fg-subtle/50"
          >
            <Plus className="size-5" aria-hidden />
          </button>
          <div className="flex-1" />
          <ModelPicker
            models={models.data ?? []}
            value={selectedModel}
            onChange={setSelectedModel}
            disabled={create.isPending}
          />
          <button
            type="submit"
            disabled={!draft.trim() || !selectedModel || create.isPending}
            aria-label="Send message"
            className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-xl bg-accent text-accent-contrast hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40"
          >
            {create.isPending ? (
              <LoaderCircle className="size-5 animate-spin" aria-hidden />
            ) : (
              <SendHorizontal className="size-5" aria-hidden />
            )}
          </button>
        </div>
      </form>
      {models.isSuccess && models.data.length === 0 && (
        <p role="status" className="max-w-2xl text-center text-xs text-warning">
          No models are enabled. Enable one in Settings → Providers.
        </p>
      )}
      {models.isError && (
        <p role="alert" className="text-sm text-danger">
          Models could not be loaded. Configure a provider in Settings first.
        </p>
      )}
      {create.isError && (
        <p role="alert" className="text-sm text-danger">
          {create.error.message}
        </p>
      )}
    </div>
  );
}

export default function ChatPage() {
  const { conversationId } = useParams<{ conversationId: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  const conversation = useConversation(conversationId);
  const messages = useMessages(conversationId);
  const projects = useProjects();
  const enabledModels = useEnabledModels();
  const updateConversation = useUpdateConversation();
  const send = useSendMessage(conversationId ?? "");
  const cancel = useCancelRun();
  const [draft, setDraft] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  // Live agent-run state is per conversation in the global store, so
  // navigating away mid-run never leaks one conversation's run into another.
  const live = useRunStore((s) =>
    conversationId ? (s.byConversation[conversationId] ?? EMPTY_LIVE_RUN) : EMPTY_LIVE_RUN,
  );
  const startRun = useRunStore((s) => s.startRun);
  const stopRun = useRunStore((s) => s.stopRun);
  const clearApproval = useRunStore((s) => s.clearApproval);
  const { activeRun, streamText, activity, streamError, toolCalls, approval } = live;
  const autoSentRef = useRef(false);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const autoScrollRef = useRef(true);

  // Landing from the new-conversation composer: send the first message
  // immediately so it streams live, then clear the navigation state.
  useEffect(() => {
    const state = location.state as
      | { firstMessage?: string; modelValue?: string }
      | null;
    if (!state?.firstMessage || autoSentRef.current || !conversationId) return;
    if (messages.isPending) return;
    if ((messages.data?.length ?? 0) > 0) {
      autoSentRef.current = true;
      return;
    }
    const model = parseModelOptionValue(state.modelValue ?? "");
    if (!model) return;
    autoSentRef.current = true;
    if (state.modelValue) setSelectedModel(state.modelValue);
    send.mutate(
      {
        content: state.firstMessage,
        endpoint_id: model.provider_id,
        model_id: model.model_id,
      },
      { onSuccess: ({ run }) => startRun(conversationId, run) },
    );
    navigate(location.pathname, { replace: true, state: null });
  }, [location, conversationId, messages.isPending, messages.data, navigate, send, startRun]);

  useEffect(() => {
    const available = enabledModels.data ?? [];
    if (available.length === 0) {
      setSelectedModel("");
      return;
    }
    const item = conversation.data;
    const workspace = projects.data?.find((project) => project.id === item?.project_id);
    const providerId = item?.endpoint_id ?? item?.provider_id ?? workspace?.endpoint_id;
    const modelId = item?.model_id ?? workspace?.model_id;
    const persisted =
      providerId && modelId
        ? modelOptionValue({ provider_id: providerId, model_id: modelId })
        : "";
    const hasPersisted = available.some((model) => modelOptionValue(model) === persisted);
    setSelectedModel((current) => {
      if (hasPersisted) return persisted;
      if (available.some((model) => modelOptionValue(model) === current)) return current;
      return modelOptionValue(available[0]!);
    });
  }, [conversation.data, enabledModels.data, projects.data]);

  // run.* WS events are handled by the global router in lib/runStore.ts,
  // which dispatches each event to the slice of the conversation that owns
  // the run — no per-page subscription needed here.

  // ── Auto-scroll to bottom ──────────────────────────────────────────────────

  // Timestamp (monotonic) of the last content change. Set in RAF so it is
  // guaranteed to run after the DOM has been updated but before the browser
  // processes subsequent scroll events — letting us distinguish programmatic
  // scrolls (happen before the RAF fires) from user scrolls (happen after).
  const contentChangedAtRef = useRef(0);
  const rafIdRef = useRef(0);

  // Track when content changes so the scroll handler can distinguish
  // programmatic scrolls from user-initiated ones.
  useEffect(() => {
    contentChangedAtRef.current = performance.now();
    return () => cancelAnimationFrame(rafIdRef.current);
  }, [messages.data, activeRun, streamText, streamError, send.isError, toolCalls, approval]);

  // Scroll-to-bottom after every paint whenever content changes.
  useLayoutEffect(() => {
    if (!autoScrollRef.current) return;
    const el = scrollContainerRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight - el.clientHeight, behavior: "auto" });
  }, [messages.data, activeRun, streamText, streamError, send.isError, toolCalls, approval]);

  // Disable auto-scroll when the user manually scrolls away from the bottom.
  useEffect(() => {
    const THRESHOLD = 50; // px from bottom to count as "at bottom"

    const checkAtBottom = () => {
      const el = scrollContainerRef.current;
      if (!el) return false;
      return el.scrollTop + el.clientHeight >= el.scrollHeight - THRESHOLD;
    };

    const onScroll = () => {
      if (!autoScrollRef.current) return;

      const now = performance.now();
      const recentlyChanged = now - contentChangedAtRef.current < 250;

      if (checkAtBottom()) {
        if (recentlyChanged) {
          // User scrolled down during a content update → follow along.
          autoScrollRef.current = true;
        } else {
          // Settled at bottom after scrolling up → re-enable auto-scroll.
          autoScrollRef.current = true;
          onIdle();
        }
      } else {
        if (!recentlyChanged) {
          // User scrolled away from bottom and content has settled → break auto-scroll.
          autoScrollRef.current = false;
        }
      }
    };

    const onIdle = () => {
      rafIdRef.current = requestAnimationFrame(() => {
        contentChangedAtRef.current = 0;
      });
    };

    const el = scrollContainerRef.current;
    el?.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      el?.removeEventListener("scroll", onScroll);
      cancelAnimationFrame(rafIdRef.current);
    };
  }, []);

  if (!conversationId) return <NewConversation />;

  const chooseModel = (value: string) => {
    setSelectedModel(value);
    const selected = parseModelOptionValue(value);
    if (!selected) return;
    updateConversation.mutate({
      id: conversationId,
      input: { endpoint_id: selected.provider_id, model_id: selected.model_id },
    });
  };

  // Forward the user's decision to the sidecar via WS (host relays any
  // envelope). The sidecar's run.approval_resolved broadcast confirms; we
  // clear optimistically since a repeated resolve is a harmless no-op.
  const decideApproval = (approved: boolean) => {
    if (!approval) return;
    socket.send({
      protocol: PROTOCOL_VERSION,
      type: "run.approve",
      request_id: crypto.randomUUID(),
      timestamp: new Date().toISOString(),
      payload: { approval_id: approval.approvalId, approved },
    });
    clearApproval(conversationId);
  };

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const content = draft.trim();
    const model = parseModelOptionValue(selectedModel);
    if (!content || activeRun || send.isPending || !model) return;
    setDraft("");
    // Always send the picker selection with the message so the model the user
    // sees is the model that runs — even if the conversation row still holds a
    // stale or since-disabled model (the PATCH in chooseModel may not have
    // happened, e.g. when the picker fell back to the first enabled model).
    send.mutate(
      {
        content,
        endpoint_id: model.provider_id,
        model_id: model.model_id,
      },
      {
        // Register the run against the conversation it was SENT to — the user
        // may have navigated elsewhere before this response arrives.
        onSuccess: ({ run }) => startRun(conversationId, run),
        onError: () => setDraft(content),
      },
    );
  };

  return (
    <div className="flex h-full flex-col">
      <header className="flex min-h-16 flex-wrap items-center justify-between gap-3 border-b border-border bg-surface-1 px-4 py-2 sm:px-6">
        <div className="min-w-0">
          <h1 className="truncate text-sm font-semibold text-fg">
            {conversation.data?.title ?? "Conversation"}
          </h1>
          {updateConversation.isError && (
            <p role="alert" className="text-xs text-danger">Could not change the model.</p>
          )}
        </div>
        <ModelPicker
          models={enabledModels.data ?? []}
          value={selectedModel}
          onChange={chooseModel}
          disabled={updateConversation.isPending || Boolean(activeRun)}
        />
      </header>

      <div
        ref={scrollContainerRef}
        className="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6"
        aria-busy={messages.isPending}
      >
        <div className="mx-auto flex w-full max-w-2xl flex-col gap-4">
          {messages.isPending && (
            <p className="flex items-center gap-2 text-sm text-fg-muted">
              <LoaderCircle className="size-4 animate-spin" aria-hidden /> Loading messages…
            </p>
          )}
          {messages.isError && (
            <p role="alert" className="rounded-xl bg-danger-subtle p-3 text-sm text-danger">
              {messages.error.message}
            </p>
          )}
          {messages.data?.length === 0 && !activeRun && (
            <p className="py-16 text-center text-sm text-fg-muted">Send the first message.</p>
          )}
          {messages.data?.map((message) => {
            const user = message.role === "user";
            const text = messageText(message.content ?? message.content_json);
            // Phase 1.5: persisted tool-call trace for this assistant message.
            // Empty for user/system messages and tool-less runs. Parsed
            // defensively (see parseToolEvents) — malformed JSON degrades to
            // "no tool calls shown" rather than crashing the conversation view.
            const historicalToolCalls = user
              ? []
              : toolEntriesFromPersisted(parseToolEvents(message.tool_events_json));
            return (
              <div key={message.id} className="contents">
                {historicalToolCalls.length > 0 && <ToolCalls entries={historicalToolCalls} />}
                <article
                  aria-label={`${message.role} message`}
                  className={`max-w-[85%] whitespace-pre-wrap rounded-sm px-4 py-3 text-sm leading-relaxed ${
                    user
                      ? "ml-auto rounded-br-none bg-accent text-accent-contrast"
                      : "mr-auto rounded-bl-none border border-border bg-surface-1 text-fg"
                  }`}
                >
                  {user
                    ? <PlainMessage content={text} />
                    : <MarkdownMessage content={text} />}
                </article>
              </div>
            );
          })}
          {activeRun && <ToolCalls entries={toolCalls} />}
          {activeRun && approval && <ApprovalBanner approval={approval} onDecide={decideApproval} />}
          {activeRun && !approval && !streamText && <AgentActivity activity={activity} />}
          {streamText && (
            <article
              aria-label="assistant message streaming"
              aria-live="polite"
              className="mr-auto max-w-[85%] whitespace-pre-wrap rounded-sm rounded-bl-none border border-border bg-surface-1 px-4 py-3 text-sm leading-relaxed text-fg"
            >
              {streamText ? <PlainMessage content={streamText} /> : <span className="text-fg-subtle">Thinking…</span>}
            </article>
          )}
          {(streamError || send.isError) && (
            <p role="alert" className="rounded-xl bg-danger-subtle p-3 text-sm text-danger">
              {streamError ?? send.error?.message ?? "Message failed."}
            </p>
          )}

          {/* Anchor element for scroll-to-bottom targeting */}
          <div aria-hidden style={{ height: 1 }} />
        </div>
      </div>

      <div className="px-4 pb-4 sm:px-6" style={{ paddingBottom: "max(env(safe-area-inset-bottom), 1rem)" }}>
        {enabledModels.isSuccess && enabledModels.data.length === 0 && (
          <p role="status" className="mx-auto mb-2 max-w-2xl text-xs text-warning">
            No models are enabled. Enable one in Settings → Providers.
          </p>
        )}
        <form
          aria-label="Message composer"
          onSubmit={submit}
          className="mx-auto flex w-full max-w-2xl items-end gap-2 rounded-2xl border border-border bg-surface-1 p-2 shadow-md"
        >
          <textarea
            rows={1}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
            disabled={Boolean(activeRun)}
            placeholder="Message Native GPT"
            aria-label="Message"
            className="max-h-40 min-h-11 flex-1 resize-none rounded-xl bg-transparent px-3 py-2.5 text-base text-fg no-focus-ring placeholder:text-fg-subtle disabled:opacity-60"
          />
          {activeRun ? (
            <button
              type="button"
              onClick={() =>
                cancel.mutate(activeRun.id, {
                  onSuccess: () => stopRun(conversationId),
                })
              }
              disabled={cancel.isPending}
              aria-label="Stop response"
              className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-xl border border-border text-danger hover:bg-danger-subtle disabled:opacity-50"
            >
              {cancel.isPending ? (
                <LoaderCircle className="size-5 animate-spin" aria-hidden />
              ) : (
                <CircleStop className="size-5" aria-hidden />
              )}
            </button>
          ) : (
            <button
              type="submit"
              disabled={!draft.trim() || !selectedModel || send.isPending}
              aria-label="Send message"
              className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-xl bg-accent text-accent-contrast hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40"
            >
              {send.isPending ? (
                <LoaderCircle className="size-5 animate-spin" aria-hidden />
              ) : (
                <SendHorizontal className="size-5" aria-hidden />
              )}
            </button>
          )}
        </form>
      </div>
    </div>
  );
}
