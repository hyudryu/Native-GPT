"""Max-mode orchestration: state machine, budgets, failure handling, and a
scripted end-to-end run against a fake engine (no network)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentgpt_runtime.orchestration.budgets import (
    DEPTH_PRESETS,
    BudgetTracker,
    preset_for_depth,
)
from agentgpt_runtime.orchestration.coordinator import (
    follow_up_subproblems,
    render_synthesis,
    run_max_thinking,
    specialist_kinds,
)
from agentgpt_runtime.orchestration.schemas import (
    CriticalThinkingResult,
    CriticResult,
    JsonExtractionError,
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
from agentgpt_runtime.orchestration.state_machine import (
    InvalidTransitionError,
    StateMachine,
)
from agentgpt_runtime.orchestration.workers import (
    WorkerOutcome,
    resolve_worker_tool_ids,
    run_investigations,
)
from agentgpt_runtime.protocol import RunStartPayload

# ── JSON extraction / tolerant parsing ──────────────────────────────────────


def test_extract_json_plain_and_fenced() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('```\n{"a": 3}\n```') == {"a": 3}


def test_extract_json_prose_wrapped_and_nested() -> None:
    text = (
        'Here is the result:\n{"outer": {"inner": [1, 2]}, "s": "a } \\"quote\\""}\n'
        "Hope this helps!"
    )
    parsed = extract_json(text)
    assert parsed["outer"]["inner"] == [1, 2]
    assert parsed["s"] == 'a } "quote"'


def test_extract_json_prefers_fence_over_surrounding_text() -> None:
    text = 'Sure! {"decoy": true}\n```json\n{"real": 1}\n```'
    assert extract_json(text) == {"real": 1}


def test_extract_json_raises_on_garbage() -> None:
    with pytest.raises(JsonExtractionError):
        extract_json("no json here at all")
    with pytest.raises(JsonExtractionError):
        extract_json("{unterminated")


def test_parse_tolerant_drops_bad_fields_to_defaults() -> None:
    data = {
        "objective": "Answer the question",
        "success_criteria": "not-a-list",  # bad type -> dropped
        "unknown_extra": 123,  # unknown -> ignored
    }
    framing = parse_tolerant(ProblemFraming, data)
    assert framing.objective == "Answer the question"
    assert framing.success_criteria == []


# ── State machine ────────────────────────────────────────────────────────────


def test_state_machine_happy_path() -> None:
    machine = StateMachine()
    for state in (
        State.FRAME,
        State.DECOMPOSE,
        State.INVESTIGATE,
        State.REVIEW,
        State.CRITIQUE,
        State.RESOLVE,
        State.SYNTHESIZE,
        State.COMPLETE,
    ):
        machine.transition(state)
    assert machine.state == State.COMPLETE


def test_state_machine_loop_backs_and_synthesize_escape() -> None:
    machine = StateMachine()
    machine.transition(State.FRAME)
    machine.transition(State.DECOMPOSE)
    machine.transition(State.INVESTIGATE)
    machine.transition(State.REVIEW)
    machine.transition(State.INVESTIGATE)  # REVIEW -> INVESTIGATE loop-back
    machine.transition(State.REVIEW)
    machine.transition(State.CRITIQUE)
    machine.transition(State.SYNTHESIZE)  # any -> SYNTHESIZE
    with pytest.raises(InvalidTransitionError):
        machine.transition(State.INVESTIGATE)


def test_state_machine_rejects_invalid_transitions() -> None:
    machine = StateMachine()
    with pytest.raises(InvalidTransitionError):
        machine.transition(State.REVIEW)  # must start at FRAME
    machine.transition(State.FRAME)
    with pytest.raises(InvalidTransitionError):
        machine.transition(State.CRITIQUE)  # FRAME skips stages: not allowed
    # FRAME -> SYNTHESIZE is the sanctioned early-exit escape (spec §3.2).
    machine.transition(State.SYNTHESIZE)
    machine.transition(State.COMPLETE)


def test_state_machine_failed_terminal() -> None:
    machine = StateMachine()
    machine.transition(State.FRAME)
    machine.transition(State.FAILED)
    with pytest.raises(InvalidTransitionError):
        machine.transition(State.SYNTHESIZE)


# ── Budgets / presets ───────────────────────────────────────────────────────


def test_depth_presets_match_spec() -> None:
    quick, standard, deep = (
        DEPTH_PRESETS["quick"],
        DEPTH_PRESETS["standard"],
        DEPTH_PRESETS["deep"],
    )
    assert (quick.max_subproblems, standard.max_subproblems, deep.max_subproblems) == (3, 6, 12)
    assert (quick.max_iterations, standard.max_iterations, deep.max_iterations) == (1, 2, 3)
    assert (quick.token_budget, standard.token_budget, deep.token_budget) == (
        40_000,
        120_000,
        300_000,
    )
    assert (quick.time_budget_s, standard.time_budget_s, deep.time_budget_s) == (180, 600, 1500)
    assert (quick.max_tool_calls, standard.max_tool_calls, deep.max_tool_calls) == (12, 40, 100)
    assert deep.specialist_critics == 2 and standard.specialist_critics == 0
    assert preset_for_depth("bogus") is standard  # default standard


def test_budget_exhaustion_latches() -> None:
    budget = BudgetTracker(DEPTH_PRESETS["quick"])
    assert budget.exhaustion() is None
    budget.record_usage({"input_tokens": 30_000, "output_tokens": 10_000})
    assert budget.exhaustion() == "token_budget"
    assert budget.exhaustion() == "token_budget"  # latched
    assert budget.can_iterate() is False


def test_specialist_critics_selected_by_problem_type() -> None:
    framing = ProblemFraming(objective="o", problem_type="decision")
    assert specialist_kinds(framing, 0) == []
    assert specialist_kinds(framing, 2) == ["cost", "reliability"]
    assert len(specialist_kinds(framing, 2)) <= 2


def test_follow_up_subproblems_unique_ids() -> None:
    batch = follow_up_subproblems(["What is X?", "What is X?"], {"existing"})
    ids = [sp.id for sp in batch]
    assert len(ids) == 2 and len(set(ids)) == 2
    batch2 = follow_up_subproblems(["What is X?"], {*ids, "existing"})
    assert batch2[0].id not in ids


# ── Worker execution (fake agents) ───────────────────────────────────────────


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.message = {"content": [{"text": text}]}
        self.metrics = SimpleNamespace(
            accumulated_usage={"inputTokens": 3, "outputTokens": 2, "totalTokens": 5},
            accumulated_metrics={"latencyMs": 1},
        )


class _FakeAgent:
    """Scripted stand-in for a Strands Agent."""

    def __init__(
        self,
        events: list[dict[str, Any]] | None = None,
        *,
        reask_text: str = "",
        fail_stream: bool = False,
    ) -> None:
        self._events = events or []
        self._reask_text = reask_text
        self._fail_stream = fail_stream
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    async def stream_async(self, prompt: str) -> Any:
        if self._fail_stream:
            raise RuntimeError("model blew up")
        for event in self._events:
            yield event

    async def invoke_async(self, prompt: str) -> Any:
        return _FakeResult(self._reask_text)


def _usage_of(result: Any) -> dict[str, Any]:
    from agentgpt_runtime.chat import usage_from_result

    return usage_from_result(result)


def _worker_result_json(subproblem_id: str = "sp-1") -> str:
    return json.dumps(
        {
            "subproblem_id": subproblem_id,
            "summary": "findings",
            "claims": [
                {
                    "claim": "X is true",
                    "classification": "verified_fact",
                    "confidence": 0.9,
                    "evidence_ids": ["s-1"],
                }
            ],
            "sources": [
                {
                    "evidence_id": "s-1",
                    "type": "official_documentation",
                    "title": "Docs",
                    "location": "https://example.com",
                    "source_quality": "high",
                }
            ],
        }
    )


def _framing() -> ProblemFraming:
    return ProblemFraming(objective="Answer X", problem_type="technical")


async def _run_workers(
    agents: list[Any], subproblems: list[Subproblem] | None = None, **overrides: Any
) -> tuple[list[WorkerOutcome], list[tuple[str, WorkerStatus]]]:
    statuses: list[tuple[str, WorkerStatus]] = []
    queue = list(agents)

    def factory(*args: Any, **kwargs: Any) -> Any:
        return queue.pop(0)

    kwargs: dict[str, Any] = {
        "framing": _framing(),
        "subproblems": subproblems or [Subproblem(id="sp-1", question="q1")],
        "model": None,
        "tools_for": lambda sp: ([], []),
        "intervention_for": lambda wid: [],
        "usage_of": _usage_of,
        "on_status": lambda sp_id, status, detail: statuses.append((sp_id, status)),
        "cancel_event": overrides.pop("cancel_event", threading.Event()),
        "synthesize_now_event": overrides.pop("synthesize_now_event", threading.Event()),
        "agent_factory": factory,
        **overrides,
    }
    outcomes = await run_investigations(**kwargs)
    return outcomes, statuses


async def test_worker_success_with_tool_status() -> None:
    agent = _FakeAgent(
        events=[
            {"current_tool_use": {"toolUseId": "c1", "name": "web_search", "input": {}}},
            {"result": _FakeResult(_worker_result_json())},
        ]
    )
    outcomes, statuses = await _run_workers([agent])
    outcome = outcomes[0]
    assert outcome.status == WorkerStatus.COMPLETE
    assert outcome.result is not None and outcome.result.summary == "findings"
    assert outcome.tools_used == ["web_search"]
    assert (  # WAITING_FOR_TOOL surfaced during the run
        "sp-1",
        WorkerStatus.WAITING_FOR_TOOL,
    ) in statuses


async def test_worker_json_reask_recovers() -> None:
    agent = _FakeAgent(
        events=[{"result": _FakeResult("sorry, I cannot help")}],
        reask_text=_worker_result_json(),
    )
    outcomes, _ = await _run_workers([agent])
    assert outcomes[0].status == WorkerStatus.COMPLETE
    assert outcomes[0].result is not None


async def test_worker_retry_then_complete() -> None:
    failing = _FakeAgent(fail_stream=True)
    good = _FakeAgent(events=[{"result": _FakeResult(_worker_result_json())}])
    outcomes, statuses = await _run_workers([failing, good])
    outcome = outcomes[0]
    assert outcome.status == WorkerStatus.COMPLETE
    assert outcome.attempts == 2
    assert ("sp-1", WorkerStatus.FAILED_RETRYABLE) in statuses


async def test_worker_double_failure_is_final() -> None:
    outcomes, statuses = await _run_workers(
        [_FakeAgent(fail_stream=True), _FakeAgent(fail_stream=True)]
    )
    assert outcomes[0].status == WorkerStatus.FAILED_FINAL
    assert outcomes[0].attempts == 2
    assert ("sp-1", WorkerStatus.FAILED_FINAL) in statuses


async def test_worker_cancelled_before_start() -> None:
    cancelled = threading.Event()
    cancelled.set()
    outcomes, _ = await _run_workers(
        [_FakeAgent(events=[{"result": _FakeResult(_worker_result_json())}])],
        cancel_event=cancelled,
    )
    assert outcomes[0].status == WorkerStatus.CANCELLED


def test_resolve_worker_tool_ids_intersects_and_normalizes() -> None:
    sub = Subproblem(
        id="sp",
        question="q",
        recommended_tools=["web_search", "shell-execute", "critical_thinking", "nope"],
    )
    ids = resolve_worker_tool_ids(sub, ["web-search", "calculate", "shell-execute"])
    assert ids == ["web-search", "shell-execute"]  # orchestration tool excluded


# ── Scripted end-to-end Max run ──────────────────────────────────────────────


def _usage(input_tokens: int = 10, output_tokens: int = 5) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "latency_ms": 1.0,
        "tokens_per_second": float(output_tokens),
    }


class ScriptedEngine:
    """Canned orchestration engine; records calls for assertions."""

    def __init__(self, **overrides: Any) -> None:
        self.calls: list[str] = []
        self.investigate_count = 0
        self.review_result = overrides.get(
            "review", ReviewResult(evidence_sufficient=True, conflicting_findings=[])
        )
        self.critic_result = overrides.get("critiques", [CriticResult(confidence=0.8)])
        self.worker_outcomes = overrides.get("worker_outcomes")
        self.synthesize_result = overrides.get(
            "synthesize",
            CriticalThinkingResult(conclusion="The answer is Tauri.", confidence=0.85),
        )
        self.frame_usage = overrides.get("frame_usage", _usage())
        self.on_investigate = overrides.get("on_investigate")
        self.gaps_seen: list[str] = []

    async def frame(self, prompt: str) -> tuple[ProblemFraming, dict[str, Any]]:
        self.calls.append("frame")
        return _framing(), self.frame_usage

    async def decompose(
        self, framing: ProblemFraming, prompt: str, max_subproblems: int
    ) -> tuple[list[Subproblem], dict[str, Any]]:
        self.calls.append("decompose")
        return [
            Subproblem(id="sp-a", question="Question A"),
            Subproblem(id="sp-b", question="Question B"),
        ][:max_subproblems], _usage()

    async def investigate(
        self,
        framing: ProblemFraming,
        subproblems: list[Subproblem],
        on_worker_status: Any,
    ) -> list[WorkerOutcome]:
        self.calls.append("investigate")
        self.investigate_count += 1
        if self.on_investigate:
            self.on_investigate()
        outcomes = self.worker_outcomes
        if outcomes is None:
            outcomes = [
                WorkerOutcome(
                    subproblem=sp,
                    status=WorkerStatus.COMPLETE,
                    result=WorkerResult(
                        subproblem_id=sp.id,
                        summary=f"findings for {sp.id}",
                        claims=[],
                    ),
                    attempts=1,
                )
                for sp in subproblems
            ]
        for outcome in outcomes:
            on_worker_status(outcome.subproblem.id, outcome.status, None)
        return outcomes

    async def review(
        self, framing: ProblemFraming, results: list[WorkerResult]
    ) -> tuple[ReviewResult, dict[str, Any]]:
        self.calls.append("review")
        return self.review_result, _usage()

    async def critique(
        self,
        framing: ProblemFraming,
        results: list[WorkerResult],
        review: ReviewResult | None,
        specialist_kinds: list[str],
    ) -> tuple[list[CriticResult], dict[str, Any]]:
        self.calls.append("critique")
        return self.critic_result, _usage()

    async def resolve(
        self,
        framing: ProblemFraming,
        results: list[WorkerResult],
        conflicts: list[str],
    ) -> tuple[ResolveResult, dict[str, Any]]:
        self.calls.append("resolve")
        return ResolveResult(), _usage()

    async def synthesize(
        self,
        framing: ProblemFraming,
        results: list[WorkerResult],
        review: ReviewResult | None,
        critiques: list[CriticResult],
        contradictions: ResolveResult | None,
        gaps: list[str],
    ) -> tuple[CriticalThinkingResult, dict[str, Any]]:
        self.calls.append("synthesize")
        self.gaps_seen = list(gaps)
        return self.synthesize_result, _usage()


@pytest.fixture()
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "tools").mkdir()
    (tmp_path / "Cargo.toml").write_text("", encoding="utf-8")
    (tmp_path / "app-data").mkdir()
    monkeypatch.setenv("AGENTGPT_REPO_ROOT", str(tmp_path))
    return tmp_path


def _payload(run_id: str = "run-max-1", depth: str = "standard") -> RunStartPayload:
    return RunStartPayload.model_validate(
        {
            "run_id": run_id,
            "conversation_id": "conv-1",
            "message_id": "msg-1",
            "prompt": "Electron or Tauri?",
            "thinking_mode": "max",
            "max_depth": depth,
            "model": {"base_url": "http://model.local", "model_id": "m"},
        }
    )


async def _run_max(
    engine: ScriptedEngine,
    fake_repo: Path,
    depth: str = "standard",
    cancel: threading.Event | None = None,
    synthesize_now: threading.Event | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    result = await run_max_thinking(
        payload=_payload(depth=depth),
        emit=lambda env: events.append(env.model_dump()),
        request_id="req-1",
        cancel_event=cancel or threading.Event(),
        synthesize_now_event=synthesize_now or threading.Event(),
        approvals=None,
        engine=engine,
    )
    return result, events


def _orchestration_states(events: list[dict[str, Any]]) -> list[str]:
    return [e["payload"]["state"] for e in events if e["type"] == "run.orchestration"]


async def test_max_run_happy_path(fake_repo: Path) -> None:
    engine = ScriptedEngine()
    result, events = await _run_max(engine, fake_repo)

    assert not result.cancelled
    states = _orchestration_states(events)
    assert states[0] == "FRAME" and states[-1] == "COMPLETE"
    for expected in ("DECOMPOSE", "INVESTIGATE", "REVIEW", "CRITIQUE", "SYNTHESIZE"):
        assert expected in states
    assert "RESOLVE" not in states  # no conflicting findings -> skipped
    assert engine.calls == [
        "frame",
        "decompose",
        "investigate",
        "review",
        "critique",
        "synthesize",
    ]

    # Synthesis text reached the chat as run.text_delta, conclusion first.
    deltas = [e["payload"]["text"] for e in events if e["type"] == "run.text_delta"]
    assert deltas and "The answer is Tauri." in deltas[0]
    assert deltas[0].startswith("## Conclusion")

    # Decision record + evidence index on disk; path returned for run.completed.
    assert result.decision_record == "runs/ct_run-max-1/decision.json"
    record_path = fake_repo / "app-data" / "runs" / "ct_run-max-1" / "decision.json"
    evidence_path = fake_repo / "app-data" / "runs" / "ct_run-max-1" / "evidence.json"
    assert record_path.is_file() and evidence_path.is_file()
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["task"] == "Electron or Tauri?"
    assert record["result"]["conclusion"] == "The answer is Tauri."
    assert record["artifacts"]["decision_record"] == "runs/ct_run-max-1/decision.json"
    assert len(record["workers"]) == 2

    # run.orchestration carries steps and budgets.
    orch = [e for e in events if e["type"] == "run.orchestration"][-1]
    step_ids = {s["id"] for s in orch["payload"]["steps"]}
    assert {"frame", "decompose", "investigate", "synthesize"} <= step_ids
    assert {"sp-sp-a", "sp-sp-b"} <= step_ids
    assert orch["payload"]["budgets"]["token_budget"] == 120_000


async def test_iteration_cap_bounds_loop_backs(fake_repo: Path) -> None:
    """quick depth: max_iterations=1 -> at most one INVESTIGATE re-entry even
    though both the reviewer and the critic ask for more."""
    engine = ScriptedEngine(
        review=ReviewResult(
            evidence_sufficient=False,
            missing_information=["memory numbers"],
            conflicting_findings=[],
        ),
        critiques=[
            CriticResult(confidence=0.7, material_unanswered_questions=["what about cost?"])
        ],
    )
    _result, events = await _run_max(engine, fake_repo, depth="quick")
    states = _orchestration_states(events)
    assert engine.investigate_count == 2  # initial + one capped loop-back
    assert states.count("INVESTIGATE") >= 2


async def test_budget_exhaustion_synthesizes_partial(fake_repo: Path) -> None:
    engine = ScriptedEngine(frame_usage=_usage(input_tokens=400_000, output_tokens=0))
    result, events = await _run_max(engine, fake_repo)
    assert not result.cancelled
    # Frame + decompose happened, then budget tripped -> straight to synthesis.
    assert "investigate" not in engine.calls
    assert engine.calls[-1] == "synthesize"
    assert any("Budget exhausted" in gap for gap in engine.gaps_seen)
    record = json.loads(
        (fake_repo / "app-data" / result.decision_record).read_text(encoding="utf-8")
    )
    assert record["result"]["execution_summary"]["budget_exhausted"] == "token_budget"


async def test_worker_failure_disclosed_in_synthesis(fake_repo: Path) -> None:
    failed_sp = Subproblem(id="sp-b", question="Question B")
    engine = ScriptedEngine(
        worker_outcomes=[
            WorkerOutcome(
                subproblem=Subproblem(id="sp-a", question="A"),
                status=WorkerStatus.COMPLETE,
                result=WorkerResult(subproblem_id="sp-a", summary="ok"),
                attempts=1,
            ),
            WorkerOutcome(
                subproblem=failed_sp,
                status=WorkerStatus.FAILED_FINAL,
                error="model blew up",
                attempts=2,
            ),
        ]
    )
    result, events = await _run_max(engine, fake_repo)
    assert not result.cancelled
    assert any("sp-b" in gap and "not answered" in gap for gap in engine.gaps_seen)
    # The failed subproblem's step shows failed.
    orch = [e for e in events if e["type"] == "run.orchestration"][-1]
    steps = {s["id"]: s for s in orch["payload"]["steps"]}
    assert steps["sp-sp-b"]["status"] == "failed"
    record = json.loads(
        (fake_repo / "app-data" / result.decision_record).read_text(encoding="utf-8")
    )
    assert record["result"]["execution_summary"]["workers_failed"] == 1
    assert any("sp-b" in lim for lim in record["result"]["limitations"])


async def test_cancel_returns_cancelled_without_synthesis(fake_repo: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    engine = ScriptedEngine()
    result, _events = await _run_max(engine, fake_repo, cancel=cancel)
    assert result.cancelled
    assert "investigate" not in engine.calls and "synthesize" not in engine.calls


async def test_synthesize_now_stops_early_but_synthesizes(fake_repo: Path) -> None:
    synthesize_now = threading.Event()
    engine = ScriptedEngine(on_investigate=synthesize_now.set)
    result, events = await _run_max(engine, fake_repo, synthesize_now=synthesize_now)
    assert not result.cancelled
    assert engine.calls[-1] == "synthesize"
    # Review/critique were skipped after the early-stop flag.
    assert "review" not in engine.calls and "critique" not in engine.calls
    record = json.loads(
        (fake_repo / "app-data" / result.decision_record).read_text(encoding="utf-8")
    )
    assert record["result"]["execution_summary"]["stopped_early"] is True
    assert any("Stopped early" in gap for gap in engine.gaps_seen)


async def test_large_worker_output_persisted_to_disk(fake_repo: Path) -> None:
    big_summary = "x" * 9000
    engine = ScriptedEngine(
        worker_outcomes=[
            WorkerOutcome(
                subproblem=Subproblem(id="sp-big", question="big"),
                status=WorkerStatus.COMPLETE,
                result=WorkerResult(subproblem_id="sp-big", summary=big_summary),
                attempts=1,
            )
        ]
    )
    result, _events = await _run_max(engine, fake_repo)
    worker_file = (
        fake_repo / "app-data" / "runs" / "ct_run-max-1" / "workers" / "sp-big.json"
    )
    assert worker_file.is_file()
    evidence = json.loads(
        (
            fake_repo / "app-data" / "runs" / "ct_run-max-1" / "evidence.json"
        ).read_text(encoding="utf-8")
    )
    assert evidence["worker_files"]["sp-big"].endswith("sp-big.json")
    # The decision record references the file instead of duplicating the blob.
    record = json.loads((fake_repo / "app-data" / result.decision_record).read_text())
    assert record["workers"][0]["output_file"].endswith("sp-big.json")
    assert record["workers"][0]["result"] is None


def test_render_synthesis_structure() -> None:
    result = CriticalThinkingResult(
        conclusion="Do X.",
        executive_summary="Because Y.",
        confidence=0.85,
        unresolved_questions=["How much?"],
        limitations=["partial evidence"],
    )
    text = render_synthesis(result)
    assert text.startswith("## Conclusion")
    assert "**Confidence:** 85%" in text
    assert "### Open questions" in text
    assert "### Limitations" in text
