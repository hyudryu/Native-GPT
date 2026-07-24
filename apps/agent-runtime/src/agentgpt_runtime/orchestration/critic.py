"""Adversarial critique role (spec §3.3, draft §5).

The general skeptic always runs; `deep` depth adds up to two specialist
critics selected from the framing's problem type (spec §3.3). Critics run
concurrently.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agentgpt_runtime.orchestration.prompts import (
    CRITIC_SYSTEM_PROMPT,
    specialist_critic_prompt,
)
from agentgpt_runtime.orchestration.schemas import (
    CallJson,
    CriticResult,
    ProblemFraming,
    ReviewResult,
    WorkerResult,
    parse_tolerant,
)
from agentgpt_runtime.orchestration.workers import merge_usage


def critique_prompt(
    framing: ProblemFraming,
    results: list[WorkerResult],
    review: ReviewResult | None,
    *,
    focus: str | None = None,
) -> str:
    claims = [
        {
            "subproblem_id": r.subproblem_id,
            "summary": r.summary,
            "claims": [c.model_dump() for c in r.claims],
            "assumptions": r.assumptions,
        }
        for r in results
    ]
    prompt = (
        "PROBLEM FRAMING (JSON):\n"
        + json.dumps(framing.model_dump(), indent=2)
        + "\n\nEMERGING FINDINGS (JSON):\n"
        + json.dumps(claims, indent=2)
    )
    if review is not None:
        prompt += (
            "\n\nEVIDENCE REVIEW (JSON):\n"
            + json.dumps(review.model_dump(), indent=2)
        )
    if focus:
        prompt += f"\n\nAttack the emerging conclusion strictly from the {focus} angle."
    else:
        prompt += "\n\nAttack the emerging conclusion. Return the JSON critique."
    return prompt


async def critique_with_model(
    call_json: CallJson,
    framing: ProblemFraming,
    results: list[WorkerResult],
    review: ReviewResult | None,
    specialist_kinds: list[str],
) -> tuple[list[CriticResult], dict[str, Any]]:
    prompts = [(CRITIC_SYSTEM_PROMPT, critique_prompt(framing, results, review), "general")]
    prompts += [
        (
            specialist_critic_prompt(kind),
            critique_prompt(framing, results, review, focus=kind),
            kind,
        )
        for kind in specialist_kinds
    ]
    outputs = await asyncio.gather(
        *(call_json(system, prompt) for system, prompt, _kind in prompts)
    )
    critics: list[CriticResult] = []
    usage: dict[str, Any] = {}
    for (data, call_usage), (_system, _prompt, kind) in zip(outputs, prompts, strict=True):
        critic = parse_tolerant(CriticResult, data)
        critic.critic_kind = kind
        critics.append(critic)
        usage = merge_usage(usage, call_usage)
    return critics, usage
