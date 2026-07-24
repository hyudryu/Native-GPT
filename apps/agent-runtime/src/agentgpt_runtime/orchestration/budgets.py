"""Depth presets and budget enforcement for max mode (spec §§3.4-3.5).

Hard limits: subproblems, workers, iterations, tokens, time, tool calls,
worker output size, concurrency. When any limit trips, the coordinator jumps
to SYNTHESIZE with whatever exists — `on_budget_exhausted:
"synthesize_partial"` is the only policy.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from agentgpt_runtime.orchestration.schemas import BudgetStatus

# Worker outputs larger than this are persisted to disk; the coordinator
# keeps only the summary + path in context (spec §8).
WORKER_OUTPUT_DISK_THRESHOLD_BYTES = 8 * 1024

# Follow-up investigation batch cap (review/critic/resolve loop-backs).
MAX_FOLLOW_UP_SUBPROBLEMS = 3


@dataclass(frozen=True)
class DepthPreset:
    max_subproblems: int
    max_iterations: int  # INVESTIGATE re-entries
    max_resolve_passes: int  # contradiction-resolution loop-backs
    specialist_critics: int  # extra specialist critics beyond the general one
    token_budget: int
    time_budget_s: int
    max_tool_calls: int
    max_parallel_workers: int = 4


# Spec §3.4.
DEPTH_PRESETS: dict[str, DepthPreset] = {
    "quick": DepthPreset(
        max_subproblems=3,
        max_iterations=1,
        max_resolve_passes=1,
        specialist_critics=0,
        token_budget=40_000,
        time_budget_s=180,
        max_tool_calls=12,
    ),
    "standard": DepthPreset(
        max_subproblems=6,
        max_iterations=2,
        max_resolve_passes=1,
        specialist_critics=0,
        token_budget=120_000,
        time_budget_s=600,
        max_tool_calls=40,
    ),
    "deep": DepthPreset(
        max_subproblems=12,
        max_iterations=3,
        max_resolve_passes=2,
        specialist_critics=2,
        token_budget=300_000,
        time_budget_s=1500,
        max_tool_calls=100,
    ),
}


def preset_for_depth(depth: str) -> DepthPreset:
    return DEPTH_PRESETS.get(depth, DEPTH_PRESETS["standard"])


@dataclass
class BudgetTracker:
    """Sums token/time/tool-call usage across every role call of a run."""

    preset: DepthPreset
    started_at: float = field(default_factory=time.monotonic)
    tokens_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    iterations: int = 0
    resolve_passes: int = 0
    exhausted_reason: str | None = None

    def record_usage(self, usage: dict[str, Any]) -> None:
        """Accumulate a normalized usage dict (chat.usage_from_result shape)."""
        self.input_tokens += int(usage.get("input_tokens", 0) or 0)
        self.output_tokens += int(usage.get("output_tokens", 0) or 0)
        self.tokens_used = self.input_tokens + self.output_tokens

    def record_tool_calls(self, count: int = 1) -> None:
        self.tool_calls += count

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def exhaustion(self) -> str | None:
        """The first hard limit that has tripped, or None."""
        if self.exhausted_reason is not None:
            return self.exhausted_reason
        if self.tokens_used >= self.preset.token_budget:
            self.exhausted_reason = "token_budget"
        elif self.elapsed_s >= self.preset.time_budget_s:
            self.exhausted_reason = "time_budget"
        elif self.tool_calls >= self.preset.max_tool_calls:
            self.exhausted_reason = "tool_call_budget"
        return self.exhausted_reason

    def can_iterate(self) -> bool:
        return self.iterations < self.preset.max_iterations and self.exhaustion() is None

    def status(self) -> BudgetStatus:
        return BudgetStatus(
            tokens_used=self.tokens_used,
            token_budget=self.preset.token_budget,
            elapsed_s=round(self.elapsed_s, 1),
            time_budget_s=self.preset.time_budget_s,
        )
