"""Max-mode multi-agent critical-thinking orchestration (spec §3).

`run_max_thinking` is the entry point, called from ChatRuns._stream when
`payload.thinking_mode == "max"`.
"""

from agentgpt_runtime.orchestration.coordinator import (
    MaxRunResult,
    OrchestrationEngine,
    StrandsEngine,
    run_max_thinking,
)
from agentgpt_runtime.orchestration.schemas import (
    CriticalThinkingResult,
    ProblemFraming,
    State,
    Subproblem,
    WorkerResult,
    WorkerStatus,
)

__all__ = [
    "CriticalThinkingResult",
    "MaxRunResult",
    "OrchestrationEngine",
    "ProblemFraming",
    "State",
    "StrandsEngine",
    "Subproblem",
    "WorkerResult",
    "WorkerStatus",
    "run_max_thinking",
]
