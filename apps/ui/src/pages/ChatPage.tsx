import { useEffect, useLayoutEffect, useRef, useState, useMemo } from "react";
import { useLocation, useNavigate, useParams } from "react-router";
import { Menu } from "@base-ui-components/react/menu";
import { Select } from "@base-ui-components/react/select";
import {
  Brain,
  Check,
  ChevronDown,
  Circle,
  CircleStop,
  Globe,
  LoaderCircle,
  Minus,
  Paperclip,
  Plus,
  Puzzle,
  SendHorizontal,
  ShieldAlert,
  Target,
  Wrench,
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
  useSynthesizeNow,
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
  type OrchestrationProgress,
  type ToolCallEntry,
} from "../lib/runStore";
import {
  loadMaxDepth,
  loadThinkingMode,
  saveMaxDepth,
  saveThinkingMode,
  type MaxDepth,
  type ThinkingMode,
} from "../lib/thinkingMode";
import { MarkdownMessage, PlainMessage } from "../components/MarkdownMessage";
import { assetPayload, renderableAssetUrl } from "../lib/assetUrl";
import { useRemoteHosts } from "../lib/remoteHosts";
import { startBrowser } from "../features/browser/browserApi";
import { useBrowserStore } from "../features/browser/browserStore";

/**
 * If a tool result includes generated asset data (from the bridge's
 * comfyui_generate or openvoice_tts MCP tools), render the asset inline — an
 * <img> for images, a <video> player for video, an <audio> player for audio.
 * Bridge-direct asset URLs are bearer-protected (and possibly self-signed),
 * so they're rewritten to the desktop's same-origin proxy route for rendering.
 */
function AssetPreview({ data }: { data: unknown }) {
  const hosts = useRemoteHosts();
  const payload = assetPayload(data);
  if (!payload) return null;
  const rawUrl = payload.asset_url;
  const kind = payload.kind;
  if (typeof rawUrl !== "string" || !rawUrl) return null;
  const assetUrl = renderableAssetUrl(rawUrl, hosts.data);
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
            alt={(payload.prompt as string) ?? "Generated image"}
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

/** Absolute timestamp shown when hovering a message, e.g. "Jul 22, 2026, 9:41 PM". */
function formatTimestamp(iso: string): string {
  const time = new Date(iso);
  if (Number.isNaN(time.getTime())) return "";
  return time.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function ToolCalls({ entries }: { entries: ToolCallEntry[] }) {
  const [openId, setOpenId] = useState<string | null>(null);
  if (entries.length === 0) return null;
  return (
    <ul aria-label="Tool calls" className="mr-auto flex w-full max-w-[50.4rem] flex-col gap-1.5">
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
      className="mr-auto w-full max-w-[50.4rem] px-1 py-2 text-sm"
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
      className="mr-auto w-full max-w-[50.4rem] rounded-xl border border-warning bg-warning-subtle p-4 text-sm"
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

// ── Thinking mode (off / high / max) ─────────────────────────────────────────
// Per-message mode (design spec §6.1): the selection is persisted in
// localStorage as the default for the next send and forwarded to the runtime
// on POST /conversations/:id/messages. Persistence + legacy-key migration live
// in lib/thinkingMode.ts.

const THINKING_MODE_OPTIONS: { value: ThinkingMode; label: string; description: string }[] = [
  { value: "off", label: "Off", description: "Fastest, direct answer" },
  { value: "high", label: "High", description: "Model's own deep reasoning" },
  { value: "max", label: "Max", description: "Multi-agent critical analysis" },
];

const MAX_DEPTH_OPTIONS: { value: MaxDepth; label: string }[] = [
  { value: "quick", label: "Quick" },
  { value: "standard", label: "Standard" },
  { value: "deep", label: "Deep" },
];

function useThinkingMode(): [ThinkingMode, (mode: ThinkingMode) => void] {
  const [mode, setMode] = useState<ThinkingMode>(loadThinkingMode);
  const update = (next: ThinkingMode) => {
    setMode(next);
    saveThinkingMode(next);
  };
  return [mode, update];
}

function useMaxDepth(): [MaxDepth, (depth: MaxDepth) => void] {
  const [depth, setDepth] = useState<MaxDepth>(loadMaxDepth);
  const update = (next: MaxDepth) => {
    setDepth(next);
    saveMaxDepth(next);
  };
  return [depth, update];
}

function ThinkingModePicker({
  value,
  onChange,
  disabled = false,
}: {
  value: ThinkingMode;
  onChange: (value: ThinkingMode) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex min-w-0 items-center gap-2 text-xs text-fg-muted">
      <Select.Root
        value={value}
        onValueChange={(next) => next !== null && onChange(next as ThinkingMode)}
        disabled={disabled}
      >
        <Select.Trigger
          aria-label="Thinking mode"
          className="flex min-h-11 min-w-0 items-center gap-2 rounded-xl border border-border bg-surface-1 px-3 text-xs text-fg transition-colors duration-150 hover:bg-surface-2 disabled:opacity-60"
        >
          <Brain className="size-4 shrink-0 text-fg-subtle" aria-hidden />
          <Select.Value className="min-w-0 flex-1 truncate text-left">
            {(current: ThinkingMode) =>
              THINKING_MODE_OPTIONS.find((option) => option.value === current)?.label ?? current
            }
          </Select.Value>
          <Select.Icon className="shrink-0 text-fg-subtle">
            <ChevronDown className="size-4" aria-hidden />
          </Select.Icon>
        </Select.Trigger>
        <Select.Portal>
          <Select.Positioner side="top" align="end" sideOffset={6} alignItemWithTrigger={false} className="z-50">
            <Select.Popup className="min-w-56 rounded-xl border border-border bg-surface-3 p-1 shadow-lg">
              {THINKING_MODE_OPTIONS.map((option) => (
                <Select.Item
                  key={option.value}
                  value={option.value}
                  className="flex min-h-9 cursor-pointer items-center gap-2 rounded-lg px-3 py-1.5 text-xs text-fg-muted data-[highlighted]:bg-surface-2 data-[highlighted]:text-fg"
                >
                  <Select.ItemIndicator className="inline-flex w-4 shrink-0 items-center text-accent">
                    <Check className="size-3.5" aria-hidden />
                  </Select.ItemIndicator>
                  <Select.ItemText className="min-w-0 flex-1">
                    <span className="block truncate">{option.label}</span>
                    <span className="block truncate text-[11px] text-fg-subtle">
                      {option.description}
                    </span>
                  </Select.ItemText>
                </Select.Item>
              ))}
            </Select.Popup>
          </Select.Positioner>
        </Select.Portal>
      </Select.Root>
    </div>
  );
}

/** Depth preset for Max mode; only rendered while Max is selected (spec §6.2). */
function MaxDepthPicker({
  value,
  onChange,
  disabled = false,
}: {
  value: MaxDepth;
  onChange: (value: MaxDepth) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex min-w-0 items-center gap-2 text-xs text-fg-muted">
      <span className="shrink-0">Depth</span>
      <Select.Root
        value={value}
        onValueChange={(next) => next !== null && onChange(next as MaxDepth)}
        disabled={disabled}
      >
        <Select.Trigger
          aria-label="Max depth"
          className="flex min-h-11 min-w-0 items-center gap-2 rounded-xl border border-border bg-surface-1 px-3 text-xs text-fg transition-colors duration-150 hover:bg-surface-2 disabled:opacity-60"
        >
          <Select.Value className="min-w-0 flex-1 truncate text-left">
            {(current: MaxDepth) =>
              MAX_DEPTH_OPTIONS.find((option) => option.value === current)?.label ?? current
            }
          </Select.Value>
          <Select.Icon className="shrink-0 text-fg-subtle">
            <ChevronDown className="size-4" aria-hidden />
          </Select.Icon>
        </Select.Trigger>
        <Select.Portal>
          <Select.Positioner side="top" align="end" sideOffset={6} alignItemWithTrigger={false} className="z-50">
            <Select.Popup className="min-w-32 rounded-xl border border-border bg-surface-3 p-1 shadow-lg">
              {MAX_DEPTH_OPTIONS.map((option) => (
                <Select.Item
                  key={option.value}
                  value={option.value}
                  className="flex min-h-9 cursor-pointer items-center gap-2 rounded-lg px-3 text-xs text-fg-muted data-[highlighted]:bg-surface-2 data-[highlighted]:text-fg"
                >
                  <Select.ItemIndicator className="inline-flex w-4 shrink-0 items-center text-accent">
                    <Check className="size-3.5" aria-hidden />
                  </Select.ItemIndicator>
                  <Select.ItemText className="truncate">{option.label}</Select.ItemText>
                </Select.Item>
              ))}
            </Select.Popup>
          </Select.Positioner>
        </Select.Portal>
      </Select.Root>
    </div>
  );
}

// ── Max run panel (design spec §6.3) ─────────────────────────────────────────

function StepStatusIcon({ status }: { status: OrchestrationProgress["steps"][number]["status"] }) {
  switch (status) {
    case "complete":
      return <Check className="size-3.5 shrink-0 text-success" aria-label="complete" />;
    case "running":
      return (
        <LoaderCircle className="size-3.5 shrink-0 animate-spin text-fg-muted" aria-label="running" />
      );
    case "failed":
      return <X className="size-3.5 shrink-0 text-danger" aria-label="failed" />;
    case "skipped":
      return <Minus className="size-3.5 shrink-0 text-fg-subtle" aria-label="skipped" />;
    default:
      return <Circle className="size-3.5 shrink-0 text-fg-subtle" aria-label="pending" />;
  }
}

/** Compact one-line rendering of a step's detail payload (string or object). */
function stepDetailText(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (typeof detail !== "object" || detail === null) return "";
  return Object.entries(detail as Record<string, unknown>)
    .map(([key, value]) =>
      `${key}: ${Array.isArray(value) ? value.map(String).join(", ") : String(value)}`,
    )
    .join(" · ");
}

function formatTokenCount(value: number): string {
  if (value >= 1000) {
    const k = value / 1000;
    return `${k >= 100 ? Math.round(k) : k.toFixed(1)}k`;
  }
  return String(Math.round(value));
}

function formatSeconds(value: number): string {
  const total = Math.max(0, Math.round(value));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function BudgetBar({
  label,
  used,
  total,
  format,
}: {
  label: string;
  used: number;
  total: number;
  format: (value: number) => string;
}) {
  const ratio = total > 0 ? Math.min(1, Math.max(0, used / total)) : 0;
  return (
    <div className="flex min-w-0 flex-1 items-center gap-2">
      <span className="shrink-0 text-[11px] text-fg-subtle">{label}</span>
      <div
        role="progressbar"
        aria-label={`${label} budget`}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={used}
        className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-surface-2"
      >
        <div className="h-full rounded-full bg-accent" style={{ width: `${ratio * 100}%` }} />
      </div>
      <span className="shrink-0 font-mono text-[11px] text-fg-muted">
        {format(used)} / {format(total)}
      </span>
    </div>
  );
}

function MaxRunPanel({
  runId,
  orchestration,
  activity,
}: {
  runId: string;
  orchestration: OrchestrationProgress;
  activity: Activity;
}) {
  const synthesize = useSynthesizeNow();
  const synthesizing = orchestration.state === "SYNTHESIZE";
  const requested =
    orchestration.synthesizeRequested || synthesize.isPending || synthesize.isSuccess;

  return (
    <section
      aria-label="Max thinking progress"
      aria-live="polite"
      className="mr-auto w-full max-w-[50.4rem] rounded-xl border border-border bg-surface-1 p-3 text-sm"
    >
      <div className="flex items-center gap-2">
        <Brain className="size-4 shrink-0 text-accent" aria-hidden />
        <span className="font-medium text-fg">Max thinking</span>
        <span className="min-w-0 truncate text-xs text-fg-muted">{activity.message}</span>
      </div>

      {orchestration.steps.length > 0 && (
        <ul aria-label="Orchestration steps" className="mt-2 flex flex-col gap-1">
          {orchestration.steps.map((step) => {
            const detail = stepDetailText(step.detail);
            return (
              <li key={step.id} className="flex items-start gap-2 text-xs">
                <span className="mt-0.5 inline-flex">
                  <StepStatusIcon status={step.status} />
                </span>
                <span
                  className={`min-w-0 flex-1 ${
                    step.status === "pending" || step.status === "skipped"
                      ? "text-fg-subtle"
                      : "text-fg"
                  }`}
                >
                  {step.label}
                  {detail && (
                    <span className="block truncate text-[11px] text-fg-muted">{detail}</span>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border pt-2">
        <BudgetBar
          label="Tokens"
          used={orchestration.budgets.tokens_used}
          total={orchestration.budgets.token_budget}
          format={formatTokenCount}
        />
        <BudgetBar
          label="Time"
          used={orchestration.budgets.elapsed_s}
          total={orchestration.budgets.time_budget_s}
          format={formatSeconds}
        />
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => synthesize.mutate(runId)}
          disabled={requested || synthesizing}
          className="inline-flex min-h-9 items-center gap-2 rounded-xl border border-border px-3 text-xs text-fg-muted transition-colors duration-150 hover:bg-surface-2 hover:text-fg disabled:cursor-not-allowed disabled:opacity-50"
        >
          {synthesize.isPending && <LoaderCircle className="size-3.5 animate-spin" aria-hidden />}
          {synthesizing ? "Synthesizing…" : requested ? "Synthesis requested" : "Stop and synthesize now"}
        </button>
        {synthesize.isError && (
          <span role="alert" className="text-xs text-danger">
            {synthesize.error.message}
          </span>
        )}
      </div>
    </section>
  );
}

// ── Composer "+" menu ────────────────────────────────────────────────────────

function PlusMenu({
  onAddFiles,
  disabled = false,
}: {
  onAddFiles: (files: File[]) => void;
  disabled?: boolean;
}) {
  const navigate = useNavigate();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const itemClass =
    "flex min-h-9 cursor-pointer items-center gap-2 rounded-lg px-3 text-sm text-fg-muted outline-none data-[highlighted]:bg-surface-2 data-[highlighted]:text-fg data-[disabled]:cursor-not-allowed data-[disabled]:opacity-50";

  /** Open the browser panel at a 50/50 split; warm up Chromium if installed. */
  const openBrowserPanel = () => {
    const s = useBrowserStore.getState();
    s.setSplitWidth(s.containerWidth > 0 ? s.containerWidth * 0.5 : 640);
    if (s.installStatus === "ready" && s.processStatus === "stopped") {
      // Fire-and-forget — the viewport surfaces install/start errors itself.
      startBrowser().catch(() => {});
    }
  };
  return (
    <>
      <Menu.Root>
        <Menu.Trigger
          aria-label="Add to message"
          disabled={disabled}
          className="inline-flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-xl text-fg-subtle transition-colors duration-150 hover:bg-surface-2 hover:text-fg disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Plus className="size-5" aria-hidden />
        </Menu.Trigger>
        <Menu.Portal>
          <Menu.Positioner side="top" align="start" sideOffset={6} className="z-50">
            <Menu.Popup className="min-w-44 rounded-xl border border-border bg-surface-3 p-1 shadow-lg">
              <Menu.Item className={itemClass} onClick={() => fileInputRef.current?.click()}>
                <Paperclip className="size-4 shrink-0" aria-hidden />
                Add file
              </Menu.Item>
              <Menu.Item className={itemClass} onClick={openBrowserPanel}>
                <Globe className="size-4 shrink-0" aria-hidden />
                Browser
              </Menu.Item>
              <Menu.Item className={itemClass} onClick={() => navigate("/apps/tools")}>
                <Wrench className="size-4 shrink-0" aria-hidden />
                Tools
              </Menu.Item>
              <Menu.Item className={itemClass} disabled>
                <Puzzle className="size-4 shrink-0" aria-hidden />
                Plugins
                <span className="ml-auto text-[10px] uppercase tracking-wide text-fg-subtle">Soon</span>
              </Menu.Item>
              <Menu.Item className={itemClass} disabled>
                <Target className="size-4 shrink-0" aria-hidden />
                Add Goal
                <span className="ml-auto text-[10px] uppercase tracking-wide text-fg-subtle">Soon</span>
              </Menu.Item>
            </Menu.Popup>
          </Menu.Positioner>
        </Menu.Portal>
      </Menu.Root>
      <input
        ref={fileInputRef}
        type="file"
        multiple
        hidden
        aria-hidden
        tabIndex={-1}
        onChange={(event) => {
          const selected = event.target.files ? [...event.target.files] : [];
          if (selected.length > 0) onAddFiles(selected);
          // Reset so picking the same file twice still fires onChange.
          event.target.value = "";
        }}
      />
    </>
  );
}

function AttachmentChips({
  files,
  onRemove,
}: {
  files: File[];
  onRemove: (index: number) => void;
}) {
  if (files.length === 0) return null;
  return (
    <ul aria-label="Attached files" className="flex flex-wrap gap-1.5 px-1 pt-1">
      {files.map((file, index) => (
        <li
          key={`${file.name}-${index}`}
          className="flex items-center gap-1.5 rounded-lg border border-border bg-surface-2 px-2 py-1 text-xs text-fg"
        >
          <Paperclip className="size-3 shrink-0 text-fg-subtle" aria-hidden />
          <span className="max-w-40 truncate">{file.name}</span>
          <button
            type="button"
            onClick={() => onRemove(index)}
            aria-label={`Remove ${file.name}`}
            className="shrink-0 text-fg-subtle hover:text-danger"
          >
            <X className="size-3.5" aria-hidden />
          </button>
        </li>
      ))}
    </ul>
  );
}

function NewConversation() {
  const navigate = useNavigate();
  const models = useEnabledModels();
  const create = useCreateConversation();
  const [draft, setDraft] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [thinkingMode, setThinkingMode] = useThinkingMode();
  const [maxDepth, setMaxDepth] = useMaxDepth();

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
        className="mx-auto flex w-full max-w-[50.4rem] flex-col gap-1 rounded-2xl border border-border bg-surface-1 p-2 shadow-md"
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
        <AttachmentChips
          files={files}
          onRemove={(index) => setFiles((current) => current.filter((_, i) => i !== index))}
        />
        <div className="flex items-center gap-1">
          <PlusMenu
            onAddFiles={(added) => setFiles((current) => [...current, ...added])}
            disabled={create.isPending}
          />
          <div className="flex-1" />
          <ModelPicker
            models={models.data ?? []}
            value={selectedModel}
            onChange={setSelectedModel}
            disabled={create.isPending}
          />
          <ThinkingModePicker
            value={thinkingMode}
            onChange={setThinkingMode}
            disabled={create.isPending}
          />
          {thinkingMode === "max" && (
            <MaxDepthPicker
              value={maxDepth}
              onChange={setMaxDepth}
              disabled={create.isPending}
            />
          )}
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
        <p role="status" className="max-w-[50.4rem] text-center text-xs text-warning">
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
  const [files, setFiles] = useState<File[]>([]);
  const [thinkingMode, setThinkingMode] = useThinkingMode();
  const [maxDepth, setMaxDepth] = useMaxDepth();
  // Live agent-run state is per conversation in the global store, so
  // navigating away mid-run never leaks one conversation's run into another.
  const live = useRunStore((s) =>
    conversationId ? (s.byConversation[conversationId] ?? EMPTY_LIVE_RUN) : EMPTY_LIVE_RUN,
  );
  const startRun = useRunStore((s) => s.startRun);
  const stopRun = useRunStore((s) => s.stopRun);
  const clearApproval = useRunStore((s) => s.clearApproval);
  const { activeRun, streamText, activity, streamError, toolCalls, approval } = live;
  const { orchestration, decisionRecord } = live;
  const autoSentRef = useRef(false);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const autoScrollRef = useRef(true);

  // Thinking-mode fields for a send: max_depth rides along only in Max mode.
  const thinkingFields = () => ({
    thinking_mode: thinkingMode,
    ...(thinkingMode === "max" ? { max_depth: maxDepth } : {}),
  });

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
        ...thinkingFields(),
      },
      { onSuccess: ({ run }) => startRun(conversationId, run) },
    );
    navigate(location.pathname, { replace: true, state: null });
    // thinkingFields reads the current hook values; the effect runs once per
    // navigation state, so stale-closure risk is nil (autoSentRef guards).
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
  }, [messages.data, activeRun, streamText, streamError, send.isError, toolCalls, approval, orchestration]);

  // Scroll-to-bottom after every paint whenever content changes.
  useLayoutEffect(() => {
    if (!autoScrollRef.current) return;
    const el = scrollContainerRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight - el.clientHeight, behavior: "auto" });
  }, [messages.data, activeRun, streamText, streamError, send.isError, toolCalls, approval, orchestration]);

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
        ...thinkingFields(),
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
        <div className="mx-auto flex w-full max-w-[50.4rem] flex-col gap-4">
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
                <div className={`group flex flex-col ${user ? "items-end" : "items-start"}`}>
                  <article
                    aria-label={`${message.role} message`}
                    className={
                      user
                        ? "max-w-[85%] whitespace-pre-wrap rounded-sm rounded-br-none bg-accent px-4 py-3 text-sm leading-relaxed text-accent-contrast"
                        : "w-full whitespace-pre-wrap py-1 text-sm leading-relaxed text-fg"
                    }
                  >
                    {user
                      ? <PlainMessage content={text} />
                      : <MarkdownMessage content={text} />}
                  </article>
                  {message.created_at && (
                    <time
                      dateTime={message.created_at}
                      className="mt-0.5 px-1 text-[11px] text-fg-subtle opacity-0 transition-opacity duration-150 group-hover:opacity-100"
                    >
                      {formatTimestamp(message.created_at)}
                    </time>
                  )}
                </div>
              </div>
            );
          })}
          {activeRun && <ToolCalls entries={toolCalls} />}
          {activeRun && approval && <ApprovalBanner approval={approval} onDecide={decideApproval} />}
          {activeRun && orchestration && (
            <MaxRunPanel runId={activeRun.id} orchestration={orchestration} activity={activity} />
          )}
          {(send.isPending || activeRun) && !approval && !streamText && !orchestration && (
            <AgentActivity activity={send.isPending ? { message: "Sending…" } : activity} />
          )}
          {streamText && (
            <article
              aria-label="assistant message streaming"
              aria-live="polite"
              className="w-full whitespace-pre-wrap py-1 text-sm leading-relaxed text-fg"
            >
              <MarkdownMessage content={streamText} />
            </article>
          )}
          {(streamError || send.isError) && (
            <p role="alert" className="rounded-xl bg-danger-subtle p-3 text-sm text-danger">
              {streamError ?? send.error?.message ?? "Message failed."}
            </p>
          )}
          {decisionRecord && !activeRun && (
            <p className="flex items-center gap-1.5 px-1 text-xs text-fg-subtle">
              <Brain className="size-3.5 shrink-0" aria-hidden />
              Decision record saved: <span className="font-mono">{decisionRecord}</span>
            </p>
          )}

          {/* Anchor element for scroll-to-bottom targeting */}
          <div aria-hidden style={{ height: 1 }} />
        </div>
      </div>

      <div className="px-4 pb-4 sm:px-6" style={{ paddingBottom: "max(env(safe-area-inset-bottom), 1rem)" }}>
        {enabledModels.isSuccess && enabledModels.data.length === 0 && (
          <p role="status" className="mx-auto mb-2 max-w-[50.4rem] text-xs text-warning">
            No models are enabled. Enable one in Settings → Providers.
          </p>
        )}
        <form
          aria-label="Message composer"
          onSubmit={submit}
          className="mx-auto flex w-full max-w-[50.4rem] flex-col gap-1 rounded-2xl border border-border bg-surface-1 p-2 shadow-md"
        >
          <AttachmentChips
            files={files}
            onRemove={(index) => setFiles((current) => current.filter((_, i) => i !== index))}
          />
          <div className="flex items-end gap-1">
            <PlusMenu
              onAddFiles={(added) => setFiles((current) => [...current, ...added])}
              disabled={Boolean(activeRun)}
            />
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
            <ThinkingModePicker
              value={thinkingMode}
              onChange={setThinkingMode}
              disabled={Boolean(activeRun)}
            />
            {thinkingMode === "max" && (
              <MaxDepthPicker
                value={maxDepth}
                onChange={setMaxDepth}
                disabled={Boolean(activeRun)}
              />
            )}
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
          </div>
        </form>
      </div>
    </div>
  );
}
