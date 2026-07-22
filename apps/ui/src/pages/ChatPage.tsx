import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router";
import {
  CircleStop,
  Github,
  LoaderCircle,
  MessageSquareDashed,
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

type Activity = { message: string; source?: string };

function AgentActivity({ activity }: { activity: Activity }) {
  const source = activity.source ?? "Agent";
  const githubActivity = source.toLowerCase().includes("github");
  const Icon = githubActivity ? Github : LoaderCircle;

  return (
    <section
      aria-label="Agent activity"
      aria-live="polite"
      className="mr-auto w-full max-w-2xl px-1 py-2 text-sm"
    >
      <p className="text-base leading-relaxed text-fg">{activity.message}</p>
      <div className="mt-3 flex items-center gap-3 text-fg-muted">
        <Icon
          className={`size-5 shrink-0 ${githubActivity ? "" : "animate-spin"}`}
          aria-hidden
        />
        <span className="min-w-0 truncate text-sm">{source}</span>
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
  const first = models.data?.[0];

  const start = () => {
    create.mutate(
      {
        title: "New conversation",
        ...(first
          ? { endpoint_id: first.provider_id, model_id: first.model_id }
          : {}),
      },
      { onSuccess: (conversation) => navigate(`/conversations/${conversation.id}`) },
    );
  };

  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 px-6 text-center">
      <div className="flex size-14 items-center justify-center rounded-2xl bg-surface-1 shadow-sm">
        <MessageSquareDashed className="size-7 text-fg-subtle" aria-hidden />
      </div>
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Start a conversation</h1>
        <p className="mt-2 max-w-sm text-sm text-fg-muted">
          Create a local conversation, then choose any model enabled in Provider settings.
        </p>
      </div>
      {models.isError && (
        <p role="alert" className="text-sm text-danger">
          Models could not be loaded. You can still create the conversation and configure it later.
        </p>
      )}
      <button
        type="button"
        onClick={start}
        disabled={create.isPending}
        className="inline-flex min-h-11 items-center gap-2 rounded-xl bg-accent px-4 text-sm font-medium text-accent-contrast hover:bg-accent-hover disabled:opacity-50"
      >
        {create.isPending && <LoaderCircle className="size-4 animate-spin" aria-hidden />}
        Create conversation
      </button>
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
  const [activity, setActivity] = useState<Activity>({ message: "Thinking through the request" });
  const [streamError, setStreamError] = useState<string | null>(null);

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
    const offActivity = socket.on("run.activity", (envelope) => {
      const payload = envelope.payload as Record<string, unknown>;
      if (!matches(envelope.request_id, payload) || typeof payload.message !== "string") return;
      setActivity({
        message: payload.message,
        ...(typeof payload.source === "string" ? { source: payload.source } : {}),
      });
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
    });
    return () => {
      offDelta();
      offActivity();
      offCompleted();
      offFailed();
    };
  }, [activeRun, conversationId, queryClient]);

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
    setActivity({ message: "Thinking through the request" });
    setStreamError(null);
    send.mutate(
      { content },
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

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6" aria-busy={messages.isPending}>
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
                {messageText(message.content ?? message.content_json)}
              </article>
            );
          })}
          {activeRun && !streamText && <AgentActivity activity={activity} />}
          {streamText && (
            <article
              aria-label="assistant message streaming"
              aria-live="polite"
              className="mr-auto max-w-[88%] whitespace-pre-wrap rounded-2xl border border-border bg-surface-1 px-4 py-3 text-sm leading-relaxed text-fg"
            >
              {streamText || <span className="text-fg-subtle">Thinking…</span>}
            </article>
          )}
          {(streamError || send.isError) && (
            <p role="alert" className="rounded-xl bg-danger-subtle p-3 text-sm text-danger">
              {streamError ?? send.error?.message ?? "Message failed."}
            </p>
          )}
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
