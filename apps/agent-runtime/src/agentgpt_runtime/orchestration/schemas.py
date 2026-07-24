"""Structured-output schemas for the max-mode orchestration roles.

Mirrors the JSON shapes in `agentgpt-critical-thinking-tool.md` (repo root):
ProblemFraming, Subproblem, WorkerResult, ReviewResult, CriticResult,
Contradiction, CriticalThinkingResult — plus the run.orchestration wire
shapes (spec §5) and worker state tracking (spec §3.7). Also hosts the
robust JSON extraction / tolerant validation helpers every role parser
uses (spec §3.3: strip fences, outermost object, one re-ask on failure).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# A role call: (system_prompt, user_prompt) -> (parsed JSON object, usage).
CallJson = Callable[[str, str], Awaitable[tuple[dict[str, Any], dict[str, Any]]]]


class State(StrEnum):
    """Deterministic orchestration states (spec §3.2)."""

    FRAME = "FRAME"
    DECOMPOSE = "DECOMPOSE"
    INVESTIGATE = "INVESTIGATE"
    REVIEW = "REVIEW"
    CRITIQUE = "CRITIQUE"
    RESOLVE = "RESOLVE"
    SYNTHESIZE = "SYNTHESIZE"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class WorkerStatus(StrEnum):
    """Worker lifecycle states surfaced in run.orchestration (spec §3.7)."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_FOR_TOOL = "WAITING_FOR_TOOL"
    COMPLETE = "COMPLETE"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    CANCELLED = "CANCELLED"


StepStatus = Literal["pending", "running", "complete", "failed", "skipped"]


class OrchestrationStep(BaseModel):
    """One row of the run.orchestration steps array (spec §5)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    status: StepStatus
    detail: dict[str, Any] | None = None


class BudgetStatus(BaseModel):
    """The budgets object of run.orchestration (spec §5)."""

    model_config = ConfigDict(extra="forbid")

    tokens_used: int = 0
    token_budget: int = 0
    elapsed_s: float = 0.0
    time_budget_s: int = 0


class ProblemFraming(BaseModel):
    objective: str
    success_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    problem_type: str = "general"  # factual|technical|strategic|mathematical|debugging|decision


class Subproblem(BaseModel):
    id: str
    question: str
    purpose: str = ""
    requires_research: bool = False
    dependencies: list[str] = Field(default_factory=list)
    priority: str = "medium"  # high|medium|low
    recommended_tools: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    claim: str
    # classification: verified_fact | supported_inference |
    # unverified_assumption | opinion | unknown
    classification: str = "unknown"
    confidence: float = 0.5
    evidence_ids: list[str] = Field(default_factory=list)


class EvidenceSource(BaseModel):
    """One evidence object (draft "Source and Evidence Tracking")."""

    evidence_id: str
    # type: official_documentation | benchmark | user_file | experiment |
    # model_knowledge | inference | web_page
    type: str = "model_knowledge"
    title: str = ""
    location: str = ""
    source_quality: str = "unknown"  # high|medium|low|unknown
    used_by: list[str] = Field(default_factory=list)
    supports_claims: list[str] = Field(default_factory=list)


class WorkerResult(BaseModel):
    subproblem_id: str
    summary: str = ""
    claims: list[Claim] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    recommended_follow_up: list[str] = Field(default_factory=list)
    sources: list[EvidenceSource] = Field(default_factory=list)


class ClaimReview(BaseModel):
    claim_id: str = ""
    claim: str = ""
    classification: str = "unknown"
    evidence_quality: str = "unknown"  # high|medium|low|unknown
    logical_support: str = "partial"  # full|partial|none
    issues: list[str] = Field(default_factory=list)
    required_follow_up: str = ""


class ReviewResult(BaseModel):
    reviews: list[ClaimReview] = Field(default_factory=list)
    evidence_sufficient: bool = True
    missing_information: list[str] = Field(default_factory=list)
    conflicting_findings: list[str] = Field(default_factory=list)


class Objection(BaseModel):
    objection: str
    severity: str = "medium"  # high|medium|low
    conditions: list[str] = Field(default_factory=list)
    recommended_test: str = ""


class CriticResult(BaseModel):
    target_conclusion: str = ""
    strongest_objections: list[Objection] = Field(default_factory=list)
    overlooked_alternatives: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    # Material questions the critic raised that warrant new investigation
    # (drives the CRITIQUE -> INVESTIGATE loop-back, spec §3.2).
    material_unanswered_questions: list[str] = Field(default_factory=list)
    # critic_kind: general | security | cost | performance | reliability |
    # ux | legal | implementation
    critic_kind: str = "general"


class Contradiction(BaseModel):
    claim_a: str
    claim_b: str
    resolution: str = ""
    remaining_uncertainty: str = ""
    confidence: float = 0.5
    # When true the contradiction needs new evidence (drives RESOLVE ->
    # INVESTIGATE loop-back, spec §3.2).
    requires_new_evidence: bool = False
    follow_up_question: str = ""


class ResolveResult(BaseModel):
    contradictions: list[Contradiction] = Field(default_factory=list)


class Finding(BaseModel):
    statement: str
    classification: str = "unknown"
    confidence: float = 0.5
    evidence_ids: list[str] = Field(default_factory=list)


class ExecutionSummary(BaseModel):
    depth: str = "standard"
    iterations: int = 0
    subproblem_count: int = 0
    worker_count: int = 0
    workers_failed: int = 0
    budget_exhausted: str | None = None
    stopped_early: bool = False


class CriticalThinkingResult(BaseModel):
    """The decision record (draft §7 + spec §3.8)."""

    conclusion: str = ""
    executive_summary: str = ""
    findings: list[Finding] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    counterarguments: list[str] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    sources: list[EvidenceSource] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    execution_summary: ExecutionSummary = Field(default_factory=ExecutionSummary)


# ── Robust JSON extraction / tolerant validation (spec §3.3) ────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class JsonExtractionError(ValueError):
    pass


def extract_json(text: str) -> dict[str, Any]:
    """Extract the outermost JSON object from model output.

    Tolerates surrounding prose and markdown code fences: strip fences, then
    brace-match the outermost ``{...}`` (string-aware) and parse it.
    Raises JsonExtractionError when no object parses.
    """
    candidates: list[str] = []
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1))
    candidates.append(text)

    for candidate in candidates:
        start = candidate.find("{")
        if start < 0:
            continue
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(candidate)):
            ch = candidate[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    fragment = candidate[start : i + 1]
                    try:
                        parsed = json.loads(fragment)
                    except json.JSONDecodeError:
                        break  # try the next candidate
                    if isinstance(parsed, dict):
                        return parsed
                    break
    raise JsonExtractionError(f"no JSON object found in model output ({len(text)} chars)")


def parse_tolerant[ModelT: BaseModel](model_cls: type[ModelT], data: dict[str, Any]) -> ModelT:
    """Validate model output, dropping top-level fields that fail validation.

    Role models ask for JSON but get no grammar guarantees; a single bad
    field (e.g. confidence as a string) must not fail the whole stage — the
    field falls back to its schema default instead.
    """
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        cleaned = dict(data)
        for error in exc.errors():
            loc = error.get("loc", ())
            if loc and loc[0] in cleaned:
                cleaned.pop(loc[0])
        return model_cls.model_validate(cleaned)
