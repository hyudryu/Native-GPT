"""Max-mode orchestration coordinator (spec §3).

Drives the deterministic state machine FRAME -> ... -> COMPLETE around
ordinary Strands Agent calls, enforces budgets, persists the decision
record, and emits run.orchestration / run.activity / run.text_delta events.

The model-touching work is behind the ``OrchestrationEngine`` protocol so
tests can script canned outputs without a live endpoint; the production
implementation is ``StrandsEngine`` (same_model strategy, spec §7: every
role uses the run's resolved model).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agentgpt_runtime.approvals import ApprovalRegistry
from agentgpt_runtime.orchestration import critic as critic_role
from agentgpt_runtime.orchestration import reviewer as reviewer_role
from agentgpt_runtime.orchestration import synthesizer as synthesizer_role
from agentgpt_runtime.orchestration.budgets import (
    MAX_FOLLOW_UP_SUBPROBLEMS,
    WORKER_OUTPUT_DISK_THRESHOLD_BYTES,
    BudgetTracker,
    preset_for_depth,
)
from agentgpt_runtime.orchestration.prompts import (
    DECOMPOSE_SYSTEM_PROMPT,
    FRAMING_SYSTEM_PROMPT,
    REASK_PROMPT,
    RESOLVE_SYSTEM_PROMPT,
)
from agentgpt_runtime.orchestration.schemas import (
    BudgetStatus,
    CriticalThinkingResult,
    CriticResult,
    EvidenceSource,
    ExecutionSummary,
    Finding,
    JsonExtractionError,
    OrchestrationStep,
    ProblemFraming,
    ResolveResult,
    ReviewResult,
    State,
    Subproblem,
    WorkerResult,
    WorkerStatus,
    extract_json,
    parse_tolerant,
)
from agentgpt_runtime.orchestration.state_machine import StateMachine
from agentgpt_runtime.orchestration.workers import (
    WorkerOutcome,
    merge_usage,
    resolve_worker_tool_ids,
    result_text,
    run_investigations,
)
from agentgpt_runtime.protocol import (
    Envelope,
    RunStartPayload,
    make_envelope,
    make_run_orchestration,
    utc_now_iso,
)
from agentgpt_runtime.tools.registry import repo_root

logger = logging.getLogger(__name__)

Emit = Callable[[Envelope], None]


# ── Event emission ──────────────────────────────────────────────────────────


class RunEmitter:
    """Sequence-numbered event emitter for one max-mode run."""

    def __init__(self, emit: Emit, request_id: str, run_id: str, conversation_id: str) -> None:
        self._emit = emit
        self._request_id = request_id
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.sequence = 0

    def _send(self, envelope: Envelope) -> None:
        self._emit(envelope.model_copy(update={"sequence": self.sequence}))
        self.sequence += 1

    def activity(self, message: str, source: str | None = None) -> None:
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "conversation_id": self.conversation_id,
            "message": message,
        }
        if source:
            payload["source"] = source
        self._send(make_envelope("run.activity", self._request_id, payload))

    def orchestration(
        self, state: State, steps: list[OrchestrationStep], budgets: BudgetStatus
    ) -> None:
        self._send(
            make_run_orchestration(
                self._request_id,
                self.run_id,
                self.conversation_id,
                state=state.value,
                steps=[s.model_dump(exclude_none=True) for s in steps],
                budgets=budgets.model_dump(),
            )
        )

    def text_delta(self, text: str) -> None:
        self._send(
            make_envelope(
                "run.text_delta",
                self._request_id,
                {
                    "run_id": self.run_id,
                    "conversation_id": self.conversation_id,
                    "text": text,
                },
            )
        )

    def approval_needed(self, approval_id: str, tool: str, tool_input: dict, prompt: str) -> None:
        self._send(
            make_envelope(
                "run.approval_needed",
                self._request_id,
                {
                    "run_id": self.run_id,
                    "conversation_id": self.conversation_id,
                    "approval_id": approval_id,
                    "tool": tool,
                    "input": tool_input,
                    "prompt": prompt,
                },
            )
        )

    def approval_resolved(self, approval_id: str, approved: bool) -> None:
        self._send(
            make_envelope(
                "run.approval_resolved",
                self._request_id,
                {
                    "run_id": self.run_id,
                    "conversation_id": self.conversation_id,
                    "approval_id": approval_id,
                    "approved": approved,
                },
            )
        )


# ── Engine protocol ─────────────────────────────────────────────────────────


class OrchestrationEngine(Protocol):
    """The model-touching roles of the orchestration. All usage dicts are the
    normalized chat.usage_from_result shape."""

    async def frame(self, prompt: str) -> tuple[ProblemFraming, dict[str, Any]]: ...

    async def decompose(
        self, framing: ProblemFraming, prompt: str, max_subproblems: int
    ) -> tuple[list[Subproblem], dict[str, Any]]: ...

    async def investigate(
        self,
        framing: ProblemFraming,
        subproblems: list[Subproblem],
        on_worker_status: Callable[[str, WorkerStatus, dict[str, Any] | None], None],
    ) -> list[WorkerOutcome]: ...

    async def review(
        self, framing: ProblemFraming, results: list[WorkerResult]
    ) -> tuple[ReviewResult, dict[str, Any]]: ...

    async def critique(
        self,
        framing: ProblemFraming,
        results: list[WorkerResult],
        review: ReviewResult | None,
        specialist_kinds: list[str],
    ) -> tuple[list[CriticResult], dict[str, Any]]: ...

    async def resolve(
        self,
        framing: ProblemFraming,
        results: list[WorkerResult],
        conflicts: list[str],
    ) -> tuple[ResolveResult, dict[str, Any]]: ...

    async def synthesize(
        self,
        framing: ProblemFraming,
        results: list[WorkerResult],
        review: ReviewResult | None,
        critiques: list[CriticResult],
        contradictions: ResolveResult | None,
        gaps: list[str],
    ) -> tuple[CriticalThinkingResult, dict[str, Any]]: ...


@dataclass
class MaxRunResult:
    cancelled: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    decision_record: str | None = None  # path relative to app-data/
    worker_count: int = 0
    workers_failed: int = 0


# ── Strands engine (same_model strategy) ────────────────────────────────────


class StrandsEngine:
    """Production engine: every role is a plain Strands Agent on the run's
    resolved model (spec §3.3). Structured outputs are parsed with
    extract_json + one re-ask on parse failure."""

    def __init__(
        self,
        payload: RunStartPayload,
        emitter: RunEmitter,
        approvals: ApprovalRegistry,
        budget: BudgetTracker,
        cancel_event: threading.Event,
        synthesize_now_event: threading.Event,
    ) -> None:
        self._payload = payload
        self._emitter = emitter
        self._approvals = approvals
        self._budget = budget
        self._cancel_event = cancel_event
        self._synthesize_now_event = synthesize_now_event
        self._model: Any | None = None
        self._tool_cache: dict[tuple[str, ...], tuple[list[Any], list[str]]] = {}

    def model(self) -> Any:
        if self._model is None:
            from agentgpt_runtime.chat import build_openai_model  # noqa: PLC0415

            # Max roles run the plain request: reasoning params are off/high
            # signals (spec §§1-2); the orchestration itself is the "max"
            # mechanism. One model config shared by every role (same_model).
            self._model = build_openai_model(self._payload)
        return self._model

    async def _call_json(
        self, system_prompt: str, user_prompt: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from strands import Agent  # noqa: PLC0415

        from agentgpt_runtime.chat import usage_from_result  # noqa: PLC0415

        agent = Agent(
            model=self.model(),
            system_prompt=system_prompt,
            callback_handler=lambda **_: None,
        )
        result = await agent.invoke_async(user_prompt)
        usage = usage_from_result(result)
        try:
            return extract_json(result_text(result)), usage
        except JsonExtractionError:
            # One re-ask on parse failure (spec §3.3).
            reasked = await agent.invoke_async(REASK_PROMPT)
            usage = merge_usage(usage, usage_from_result(reasked))
            return extract_json(result_text(reasked)), usage

    async def frame(self, prompt: str) -> tuple[ProblemFraming, dict[str, Any]]:
        data, usage = await self._call_json(
            FRAMING_SYSTEM_PROMPT,
            "USER REQUEST:\n" + prompt + "\n\nFrame this problem as one JSON object.",
        )
        return parse_tolerant(ProblemFraming, data), usage

    async def decompose(
        self, framing: ProblemFraming, prompt: str, max_subproblems: int
    ) -> tuple[list[Subproblem], dict[str, Any]]:
        data, usage = await self._call_json(
            DECOMPOSE_SYSTEM_PROMPT,
            "USER REQUEST:\n"
            + prompt
            + "\n\nPROBLEM FRAMING (JSON):\n"
            + json.dumps(framing.model_dump(), indent=2)
            + f"\n\nDecompose into AT MOST {max_subproblems} subproblems. "
            'Return {"subproblems": [...]} as one JSON object.',
        )
        raw = data.get("subproblems", [])
        if not isinstance(raw, list):
            raw = []
        subproblems: list[Subproblem] = []
        seen: set[str] = set()
        for item in raw[: max_subproblems * 2]:  # parse generously, cap below
            if not isinstance(item, dict) or not item.get("id") or not item.get("question"):
                continue
            sub = parse_tolerant(Subproblem, item)
            if sub.id in seen:
                continue
            seen.add(sub.id)
            subproblems.append(sub)
        return subproblems[:max_subproblems], usage

    def _tools_for(self, subproblem: Subproblem) -> tuple[list[Any], list[str]]:
        """Resolve worker tools: registry ∩ enabled_tools (spec §3.3).

        Cached per tool-id set so repeated batches don't re-import modules.
        """
        ids = tuple(resolve_worker_tool_ids(subproblem, self._payload.enabled_tools))
        if ids in self._tool_cache:
            return self._tool_cache[ids]
        from agentgpt_runtime.chat import approval_allowed_tools  # noqa: PLC0415
        from agentgpt_runtime.tools import load_tool_manifests, load_tools  # noqa: PLC0415

        if not ids:
            resolved: tuple[list[Any], list[str]] = ([], [])
        else:
            tools = load_tools(list(ids))
            manifests = load_tool_manifests(list(ids))
            allowed = approval_allowed_tools(list(ids), tools, manifests)
            resolved = (tools, allowed)
        self._tool_cache[ids] = resolved
        return resolved

    async def investigate(
        self,
        framing: ProblemFraming,
        subproblems: list[Subproblem],
        on_worker_status: Callable[[str, WorkerStatus, dict[str, Any] | None], None],
    ) -> list[WorkerOutcome]:
        from agentgpt_runtime.chat import (  # noqa: PLC0415
            build_approval_intervention,
            usage_from_result,
        )

        def intervention_for(worker_id: str) -> list[Any]:
            hitl_holder: dict[str, Any] = {}

            async def ask_ui(prompt: str, **_: Any) -> str:
                """Bridge a worker's approval prompt to the UI (same wire
                events as the main run path)."""
                approval_id = uuid.uuid4().hex
                hitl = hitl_holder.get("hitl")
                tool_use = getattr(hitl, "pending_tool_use", None) or {}
                raw_input = tool_use.get("input")
                self._emitter.approval_needed(
                    approval_id,
                    str(tool_use.get("name") or "tool"),
                    raw_input if isinstance(raw_input, dict) else {},
                    prompt,
                )
                future = await self._approvals.create(
                    approval_id,
                    self._payload.run_id,
                    prompt,
                    asyncio.get_running_loop(),
                )
                approved = await future
                self._emitter.approval_resolved(approval_id, approved)
                return "y" if approved else "n"

            # Resolve the worker's tools to know which names skip the gate.
            sub = next((s for s in subproblems if s.id == worker_id), None)
            allowed: list[str] = []
            if sub is not None:
                _tools, allowed = self._tools_for(sub)
            if not allowed:
                return []  # no tools or all approval-free -> no gate needed
            hitl = build_approval_intervention(allowed, ask_ui)
            hitl_holder["hitl"] = hitl
            return [hitl]

        return await run_investigations(
            framing=framing,
            subproblems=subproblems,
            model=self.model(),
            tools_for=self._tools_for,
            intervention_for=intervention_for,
            usage_of=usage_from_result,
            on_status=on_worker_status,
            cancel_event=self._cancel_event,
            synthesize_now_event=self._synthesize_now_event,
            max_parallel_workers=self._budget.preset.max_parallel_workers,
        )

    async def review(
        self, framing: ProblemFraming, results: list[WorkerResult]
    ) -> tuple[ReviewResult, dict[str, Any]]:
        return await reviewer_role.review_with_model(self._call_json, framing, results)

    async def critique(
        self,
        framing: ProblemFraming,
        results: list[WorkerResult],
        review: ReviewResult | None,
        specialist_kinds: list[str],
    ) -> tuple[list[CriticResult], dict[str, Any]]:
        return await critic_role.critique_with_model(
            self._call_json, framing, results, review, specialist_kinds
        )

    async def resolve(
        self,
        framing: ProblemFraming,
        results: list[WorkerResult],
        conflicts: list[str],
    ) -> tuple[ResolveResult, dict[str, Any]]:
        claims = [
            {"subproblem_id": r.subproblem_id, "claims": [c.model_dump() for c in r.claims]}
            for r in results
        ]
        data, usage = await self._call_json(
            RESOLVE_SYSTEM_PROMPT,
            "PROBLEM FRAMING (JSON):\n"
            + json.dumps(framing.model_dump(), indent=2)
            + "\n\nWORKER CLAIMS (JSON):\n"
            + json.dumps(claims, indent=2)
            + "\n\nCONFLICTING FINDINGS TO RESOLVE (JSON):\n"
            + json.dumps(conflicts, indent=2)
            + "\n\nResolve each material contradiction (or preserve it "
            "honestly). Return the JSON resolution.",
        )
        return parse_tolerant(ResolveResult, data), usage

    async def synthesize(
        self,
        framing: ProblemFraming,
        results: list[WorkerResult],
        review: ReviewResult | None,
        critiques: list[CriticResult],
        contradictions: ResolveResult | None,
        gaps: list[str],
    ) -> tuple[CriticalThinkingResult, dict[str, Any]]:
        return await synthesizer_role.synthesize_with_model(
            self._call_json, framing, results, review, critiques, contradictions, gaps
        )


# ── Step tracking ───────────────────────────────────────────────────────────

_STATE_STEPS: list[tuple[str, str]] = [
    ("frame", "Framing the problem"),
    ("decompose", "Creating investigations"),
    ("investigate", "Investigating subproblems"),
    ("review", "Reviewing evidence"),
    ("critique", "Adversarial critique"),
    ("resolve", "Resolving contradictions"),
    ("synthesize", "Final synthesis"),
]

_STATE_TO_STEP: dict[State, str] = {
    State.FRAME: "frame",
    State.DECOMPOSE: "decompose",
    State.INVESTIGATE: "investigate",
    State.REVIEW: "review",
    State.CRITIQUE: "critique",
    State.RESOLVE: "resolve",
    State.SYNTHESIZE: "synthesize",
}


class StepTracker:
    """Ordered orchestration steps: the fixed stage rows plus one row per
    subproblem, emitted with every run.orchestration event."""

    def __init__(self) -> None:
        self._steps: dict[str, OrchestrationStep] = {
            step_id: OrchestrationStep(id=step_id, label=label, status="pending")
            for step_id, label in _STATE_STEPS
        }

    def enter(self, state: State) -> None:
        step_id = _STATE_TO_STEP.get(state)
        if step_id is None:
            return
        step = self._steps[step_id]
        if step.status == "pending":
            step.status = "running"

    def complete(self, state: State, status: str = "complete") -> None:
        step_id = _STATE_TO_STEP.get(state)
        if step_id is None:
            return
        self._steps[step_id].status = status  # type: ignore[assignment]

    def add_subproblem(self, subproblem: Subproblem) -> None:
        self._steps[f"sp-{subproblem.id}"] = OrchestrationStep(
            id=f"sp-{subproblem.id}",
            label=f"Investigating: {subproblem.question}",
            status="pending",
        )

    def set_subproblem(
        self, subproblem_id: str, status: str, detail: dict[str, Any] | None = None
    ) -> None:
        step = self._steps.get(f"sp-{subproblem_id}")
        if step is not None:
            step.status = status  # type: ignore[assignment]
            step.detail = detail

    def snapshot(self) -> list[OrchestrationStep]:
        return list(self._steps.values())


# ── Persistence ─────────────────────────────────────────────────────────────


def run_dir_for(run_id: str) -> Path:
    return repo_root() / "app-data" / "runs" / f"ct_{run_id}"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── The coordinator ─────────────────────────────────────────────────────────


async def run_max_thinking(
    *,
    payload: RunStartPayload,
    emit: Emit,
    request_id: str,
    cancel_event: threading.Event,
    synthesize_now_event: threading.Event,
    approvals: Any,
    engine: OrchestrationEngine | None = None,
) -> MaxRunResult:
    """Run the full max-mode orchestration for one chat run.

    Emits the final synthesis as run.text_delta, persists the decision
    record + evidence index under app-data/runs/ct_<run_id>/, and returns
    what chat.py needs for the terminal event.
    """
    emitter = RunEmitter(emit, request_id, payload.run_id, payload.conversation_id)
    preset = preset_for_depth(payload.max_depth)
    budget = BudgetTracker(preset)
    machine = StateMachine()
    steps = StepTracker()
    if engine is None:
        engine = StrandsEngine(
            payload, emitter, approvals, budget, cancel_event, synthesize_now_event
        )

    gaps: list[str] = []
    worker_outcomes: list[WorkerOutcome] = []
    review: ReviewResult | None = None
    critiques: list[CriticResult] = []
    resolved: ResolveResult | None = None
    stopped_early = False

    def emit_orchestration(state: State) -> None:
        emitter.orchestration(state, steps.snapshot(), budget.status())

    def transition(state: State) -> None:
        machine.transition(state)
        steps.enter(state)
        emit_orchestration(state)

    def controls() -> str | None:
        """Check user controls and budgets at a state-transition boundary
        (spec §§3.5, 3.7)."""
        if cancel_event.is_set():
            return "cancel"
        if synthesize_now_event.is_set():
            return "synthesize"
        if budget.exhaustion() is not None:
            return "budget"
        return None

    def on_worker_status(
        subproblem_id: str, status: WorkerStatus, detail: dict[str, Any] | None
    ) -> None:
        step_status = {
            WorkerStatus.QUEUED: "pending",
            WorkerStatus.RUNNING: "running",
            WorkerStatus.WAITING_FOR_TOOL: "running",
            WorkerStatus.COMPLETE: "complete",
            WorkerStatus.FAILED_RETRYABLE: "running",
            WorkerStatus.FAILED_FINAL: "failed",
            WorkerStatus.CANCELLED: "skipped",
        }[status]
        steps.set_subproblem(subproblem_id, step_status, detail)
        emit_orchestration(State.INVESTIGATE)

    def completed_results() -> list[WorkerResult]:
        return [o.result for o in worker_outcomes if o.result is not None]

    def collect_gaps() -> None:
        for outcome in worker_outcomes:
            if outcome.status == WorkerStatus.FAILED_FINAL:
                gaps.append(
                    f"Subproblem '{outcome.subproblem.id}' was not answered "
                    f"(worker failed: {outcome.error or 'unknown error'})."
                )
            elif outcome.status == WorkerStatus.CANCELLED:
                gaps.append(
                    f"Subproblem '{outcome.subproblem.id}' was not investigated "
                    "(stopped before completion)."
                )

    framing: ProblemFraming | None = None
    run_dir = run_dir_for(payload.run_id)

    def persist_worker_outputs(outcomes: list[WorkerOutcome]) -> None:
        for outcome in outcomes:
            if outcome.result is None:
                continue
            blob = outcome.result.model_dump_json(indent=2)
            if len(blob.encode("utf-8")) > WORKER_OUTPUT_DISK_THRESHOLD_BYTES:
                path = run_dir / "workers" / f"{outcome.subproblem.id}.json"
                try:
                    _write_json(path, json.loads(blob))
                    outcome.output_path = (
                        Path("runs")
                        / f"ct_{payload.run_id}"
                        / "workers"
                        / f"{outcome.subproblem.id}.json"
                    ).as_posix()
                except OSError:
                    logger.warning(
                        "could not persist worker output for %s", outcome.subproblem.id
                    )

    async def investigate_batch(batch: list[Subproblem]) -> None:
        if not batch:
            return
        transition(State.INVESTIGATE)
        emitter.activity(f"Investigating {len(batch)} subproblem(s)")
        for subproblem in batch:
            steps.add_subproblem(subproblem)
        outcomes = await engine.investigate(framing, batch, on_worker_status)  # type: ignore[arg-type]
        worker_outcomes.extend(outcomes)
        # Persist large worker outputs; keep summary + path in context (§8).
        persist_worker_outputs(outcomes)
        steps.complete(State.INVESTIGATE)
        emit_orchestration(State.INVESTIGATE)

    try:
        # ── FRAME ──
        transition(State.FRAME)
        emitter.activity("Framing the problem")
        framing, usage = await engine.frame(payload.prompt)
        budget.record_usage(usage)
        steps.complete(State.FRAME)
        emit_orchestration(State.FRAME)

        # ── DECOMPOSE ──
        transition(State.DECOMPOSE)
        emitter.activity("Creating investigations")
        subproblems, usage = await engine.decompose(
            framing, payload.prompt, preset.max_subproblems
        )
        budget.record_usage(usage)
        steps.complete(State.DECOMPOSE, "complete" if subproblems else "failed")
        emit_orchestration(State.DECOMPOSE)
        if not subproblems:
            gaps.append("The problem could not be decomposed into subproblems.")

        control = controls()
        if control is None:
            # ── INVESTIGATE (first pass) ──
            await investigate_batch(subproblems)
            control = controls()

        # ── REVIEW (+ loop-back) ──
        if control is None and completed_results():
            transition(State.REVIEW)
            emitter.activity("Reviewing evidence")
            review, usage = await engine.review(framing, completed_results())  # type: ignore[arg-type]
            budget.record_usage(usage)
            steps.complete(State.REVIEW)
            emit_orchestration(State.REVIEW)
            control = controls()

            if (
                control is None
                and not review.evidence_sufficient
                and review.missing_information
                and budget.can_iterate()
            ):
                budget.iterations += 1
                follow_ups = follow_up_subproblems(
                    review.missing_information, {sp.id for sp in subproblems}
                )
                emitter.activity("Evidence insufficient — investigating follow-ups")
                await investigate_batch(follow_ups)
                control = controls()

        # ── CRITIQUE (+ loop-back) ──
        if control is None and completed_results():
            transition(State.CRITIQUE)
            emitter.activity("Running adversarial critique")
            kinds = specialist_kinds(framing, preset.specialist_critics)  # type: ignore[arg-type]
            critiques, usage = await engine.critique(
                framing, completed_results(), review, kinds  # type: ignore[arg-type]
            )
            budget.record_usage(usage)
            steps.complete(State.CRITIQUE)
            emit_orchestration(State.CRITIQUE)
            control = controls()

            questions = [
                q for critic in critiques for q in critic.material_unanswered_questions
            ]
            if control is None and questions and budget.can_iterate():
                budget.iterations += 1
                follow_ups = follow_up_subproblems(
                    questions, {o.subproblem.id for o in worker_outcomes}
                )
                emitter.activity("Critic raised material questions — investigating")
                await investigate_batch(follow_ups)
                control = controls()

        # ── RESOLVE (+ loop-back) ──
        conflicts = list(review.conflicting_findings) if review else []
        if control is None and conflicts and completed_results():
            transition(State.RESOLVE)
            emitter.activity("Resolving contradictions")
            resolved, usage = await engine.resolve(framing, completed_results(), conflicts)  # type: ignore[arg-type]
            budget.record_usage(usage)
            steps.complete(State.RESOLVE)
            emit_orchestration(State.RESOLVE)
            control = controls()

            needs = [
                c.follow_up_question
                for c in resolved.contradictions
                if c.requires_new_evidence and c.follow_up_question
            ]
            if (
                control is None
                and needs
                and budget.resolve_passes < preset.max_resolve_passes
                and budget.can_iterate()
            ):
                budget.resolve_passes += 1
                budget.iterations += 1
                follow_ups = follow_up_subproblems(
                    needs, {o.subproblem.id for o in worker_outcomes}
                )
                emitter.activity("Contradiction needs new evidence — investigating")
                await investigate_batch(follow_ups)
                control = controls()
        elif not conflicts:
            steps.complete(State.RESOLVE, "skipped")

        if budget.exhaustion() is not None:
            gaps.append(f"Budget exhausted ({budget.exhaustion()}); evidence is partial.")

        # ── SYNTHESIZE (always reachable: spec §3.5) ──
        if control == "cancel":
            for outcome in worker_outcomes:
                if outcome.status in (WorkerStatus.QUEUED, WorkerStatus.RUNNING):
                    outcome.status = WorkerStatus.CANCELLED
            return MaxRunResult(cancelled=True, usage=usage_summary(budget))
        if control == "synthesize":
            stopped_early = True
            gaps.append("Stopped early at the user's request; evidence is partial.")
            emitter.activity("Stopping investigation — synthesizing now")

        collect_gaps()
        transition(State.SYNTHESIZE)
        emitter.activity("Synthesizing the final answer")
        try:
            result, usage = await engine.synthesize(
                framing,  # type: ignore[arg-type]
                completed_results(),
                review,
                critiques,
                resolved,
                gaps,
            )
            budget.record_usage(usage)
        except Exception:  # noqa: BLE001 - never fail the run at the last stage
            logger.exception("synthesizer failed; falling back to deterministic synthesis")
            result = fallback_synthesis(
                framing, completed_results(), critiques, resolved, gaps  # type: ignore[arg-type]
            )
        result.execution_summary = ExecutionSummary(
            depth=payload.max_depth,
            iterations=budget.iterations,
            subproblem_count=len(worker_outcomes),
            worker_count=sum(1 for o in worker_outcomes if o.status == WorkerStatus.COMPLETE),
            workers_failed=sum(
                1 for o in worker_outcomes if o.status == WorkerStatus.FAILED_FINAL
            ),
            budget_exhausted=budget.exhausted_reason,
            stopped_early=stopped_early,
        )
        for gap in gaps:
            if gap not in result.limitations:
                result.limitations.append(gap)

        # To the chat: readable synthesis (conclusion first), not transcripts.
        emitter.text_delta(render_synthesis(result))
        steps.complete(State.SYNTHESIZE)
        transition(State.COMPLETE)
        emit_orchestration(State.COMPLETE)

        record_rel = persist_decision_record(
            run_dir, payload, result, worker_outcomes, budget, stopped_early
        )
        return MaxRunResult(
            cancelled=False,
            usage=usage_summary(budget),
            decision_record=record_rel,
            worker_count=result.execution_summary.worker_count,
            workers_failed=result.execution_summary.workers_failed,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("max-mode run %s failed", payload.run_id)
        # Fatal error with no usable partial results: mark FAILED (spec §3.2)
        # and propagate so chat.py emits run.failed.
        try:
            if machine.state is not None and machine.can_transition(State.FAILED):
                transition(State.FAILED)
        except Exception:  # noqa: BLE001
            pass
        raise


def usage_summary(budget: BudgetTracker) -> dict[str, Any]:
    latency_ms = budget.elapsed_s * 1000
    return {
        "input_tokens": budget.input_tokens,
        "output_tokens": budget.output_tokens,
        "total_tokens": budget.input_tokens + budget.output_tokens,
        "latency_ms": latency_ms,
        "tokens_per_second": budget.output_tokens / (latency_ms / 1000)
        if latency_ms > 0
        else 0.0,
    }


def follow_up_subproblems(questions: list[str], existing_ids: set[str]) -> list[Subproblem]:
    """Deterministic follow-up subproblems from review/critic/resolve output."""
    batch: list[Subproblem] = []
    for question in questions[:MAX_FOLLOW_UP_SUBPROBLEMS]:
        base = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:40] or "follow-up"
        candidate = f"followup-{base}"
        n = 2
        while candidate in existing_ids:
            candidate = f"followup-{base}-{n}"
            n += 1
        existing_ids.add(candidate)
        batch.append(
            Subproblem(
                id=candidate,
                question=question,
                purpose="Follow-up investigation requested during orchestration",
                requires_research=True,
                priority="high",
                recommended_tools=["web-search", "web-fetch"],
            )
        )
    return batch


def specialist_kinds(framing: ProblemFraming, count: int) -> list[str]:
    """Pick specialist critics from the problem type (deep depth, spec §3.3)."""
    if count <= 0:
        return []
    by_type: dict[str, list[str]] = {
        "technical": ["implementation", "performance"],
        "strategic": ["cost", "implementation"],
        "decision": ["cost", "reliability"],
        "debugging": ["reliability", "performance"],
        "factual": ["reliability", "implementation"],
        "mathematical": ["reliability", "implementation"],
    }
    candidates = by_type.get(framing.problem_type.lower(), ["implementation", "cost"])
    return candidates[:count]


def fallback_synthesis(
    framing: ProblemFraming,
    results: list[WorkerResult],
    critiques: list[CriticResult],
    resolved: ResolveResult | None,
    gaps: list[str],
) -> CriticalThinkingResult:
    """Deterministic last-resort synthesis when the synthesis role fails."""
    findings = [
        Finding(
            statement=claim.claim,
            classification=claim.classification,
            confidence=claim.confidence,
            evidence_ids=claim.evidence_ids,
        )
        for worker in results
        for claim in worker.claims
    ]
    summary = " | ".join(w.summary for w in results if w.summary)[:2000]
    return CriticalThinkingResult(
        conclusion=summary or "The orchestration did not produce a conclusion.",
        executive_summary=summary,
        findings=findings,
        assumptions=[a for w in results for a in w.assumptions],
        counterarguments=[o.objection for c in critiques for o in c.strongest_objections],
        contradictions=resolved.contradictions if resolved else [],
        unresolved_questions=list(gaps),
        recommended_actions=[],
        confidence=0.3,
        sources=[s for w in results for s in w.sources],
        limitations=list(gaps) or ["Synthesis generated from raw worker output."],
    )


def render_synthesis(result: CriticalThinkingResult) -> str:
    """Readable chat answer: conclusion first, not a transcript dump (§3.8)."""
    parts: list[str] = [f"## Conclusion\n\n{result.conclusion}"]
    if result.executive_summary and result.executive_summary != result.conclusion:
        parts.append(result.executive_summary)
    parts.append(f"**Confidence:** {result.confidence:.0%}")
    if result.findings:
        lines = "\n".join(f"- {f.statement} _({f.confidence:.0%})_" for f in result.findings)
        parts.append(f"### Key findings\n\n{lines}")
    if result.counterarguments:
        lines = "\n".join(f"- {c}" for c in result.counterarguments)
        parts.append(f"### Strongest counterarguments\n\n{lines}")
    if result.contradictions:
        lines = "\n".join(
            f"- **{c.claim_a}** vs **{c.claim_b}** — {c.resolution}"
            for c in result.contradictions
        )
        parts.append(f"### Contradictions\n\n{lines}")
    if result.assumptions:
        lines = "\n".join(f"- {a}" for a in result.assumptions)
        parts.append(f"### Assumptions\n\n{lines}")
    if result.unresolved_questions:
        lines = "\n".join(f"- {q}" for q in result.unresolved_questions)
        parts.append(f"### Open questions\n\n{lines}")
    if result.recommended_actions:
        lines = "\n".join(f"- {a}" for a in result.recommended_actions)
        parts.append(f"### Recommended next steps\n\n{lines}")
    if result.limitations:
        lines = "\n".join(f"- {lim}" for lim in result.limitations)
        parts.append(f"### Limitations\n\n{lines}")
    if result.sources:
        lines = "\n".join(
            f"- {s.title or s.evidence_id}"
            + (f" ({s.location})" if s.location else "")
            for s in result.sources
        )
        parts.append(f"### Sources\n\n{lines}")
    return "\n\n".join(parts)


def persist_decision_record(
    run_dir: Path,
    payload: RunStartPayload,
    result: CriticalThinkingResult,
    worker_outcomes: list[WorkerOutcome],
    budget: BudgetTracker,
    stopped_early: bool,
) -> str:
    """Write decision.json + evidence.json; return the app-data-relative path.

    Records are kept forever until the user deletes them (spec §3.8).
    """
    record_rel = (Path("runs") / f"ct_{payload.run_id}" / "decision.json").as_posix()
    evidence_rel = (Path("runs") / f"ct_{payload.run_id}" / "evidence.json").as_posix()

    evidence_index: dict[str, EvidenceSource] = {}
    for outcome in worker_outcomes:
        if outcome.result is None:
            continue
        worker_name = f"worker-{outcome.subproblem.id}"
        for source in outcome.result.sources:
            existing = evidence_index.get(source.evidence_id)
            if existing is None:
                source.used_by = sorted({*source.used_by, worker_name})
                evidence_index[source.evidence_id] = source
            else:
                existing.used_by = sorted({*existing.used_by, *source.used_by, worker_name})
                existing.supports_claims = sorted(
                    {*existing.supports_claims, *source.supports_claims}
                )
    for source in result.sources:
        evidence_index.setdefault(source.evidence_id, source)

    decision = {
        "run_id": f"ct_{payload.run_id}",
        "task": payload.prompt,
        "status": "complete",
        "created_at": utc_now_iso(),
        "depth": payload.max_depth,
        "subproblem_count": len(worker_outcomes),
        "worker_count": result.execution_summary.worker_count,
        "iterations": budget.iterations,
        "token_usage": {
            "input": budget.input_tokens,
            "output": budget.output_tokens,
        },
        "stopped_early": stopped_early,
        "artifacts": {
            "decision_record": record_rel,
            "evidence_index": evidence_rel,
        },
        "result": json.loads(result.model_dump_json()),
        "workers": [
            {
                "subproblem_id": o.subproblem.id,
                "status": o.status.value,
                "attempts": o.attempts,
                "error": o.error,
                "output_file": o.output_path,
                # Small results stay inline; >8KB results live in their
                # worker file (spec §8) and are referenced, not duplicated.
                "result": (
                    json.loads(o.result.model_dump_json())
                    if o.result is not None and o.output_path is None
                    else None
                ),
            }
            for o in worker_outcomes
        ],
    }
    evidence = {
        "run_id": f"ct_{payload.run_id}",
        "evidence": [json.loads(s.model_dump_json()) for s in evidence_index.values()],
        "worker_files": {
            o.subproblem.id: o.output_path for o in worker_outcomes if o.output_path
        },
    }
    try:
        _write_json(run_dir / "decision.json", decision)
        _write_json(run_dir / "evidence.json", evidence)
    except OSError:
        logger.exception("could not persist decision record for run %s", payload.run_id)
    return record_rel
