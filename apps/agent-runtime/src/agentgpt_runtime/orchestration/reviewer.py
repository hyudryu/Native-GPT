"""Evidence and assumption review role (spec §3.3, draft §4)."""

from __future__ import annotations

import json
from typing import Any

from agentgpt_runtime.orchestration.prompts import REVIEWER_SYSTEM_PROMPT
from agentgpt_runtime.orchestration.schemas import (
    CallJson,
    ProblemFraming,
    ReviewResult,
    WorkerResult,
    parse_tolerant,
)


def review_prompt(framing: ProblemFraming, results: list[WorkerResult]) -> str:
    worker_dump = [
        {
            "subproblem_id": r.subproblem_id,
            "summary": r.summary,
            "claims": [c.model_dump() for c in r.claims],
            "assumptions": r.assumptions,
            "limitations": r.limitations,
            "sources": [s.model_dump() for s in r.sources],
        }
        for r in results
    ]
    return (
        "PROBLEM FRAMING (JSON):\n"
        + json.dumps(framing.model_dump(), indent=2)
        + "\n\nWORKER RESULTS (JSON):\n"
        + json.dumps(worker_dump, indent=2)
        + "\n\nReview every claim and judge whether the evidence is sufficient "
        "for the objective. Return the JSON review."
    )


async def review_with_model(
    call_json: CallJson,
    framing: ProblemFraming,
    results: list[WorkerResult],
) -> tuple[ReviewResult, dict[str, Any]]:
    data, usage = await call_json(REVIEWER_SYSTEM_PROMPT, review_prompt(framing, results))
    return parse_tolerant(ReviewResult, data), usage
