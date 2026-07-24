# Thinking Modes (Off / High / Max) — Design Spec

**Date:** 2026-07-23
**Status:** Approved (implementing, single phase)
**Supersedes:** the informal `ThinkingLevel = "low" | "medium" | "high"` UI stub in `apps/ui/src/pages/ChatPage.tsx:350-434`
**Source material:** `agentgpt-critical-thinking-tool.md` (repo root, working draft)

## Summary

AgentGPT gets exactly three user-selectable thinking modes per message:

| Mode | Behavior |
|---|---|
| **Off** | The runtime sends a provider-appropriate "do not think" request parameter so the model answers directly with minimal latency. No reasoning tokens are requested. |
| **High** | The runtime requests the model's own high-reasoning behavior (e.g. `reasoning_effort: "high"`) and streams a normal single-agent answer. |
| **Max** | The runtime runs a structured multi-agent orchestration (frame → decompose → investigate → review → critique → resolve → synthesize) inside the Python sidecar before returning a final synthesized answer with a decision record. |

The mode travels end-to-end with each run: UI picker → host REST → Rust protocol → sidecar `RunStartPayload` → request construction (`build_openai_model`) or the Max orchestrator.

## Goals

1. **Three modes only.** Replace the dead-end low/medium/high picker; no other granularity is exposed to users.
2. **Off and High are real request-level signals**, not prompt hacks. Where the endpoint supports it, reasoning is disabled/raised via API parameters.
3. **Max is a real orchestration**, not "think step by step" prompt text. Its value comes from independent investigation, evidence review, adversarial critique, and contradiction resolution (per the source draft's central principle: *more reasoning is not automatically better reasoning*).
4. **Bounded execution.** The Max runtime is a deterministic state machine with hard budgets; the model cannot create unbounded loops.
5. **Reuse the existing run pipeline** (streaming events, cancellation, persistence) the same way `factory_mode` does.
6. **Single-phase delivery.** Everything in this spec ships at once.

## Non-goals

- Auto-invocation of Max mode by the main agent (the draft's "Invocation Rules" § is a later consideration; mode is explicitly user-selected).
- `mixed_models` / `strongest_available` model strategies — every role runs on the conversation's resolved model (`same_model`). The payload extension for multi-model is specified but not implemented.
- Exposing Max as a callable Strands *tool* the model can invoke mid-run. It is a **run mode** chosen by the user, parallel to `factory_mode`. This also makes the draft's recursion problem structurally impossible (see Recursion Prevention).
- Batched tool approvals (workers reuse the existing per-call HITL flow).

## Decisions (confirmed with owner 2026-07-23)

| Decision | Choice |
|---|---|
| Mode identity | `thinking_mode: "off" \| "high" \| "max"` on `run.start`; default `"high"` |
| Off/High mechanism | Strands `OpenAIModel` params passthrough, driven by per-endpoint thinking profiles |
| Unsupported-param handling | Learn from the first 400-class parameter error: retry once without thinking params, cache the endpoint+model as unsupported (persisted), never send thinking params to it again |
| Max-mode home | New orchestration module in the sidecar, branched from `ChatRuns._stream` beside `factory_mode` |
| Orchestration engine | Deterministic Python state machine around ordinary Strands `Agent` calls (ADR-0002: Strands is the mandated agent loop) |
| Progress reporting | New structured `run.orchestration` event type (keeps plain `run.activity` for one-liners) |
| Worker parallelism | `asyncio` tasks inside the sidecar process; no new processes, no new sidecars |
| Depth presets | `quick` / `standard` / `deep`; UI shows the selector only in Max mode; default `standard` |
| Model strategy | `same_model` — the resolved `payload.model` is used for every role |
| Worker tool approvals | Workers inherit the existing per-call HITL approval intervention unchanged |
| Recursion prevention | Structural: workers are internal agents with no access to any Max-invocation path |
| Budget exhaustion | Always synthesize best-available partial result; never fail silently |
| Decision-record retention | Keep forever until the user deletes them; no automatic cap |
| Stub migration | `low → off`, `medium → high`, `high → high` |

---

## 1. Mode: Off

### 1.1 Request-level behavior

The only place model parameters are built today is `build_openai_model()` in `apps/agent-runtime/src/agentgpt_runtime/chat.py:47-76`. Strands' `OpenAIModel` accepts a `params` dict passed through to the chat-completions request (verify against the pinned `strands-agents` version at implementation time; if the constructor key differs, use whatever passthrough it exposes). In Off mode the runtime merges a **thinking-off profile** into those params.

Because AgentGPT talks to arbitrary OpenAI-compatible endpoints, there is no single universal "off" switch. The runtime uses a lookup table of known profiles, with a per-endpoint override:

```python
THINKING_OFF_PROFILES: dict[str, dict] = {
    # OpenAI reasoning models (gpt-5 family, o-series)
    "openai":    {"reasoning_effort": "none"},      # retry with "minimal" on 400
    # Anthropic-compatible endpoints
    "anthropic": {"thinking": {"type": "disabled"}},
    # Qwen / DashScope-style
    "qwen":      {"extra_body": {"enable_thinking": False}},
    # DeepSeek-style
    "deepseek":  {"extra_body": {"thinking": {"type": "disabled"}}},
}
```

Resolution order for a run with `thinking_mode == "off"`:

1. **Endpoint override** — if the provider/endpoint record in the host has `thinking_off_params` set (JSON, user-editable in Settings → Providers), use exactly that.
2. **Unsupported cache** — if this endpoint+model previously 400'd on thinking params, send nothing.
3. **Known profile** — match by `base_url` host and/or `model_id` prefix against `THINKING_OFF_PROFILES`.
4. **Best-effort default** — `{"reasoning_effort": "none"}`; on a 400-class parameter error, retry once with `{"reasoning_effort": "minimal"}`, then record the endpoint as unsupported and continue without thinking params.
5. **No silent prompt injection.** If the endpoint rejects all parameter attempts, the run proceeds without them and the UI shows a one-time notice: *"This endpoint does not support disabling thinking."* The runtime never appends "don't think" text to the user's prompt.

The unsupported cache is persisted at `app-data/thinking-params-cache.json`, keyed by `base_url + model_id`.

Non-reasoning models (the common case) receive no extra params once the profile table doesn't match, so Off costs nothing.

### 1.2 Streaming behavior

Off mode streams normally (`run.text_delta`). Providers that separate reasoning content simply emit none. No special UI handling.

---

## 2. Mode: High

High is the default. It mirrors Off with a **thinking-high profile** table:

```python
THINKING_HIGH_PROFILES: dict[str, dict] = {
    "openai":    {"reasoning_effort": "high"},
    "anthropic": {"thinking": {"type": "enabled", "budget_tokens": 16384}},
    "qwen":      {"extra_body": {"enable_thinking": True}},
    "deepseek":  {"extra_body": {"thinking": {"type": "enabled"}}},
}
```

The same resolution order, endpoint override (`thinking_high_params`), retry ladder, and persisted unsupported-cache apply: if an endpoint 400s on the high profile, retry once with no thinking params, cache it, and never send them again. For non-reasoning models this converges to the plain request after at most one learning round-trip.

---

## 3. Mode: Max

This section grounds the workflow in `agentgpt-critical-thinking-tool.md` in the actual codebase. The draft's workflow, agent roles, claim classification, contradiction handling, and evidence tracking are adopted as written; this section specifies how they map onto the sidecar.

### 3.1 Where it lives

New package: `apps/agent-runtime/src/agentgpt_runtime/orchestration/`

```
orchestration/
├── __init__.py
├── state_machine.py     # FRAME → DECOMPOSE → ... → COMPLETE transitions
├── coordinator.py       # framing, decomposition, assignment, budgets, execution record
├── workers.py           # investigator agent construction + parallel execution
├── reviewer.py          # evidence & assumption review
├── critic.py            # adversarial critique
├── synthesizer.py       # final synthesis + decision record
├── schemas.py           # ProblemFraming, Subproblem, WorkerResult, ReviewResult,
│                        # CriticResult, Contradiction, CriticalThinkingResult
├── budgets.py           # ExecutionPolicy enforcement
└── prompts.py           # role system prompts
```

`ChatRuns._stream` (`chat.py:302`) gains a third branch:

```python
if payload.thinking_mode == "max":
    result = await run_max_thinking(payload, emit, cancel_event, ...)
    # synthesizer's final text is streamed as run.text_delta like any answer
elif payload.factory_mode:
    ...
else:
    ...  # existing single-agent path (off + high)
```

### 3.2 State machine

Deterministic; models never control transitions directly. Adopted from the draft:

```text
FRAME → DECOMPOSE → INVESTIGATE → REVIEW → CRITIQUE → RESOLVE → SYNTHESIZE → COMPLETE

REVIEW   → INVESTIGATE   when evidence is insufficient (counts as one iteration)
CRITIQUE → INVESTIGATE   when the critic raises a material unanswered question
RESOLVE  → INVESTIGATE   when a contradiction requires new evidence
Any      → SYNTHESIZE    on "stop and synthesize", budget exhaustion, or iteration cap
Any      → FAILED        fatal error with no usable partial results
```

The coordinator owns the iteration counter; `max_iterations` caps the number of INVESTIGATE re-entries.

### 3.3 Agent roles and worker execution

Roles per the draft: Coordinator, Investigator workers, Evidence reviewer, Adversarial critic, Synthesis agent. Implementation notes:

- Each role is a plain Strands `Agent` built with the same `OpenAIModel` config as the parent run (`same_model`), a role-specific system prompt from `prompts.py`, and structured-output instructions matching `schemas.py`. Structured outputs are parsed as JSON with a robust extraction/repair step (strip code fences, locate the outermost JSON object, one re-ask on parse failure).
- **Investigators run as `asyncio` tasks**, capped by `max_parallel_workers` (default 4) via a semaphore. No new processes — one run is already one thread + one event loop (`chat.py:184-189`, `274-276`).
- **Isolation:** each worker receives only the problem framing, its assigned subproblem, relevant constraints, its tool subset, and the output schema. Workers never see each other's results. The coordinator keeps only summaries + references in its context.
- **Worker tools:** each subproblem's `recommended_tools` is resolved through the existing tool registry (`tools/registry.py:53-74`), intersected with the run's `enabled_tools`. Workers inherit the existing HITL approval intervention, so tools whose manifests set `requires_approval` (e.g. `shell-execute`) still prompt the user per call.
- **Specialist critics** (security, cost, performance, etc.) are selected by the coordinator based on the framing's problem type; `deep` depth may run several, `quick` runs one general skeptic.

### 3.4 Depth presets

| | quick | standard | deep |
|---|---|---|---|
| max_subproblems | 3 | 6 | 12 |
| max_iterations (investigation passes) | 1 | 2 | 3 |
| critics | 1 general | 1 general | 1 general + up to 2 specialists |
| contradiction-resolution passes | 0–1 | 1 | 2 |
| token_budget | 40k | 120k | 300k |
| time_budget_seconds | 180 | 600 | 1500 |
| max_tool_calls | 12 | 40 | 100 |

`standard` is the default. Depth is part of `run.start` (see §4) and changeable in the composer only when Max is selected.

### 3.5 Budgets and partial synthesis

`budgets.py` enforces the full limit set: subproblems, workers, iterations, tokens, time, tool calls, worker output size, concurrency. Token usage is tracked per role via Strands' usage metrics and summed by the coordinator.

When any hard limit trips, the state machine jumps to SYNTHESIZE with whatever exists; the synthesizer must disclose gaps (`unresolved_questions`, `limitations`). `on_budget_exhausted: "synthesize_partial"` is the only policy.

### 3.6 Recursion prevention

Because Max is a **run mode** rather than a tool, the draft's recursion vector does not exist: workers are internal agents constructed by the coordinator, there is no `critical_thinking` tool to call, and workers cannot issue `run.start` requests. Additionally:

- Workers' tool lists are explicitly filtered to exclude any future orchestration tool.
- If Max is ever exposed as a tool, the runtime must set `max_thinking_active=true` in the run context and reject nested invocation at the boundary.

### 3.7 Failure handling

Per the draft: a failed worker does not fail the run. Worker states `QUEUED / RUNNING / WAITING_FOR_TOOL / COMPLETE / FAILED_RETRYABLE / FAILED_FINAL / CANCELLED` are tracked by the coordinator and surfaced in `run.orchestration` events. Retry policy: one retry with a corrected prompt for `FAILED_RETRYABLE`, then mark the gap and continue; the synthesis discloses missing subproblems.

Cancellation reuses the existing `threading.Event` + `agent.cancel()` mechanism (`chat.py:196-208`): the coordinator checks the cancel event at every state transition and cancels in-flight worker agents.

"Stop and synthesize now" is a new request type `run.synthesize_now` (UI → host → sidecar) that sets a flag the coordinator checks at every state transition, jumping to SYNTHESIZE with current partial results.

### 3.8 Result and decision record

The synthesizer produces `CriticalThinkingResult` (draft §7: conclusion, executive_summary, findings, assumptions, counterarguments, contradictions, unresolved_questions, recommended_actions, confidence, sources, execution_summary).

- **To the chat:** a readable synthesis — conclusion first, then key findings, counterarguments, confidence, open questions. Not a dump of worker transcripts.
- **To disk:** the full decision record + evidence index at `app-data/runs/ct_<run_id>/{decision.json, evidence.json}`, matching the draft's execution-record schema. Records are kept forever until the user deletes them. The chat message stores the record path so "Open evidence" / "Export decision record" can reopen it.

---

## 4. End-to-end plumbing

`thinking_mode` (and `max_depth`) must be added at every hop. The existing `factory_mode` boolean is the exact precedent for each change.

| Hop | File | Change |
|---|---|---|
| UI send | `apps/ui/src/lib/dataApi.ts:298-300` | Add `thinking_mode`, `max_depth?` to the send-message body |
| UI composer | `apps/ui/src/pages/ChatPage.tsx:849-860` | Include mode in the send mutation |
| Host REST | `crates/server/src/chat.rs:16-29` | Add fields to `SendMessage`; default `high` when absent |
| Host → sidecar | `crates/server/src/chat.rs:198-220` | Populate `RunStart` fields |
| Rust protocol | `crates/server/src/protocol.rs:122-140` | Add `thinking_mode`, `max_depth` to `RunStart` |
| Wire schema | `packages/protocol-types/schemas/messages.json:99` | Add fields to `run.start` (enum: `off`/`high`/`max`) |
| Python protocol | `apps/agent-runtime/src/agentgpt_runtime/protocol.py:167-181` | Add fields to `RunStartPayload` (note: `extra="forbid"` — unknown fields currently hard-fail, so host and sidecar must ship together) |
| Request build | `apps/agent-runtime/src/agentgpt_runtime/chat.py:47-76` | Merge thinking profile params for `off`/`high`, with learn-on-400 retry |
| Orchestration branch | `apps/agent-runtime/src/agentgpt_runtime/chat.py:302` | Route `max` into `orchestration/` |
| Synthesize-now | `protocol.py` request types, `server.py` dispatch, Rust relay, UI control | New `run.synthesize_now` request carrying `run_id` |

The mode is **per message**, not per conversation. The picker selection is remembered in `localStorage` as the default for the next send.

## 5. Protocol events for Max progress

New event type `run.orchestration`, relayed transparently by the supervisor like all run events:

```json
{
  "type": "run.orchestration",
  "payload": {
    "run_id": "...",
    "state": "INVESTIGATE",
    "steps": [
      {"id": "frame", "label": "Framed the problem", "status": "complete"},
      {"id": "sp-memory-comparison", "label": "Investigating memory comparison",
       "status": "running", "detail": {"worker": "worker-2", "tools_used": ["web-search"]}}
    ],
    "budgets": {"tokens_used": 48200, "token_budget": 120000, "elapsed_s": 140, "time_budget_s": 600}
  }
}
```

`step.status`: `pending | running | complete | failed | skipped`. Emitted on every state transition and worker status change. Registered in:

- `packages/protocol-types/schemas/messages.json` (schema)
- `apps/agent-runtime/src/agentgpt_runtime/protocol.py` (constant + emit helper)
- `apps/ui/src/lib/runStore.ts:276-388` (`dispatchRunEvent` case, extending the `THINKING` activity placeholder at `runStore.ts:39`)

Plain `run.activity` strings ("Reviewing evidence…") are still emitted alongside for the compact activity line.

## 6. UI changes

1. **Replace `ThinkingLevelPicker`** (`ChatPage.tsx:350-434`) with a three-option mode picker: **Off / High / Max**, with one-line descriptions ("Fastest, direct answer" / "Model's own deep reasoning" / "Multi-agent critical analysis"). Storage key migrates from `agentgpt.thinkingLevel`; old values map: `low → off`, `medium → high`, `high → high`.
2. **Depth selector** appears next to the picker only when Max is selected (Quick / Standard / Deep; default Standard).
3. **Max run panel** in the existing activity area: checklist of states and subproblems per §5 (✓/●/○), expandable subproblem rows (status, tools, sources, finding, confidence), budget meter, and controls: **Stop**, **Stop and synthesize now** (sends `run.synthesize_now`). (Pause, Increase depth, Add a question, Retry failed investigation, Export — later.)
4. **Off-mode notice** when the endpoint rejects thinking-off params (§1.1 step 5).

## 7. Model strategy

- **Now — `same_model` only.** Every role uses `payload.model`. Works with any configured provider, including fully local models.
- **Later — `mixed_models`.** Extend `run.start` with an optional `models: {coordinator?, worker?, reviewer?, critic?, synthesis?}` map of fully-resolved `{base_url, model_id, api_key}` objects (the host already resolves and brokers credentials per model, `crates/server/src/chat.rs:197`). Settings UI lets users assign roles to providers. Until this ships, the field must be absent — `extra="forbid"` on `RunStartPayload` will reject it otherwise.

## 8. Memory and long-session behavior

- Worker outputs above a threshold (default 8 KB) are written to `app-data/runs/ct_<run_id>/workers/<subproblem_id>.json`; the coordinator keeps summary + path only.
- Completed worker agents are dropped (no lingering Strands message history); the source corpus is never copied into every worker prompt — workers fetch what they need via their tools.
- Users can delete the full execution trace from the run panel while retaining the synthesized chat answer.
- Decision records are kept forever until the user deletes them; no automatic cap.

## 9. Testing

- **Unit (sidecar):** state-machine transitions and iteration caps; thinking profile resolution, retry ladder, and unsupported-cache persistence; budget exhaustion → partial synthesis; worker failure → gap disclosure; JSON extraction/repair of worker/review/critic outputs.
- **Contract:** `run.start` with each mode round-trips UI → host → sidecar (the `extra="forbid"` guard makes drift a loud failure).
- **Integration:** scripted Max run against a mock OpenAI-compatible server returning canned worker/review/critic completions; assert event sequence, decision record on disk, and final synthesized text.
- **UI:** mode persisted and sent; orchestration panel renders each `step.status`; cancel and synthesize-now propagate.

## 10. Delivery plan

Single phase, implemented in this order (dependency order, not rollout gates):

1. Wire schema (`messages.json`) + Python protocol + Rust protocol — the contract.
2. Sidecar thinking profiles (Off/High) with learn-on-400 cache.
3. Sidecar `orchestration/` package (state machine, roles, budgets, persistence) + `run.synthesize_now`.
4. Rust host plumbing (`SendMessage`, `RunStart`, synthesize-now relay).
5. UI: picker replacement, depth selector, orchestration panel, notices.
6. Tests per §9; `cargo check`, sidecar unit tests, `pnpm build` all green.
