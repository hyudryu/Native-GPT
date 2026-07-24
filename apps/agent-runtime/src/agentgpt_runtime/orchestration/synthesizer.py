"""Final synthesis role (spec §3.8, draft §7).

Produces the CriticalThinkingResult decision record. The chat-readable
rendering is deterministic (coordinator.render_synthesis) so the synthesizer
only emits the structured record — conclusion first, gaps disclosed.
"""

from __future__ import annotations

import json
from typing import Any

from agentgpt_runtime.orchestration.prompts import SYNTHESIZER_SYSTEM_PROMPT
from agentgpt_runtime.orchestration.schemas import (
    CallJson,
    CriticalThinkingResult,
    CriticResult,
    ProblemFraming,
    ResolveResult,
    ReviewResult,
    WorkerResult,
    parse_tolerant,
)


def synthesize_prompt(
    framing: ProblemFraming,
    results: list[WorkerResult],
    review: ReviewResult | None,
    critiques: list[CriticResult],
    resolved: ResolveResult | None,
    gaps: list[str],
) -> str:
    sections = [
        "PROBLEM FRAMING (JSON):\n" + json.dumps(framing.model_dump(), indent=2),
        "WORKER RESULTS (JSON):\n"
        + json.dumps([r.model_dump() for r in results], indent=2),
    ]
    if review is not None:
        sections.append(
            "EVIDENCE REVIEW (JSON):\n" + json.dumps(review.model_dump(), indent=2)
        )
    if critiques:
        sections.append(
            "ADVERSARIAL CRITIQUES (JSON):\n"
            + json.dumps([c.model_dump() for c in critiques], indent=2)
        )
    if resolved is not None:
        sections.append(
            "CONTRADICTION RESOLUTIONS (JSON):\n"
            + json.dumps(resolved.model_dump(), indent=2)
        )
    if gaps:
        sections.append(
            "DISCLOSED GAPS (these MUST appear in unresolved_questions/limitations):\n"
            + json.dumps(gaps, indent=2)
        )
    sections.append("Synthesize the final decision record as one JSON object.")
    return "\n\n".join(sections)


async def synthesize_with_model(
    call_json: CallJson,
    framing: ProblemFraming,
    results: list[WorkerResult],
    review: ReviewResult | None,
    critiques: list[CriticResult],
    resolved: ResolveResult | None,
    gaps: list[str],
) -> tuple[CriticalThinkingResult, dict[str, Any]]:
    data, usage = await call_json(
        SYNTHESIZER_SYSTEM_PROMPT,
        synthesize_prompt(framing, results, review, critiques, resolved, gaps),
    )
    return parse_tolerant(CriticalThinkingResult, data), usage
