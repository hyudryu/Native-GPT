import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router";
import { ArrowLeft, Loader2 } from "lucide-react";
import AppPage, { field, panel, primaryButton, secondaryButton } from "../features/apps/AppPage";
import { socket } from "../lib/ws";
import {
  useCreateTool,
  useEnabledModelsForFactory,
  useToolSource,
  useUpdateToolFiles,
  type ToolManifest,
} from "../lib/appsApi";
import { createConversation, sendMessage } from "../lib/dataApi";

const RISK_OPTIONS = ["read", "write", "execute", "external_side_effect"] as const;
const NETWORK_OPTIONS = ["none", "outbound"] as const;

const EMPTY_MANIFEST: ToolManifest = {
  id: "",
  name: "",
  description: "",
  version: "1.0.0",
  trusted: false,
  default_enabled: false,
  risk: "read",
  requires_approval: false,
  network: "none",
  timeout_seconds: 30,
};

interface SaveToolInput {
  id: string;
  name: string;
  description: string;
  version: string;
  risk: string;
  requires_approval: boolean;
  network: string;
  timeout_seconds: number;
  trusted: boolean;
  tool_code: string;
}

export default function ToolFactoryPage() {
  const { toolId } = useParams<{ toolId?: string }>();
  const isEdit = Boolean(toolId);
  const navigate = useNavigate();

  const source = useToolSource(toolId);
  const createTool = useCreateTool();
  const updateTool = useUpdateToolFiles();
  const models = useEnabledModelsForFactory();

  const [manifest, setManifest] = useState<ToolManifest>(EMPTY_MANIFEST);
  const [toolCode, setToolCode] = useState<string>("");
  const [requirement, setRequirement] = useState<string>("");
  const [transcript, setTranscript] = useState<string>("");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const activeRun = useRef<{ requestId: string; runId: string } | null>(null);

  // Edit mode: load existing tool into the form/code panel.
  useEffect(() => {
    if (source.data) {
      setManifest(source.data.manifest);
      setToolCode(source.data.tool_code);
    }
  }, [source.data]);

  async function handleGenerate() {
    if (!requirement.trim() || models.data?.length === 0) return;
    const model = models.data?.[0];
    if (!model) return;
    setError(null);
    setTranscript("");
    setStreaming(true);
    try {
      // A transient tool-manager conversation scoped to this session.
      const conv = await createConversation({
        title: `Tool Manager: ${requirement.slice(0, 40)}`,
        endpoint_id: model.provider_id,
        model_id: model.model_id,
      });
      const res = await sendMessage(conv.id, {
        content: requirement,
        endpoint_id: model.provider_id,
        model_id: model.model_id,
        factory_mode: true,
        factory_revision: isEdit ? toolId : undefined,
      });
      activeRun.current = { requestId: res.run.request_id, runId: res.run.id };
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start generation");
      setStreaming(false);
    }
  }

  // Listen for the agent's save_tool call + streamed text.
  useEffect(() => {
    const offDelta = socket.on("run.text_delta", (envelope) => {
      const run = activeRun.current;
      const p = envelope.payload as Record<string, unknown>;
      if (!run || (envelope.request_id !== run.requestId && p.run_id !== run.runId)) return;
      if (typeof p.text === "string") setTranscript((cur) => cur + p.text);
    });
    const offToolCall = socket.on("run.tool_call", (envelope) => {
      const run = activeRun.current;
      const p = envelope.payload as Record<string, unknown>;
      if (!run || (envelope.request_id !== run.requestId && p.run_id !== run.runId)) return;
      if (p.tool !== "save_tool" || typeof p.call_id !== "string") return;
      const input = p.input as Partial<SaveToolInput> | undefined;
      if (!input) return;
      setManifest((cur) => ({
        ...cur,
        id: input.id ?? cur.id,
        name: input.name ?? cur.name,
        description: input.description ?? cur.description,
        version: input.version ?? cur.version,
        risk: (input.risk as ToolManifest["risk"]) ?? cur.risk,
        requires_approval: input.requires_approval ?? cur.requires_approval,
        network: (input.network as ToolManifest["network"]) ?? cur.network,
        timeout_seconds: input.timeout_seconds ?? cur.timeout_seconds,
        trusted: input.trusted ?? cur.trusted,
      }));
      if (typeof input.tool_code === "string") setToolCode(input.tool_code);
    });
    const offCompleted = socket.on("run.completed", (envelope) => {
      const run = activeRun.current;
      const p = envelope.payload as Record<string, unknown>;
      if (!run || (envelope.request_id !== run.requestId && p.run_id !== run.runId)) return;
      setStreaming(false);
    });
    const offFailed = socket.on("run.failed", (envelope) => {
      const run = activeRun.current;
      const p = envelope.payload as Record<string, unknown>;
      if (!run || (envelope.request_id !== run.requestId && p.run_id !== run.runId)) return;
      setStreaming(false);
      const err = p.error as { message?: string } | undefined;
      setError(err?.message ?? "Generation failed");
    });
    return () => {
      offDelta();
      offToolCall();
      offCompleted();
      offFailed();
    };
  }, []);

  function handleSave() {
    setError(null);
    if (isEdit && toolId) {
      updateTool.mutate(
        { id: toolId, manifest, tool_code: toolCode },
        { onSuccess: () => navigate("/apps/tools"), onError: (e) => setError(e.message) },
      );
    } else {
      createTool.mutate(
        { id: manifest.id, manifest, tool_code: toolCode },
        { onSuccess: () => navigate("/apps/tools"), onError: (e) => setError(e.message) },
      );
    }
  }

  const saving = createTool.isPending || updateTool.isPending;
  const dirty = toolCode.trim().length > 0 && manifest.id.trim().length > 0;

  return (
    <AppPage
      title={isEdit ? `Edit tool: ${manifest.name || toolId}` : "Tool Manager"}
      description={isEdit ? "Revise this tool with the agent or edit the code directly." : "Describe a tool and let the agent build it. Review, then save."}
      icon={ArrowLeft}
      actions={
        <button type="button" className={secondaryButton} onClick={() => navigate("/apps/tools")}>
          <ArrowLeft className="size-4" aria-hidden /> Back to Tools
        </button>
      }
    >
      <div className="grid gap-4 lg:grid-cols-2">
        <section className={panel}>
          <h2 className="text-lg font-medium">{isEdit ? "Revision request" : "Requirement"}</h2>
          <textarea
            className={`${field} mt-3 min-h-24`}
            placeholder={isEdit ? "e.g. add an option to format as 24-hour" : "e.g. a tool that displays the current time"}
            value={requirement}
            onChange={(e) => setRequirement(e.target.value)}
          />
          <div className="mt-3 flex items-center gap-2">
            <button type="button" className={primaryButton} disabled={streaming || !requirement.trim()} onClick={handleGenerate}>
              {streaming ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
              {streaming ? "Generating…" : isEdit ? "Revise with agent" : "Generate with agent"}
            </button>
          </div>
          {transcript && (
            <pre className="mt-4 max-h-60 overflow-auto whitespace-pre-wrap rounded-xl bg-surface-2 p-3 text-xs text-fg-muted">{transcript}</pre>
          )}
        </section>

        <section className={panel}>
          <h2 className="text-lg font-medium">Manifest</h2>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <label className="text-xs text-fg-muted">
              ID (folder)
              <input className={`${field} mt-1 font-mono`} value={manifest.id} disabled={isEdit} onChange={(e) => setManifest({ ...manifest, id: e.target.value })} />
            </label>
            <label className="text-xs text-fg-muted">
              Name
              <input className={`${field} mt-1`} value={manifest.name} onChange={(e) => setManifest({ ...manifest, name: e.target.value })} />
            </label>
            <label className="text-xs text-fg-muted sm:col-span-2">
              Description
              <input className={`${field} mt-1`} value={manifest.description} onChange={(e) => setManifest({ ...manifest, description: e.target.value })} />
            </label>
            <label className="text-xs text-fg-muted">
              Version
              <input className={`${field} mt-1`} value={manifest.version} onChange={(e) => setManifest({ ...manifest, version: e.target.value })} />
            </label>
            <label className="text-xs text-fg-muted">
              Risk
              <select className={`${field} mt-1`} value={manifest.risk ?? "read"} onChange={(e) => setManifest({ ...manifest, risk: e.target.value as ToolManifest["risk"] })}>
                {RISK_OPTIONS.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </label>
            <label className="text-xs text-fg-muted">
              Network
              <select className={`${field} mt-1`} value={manifest.network ?? "none"} onChange={(e) => setManifest({ ...manifest, network: e.target.value as ToolManifest["network"] })}>
                {NETWORK_OPTIONS.map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
            <label className="text-xs text-fg-muted">
              Timeout (s)
              <input type="number" className={`${field} mt-1`} value={manifest.timeout_seconds ?? 30} onChange={(e) => setManifest({ ...manifest, timeout_seconds: Number(e.target.value) })} />
            </label>
            <label className="text-xs text-fg-muted inline-flex items-center gap-2 sm:col-span-2">
              <input type="checkbox" className="size-4 accent-[var(--color-accent)]" checked={manifest.requires_approval ?? false} onChange={(e) => setManifest({ ...manifest, requires_approval: e.target.checked })} />
              Requires approval (prompt before each call)
            </label>
            <label className="text-xs text-fg-muted inline-flex items-center gap-2 sm:col-span-2">
              <input type="checkbox" className="size-4 accent-[var(--color-accent)]" checked={manifest.trusted} onChange={(e) => setManifest({ ...manifest, trusted: e.target.checked })} />
              Trusted (can be enabled and reach the agent)
            </label>
          </div>
        </section>
      </div>

      <section className={`${panel} mt-4`}>
        <h2 className="text-lg font-medium">tool.py</h2>
        <textarea
          className={`${field} mt-3 min-h-80 font-mono text-xs`}
          spellCheck={false}
          value={toolCode}
          onChange={(e) => setToolCode(e.target.value)}
          placeholder={"from strands import tool\n\n@tool\ndef my_tool() -> str:\n    \"\"\"...\"\"\"\n    ...\n\nTOOL = my_tool"}
        />
      </section>

      {error && <p role="alert" className="mt-4 rounded-xl bg-danger-subtle p-3 text-sm text-danger">{error}</p>}

      <div className="mt-4 flex items-center gap-2">
        <button type="button" className={primaryButton} disabled={!dirty || saving} onClick={handleSave}>
          {saving ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
          {isEdit ? "Save changes" : "Create tool"}
        </button>
        <button type="button" className={secondaryButton} onClick={() => navigate("/apps/tools")}>Cancel</button>
      </div>
    </AppPage>
  );
}
