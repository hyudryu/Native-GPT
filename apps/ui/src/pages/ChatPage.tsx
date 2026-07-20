import { useEffect, useLayoutEffect, useRef, useState, useMemo } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate, useParams } from "react-router";
import {
  CircleStop,
  LoaderCircle,
  Plus,
  SendHorizontal,
} from "lucide-react";
import {
  messageText,
  modelOptionValue,
  parseModelOptionValue,
  useCancelRun,
  useConversation,
  useCreateConversation,
  useEnabledModels,
  useMessages,
  useProjects,
  useSendMessage,
  useUpdateConversation,
  type EnabledModel,
  type RunRef,
} from "../lib/dataApi";
import { socket } from "../lib/ws";
import { MarkdownMessage, PlainMessage } from "../components/MarkdownMessage";

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

  return (
    <label className="flex min-w-0 items-center gap-2 text-xs text-fg-muted">
      <span className="shrink-0">Model</span>
      <select
        aria-label="Conversation model"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled || models.length === 0}
        className="min-h-11 min-w-0 max-w-64 rounded-xl border border-border bg-surface-1 px-3 font-mono text-xs text-fg disabled:opacity-60"
      >
        {models.length === 0 ? (
          <option value="">No enabled models</option>
        ) : (
          [...groups.entries()].map(([provider, entries]) => (
            <optgroup key={provider} label={provider}>
              {entries.map((model) => (
                <option key={modelOptionValue(model)} value={modelOptionValue(model)}>
                  {model.model_id}
                </option>
              ))}
            </optgroup>
          ))
        )}
      </select>
    </label>
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
          className="max-h-40 min-h-11 flex-1 resize-none rounded-xl bg-transparent px-3 py-2.5 text-base text-fg placeholder:text-fg-subtle"
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
  const queryClient = useQueryClient();
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
  const [activeRun, setActiveRun] = useState<RunRef | null>(null);
  const [streamText, setStreamText] = useState("");
  const [streamError, setStreamError] = useState<string | null>(null);
  const autoSentRef = useRef(false);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const autoScrollRef = useRef(true);
  const contentKeyRef = useRef(0);

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
    setStreamText("");
    setStreamError(null);
    send.mutate(
      {
        content: state.firstMessage,
        endpoint_id: model.provider_id,
        model_id: model.model_id,
      },
      { onSuccess: ({ run }) => setActiveRun(run) },
    );
    navigate(location.pathname, { replace: true, state: null });
  }, [location, conversationId, messages.isPending, messages.data, navigate, send]);

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

  useEffect(() => {
    if (!activeRun || !conversationId) return;
    const matches = (requestId: string, payload: Record<string, unknown>) =>
      requestId === activeRun.request_id || payload.run_id === activeRun.id;
    const offDelta = socket.on("run.text_delta", (envelope) => {
      const payload = envelope.payload as Record<string, unknown>;
      if (!matches(envelope.request_id, payload) || typeof payload.text !== "string") return;
      setStreamText((current) => current + payload.text);
    });
    const offCompleted = socket.on("run.completed", (envelope) => {
      const payload = envelope.payload as Record<string, unknown>;
      if (!matches(envelope.request_id, payload)) return;
      setActiveRun(null);
      void queryClient
        .invalidateQueries({ queryKey: ["conversations", conversationId, "messages"] })
        .then(() => setStreamText(""));
    });
    const offFailed = socket.on("run.failed", (envelope) => {
      const payload = envelope.payload as Record<string, unknown>;
      if (!matches(envelope.request_id, payload)) return;
      const error = payload.error as { message?: string } | undefined;
      setStreamError(error?.message ?? "The response failed.");
      setActiveRun(null);
      // The host persists any partial assistant text on failure/cancel — refetch.
      void queryClient
        .invalidateQueries({ queryKey: ["conversations", conversationId, "messages"] })
        .then(() => setStreamText(""));
    });
    return () => {
      offDelta();
      offCompleted();
      offFailed();
    };
  }, [activeRun, conversationId, queryClient]);

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
  }, [messages.data, activeRun, streamText, streamError, send.isError]);

  // Scroll-to-bottom after every paint whenever content changes.
  useLayoutEffect(() => {
    if (!autoScrollRef.current) return;
    const el = scrollContainerRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight - el.clientHeight, behavior: "auto" });
  }, [messages.data, activeRun, streamText, streamError, send.isError]);

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

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const content = draft.trim();
    const model = parseModelOptionValue(selectedModel);
    if (!content || activeRun || send.isPending || !model) return;
    setDraft("");
    setStreamText("");
    setStreamError(null);
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
        onSuccess: ({ run }) => setActiveRun(run),
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
            return (
              <article
                key={message.id}
                aria-label={`${message.role} message`}
                className={`max-w-[88%] whitespace-pre-wrap rounded-2xl px-4 py-3 text-sm leading-relaxed ${
                  user
                    ? "ml-auto bg-accent text-accent-contrast"
                    : "mr-auto border border-border bg-surface-1 text-fg"
                }`}
              >
                {user
                  ? <PlainMessage content={text} />
                  : <MarkdownMessage content={text} />}
              </article>
            );
          })}
          {(activeRun || streamText) && (
            <article
              aria-label="assistant message streaming"
              aria-live="polite"
              className="mr-auto max-w-[88%] whitespace-pre-wrap rounded-2xl border border-border bg-surface-1 px-4 py-3 text-sm leading-relaxed text-fg"
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
            className="max-h-40 min-h-11 flex-1 resize-none rounded-xl bg-transparent px-3 py-2.5 text-base text-fg placeholder:text-fg-subtle disabled:opacity-60"
          />
          {activeRun ? (
            <button
              type="button"
              onClick={() =>
                cancel.mutate(activeRun.id, {
                  onSuccess: () => {
                    setActiveRun(null);
                    setStreamError("Response stopped.");
                    // Partial assistant text is persisted on cancel — refetch.
                    void queryClient
                      .invalidateQueries({ queryKey: ["conversations", conversationId, "messages"] })
                      .then(() => setStreamText(""));
                  },
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
