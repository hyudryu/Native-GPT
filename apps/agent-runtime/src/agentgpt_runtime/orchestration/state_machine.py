"""Deterministic orchestration state machine (spec §3.2).

FRAME -> DECOMPOSE -> INVESTIGATE -> REVIEW -> CRITIQUE -> RESOLVE ->
SYNTHESIZE -> COMPLETE, with loop-backs to INVESTIGATE (capped by the
coordinator's iteration counter) and an any-state -> SYNTHESIZE escape on
stop/budget/iteration cap. Models never control transitions directly: the
coordinator decides, this class validates.
"""

from __future__ import annotations

from agentgpt_runtime.orchestration.schemas import State

# Allowed transitions. INVESTIGATE appears as a target of REVIEW / CRITIQUE /
# RESOLVE loop-backs; SYNTHESIZE is reachable from any working state.
_TRANSITIONS: dict[State, frozenset[State]] = {
    State.FRAME: frozenset({State.DECOMPOSE, State.SYNTHESIZE, State.FAILED}),
    State.DECOMPOSE: frozenset({State.INVESTIGATE, State.SYNTHESIZE, State.FAILED}),
    State.INVESTIGATE: frozenset(
        {State.REVIEW, State.CRITIQUE, State.SYNTHESIZE, State.FAILED}
    ),
    State.REVIEW: frozenset(
        {State.INVESTIGATE, State.CRITIQUE, State.SYNTHESIZE, State.FAILED}
    ),
    State.CRITIQUE: frozenset(
        {State.INVESTIGATE, State.RESOLVE, State.SYNTHESIZE, State.FAILED}
    ),
    State.RESOLVE: frozenset(
        {State.INVESTIGATE, State.SYNTHESIZE, State.FAILED}
    ),
    State.SYNTHESIZE: frozenset({State.COMPLETE, State.FAILED}),
    State.COMPLETE: frozenset(),
    State.FAILED: frozenset(),
}


class InvalidTransitionError(ValueError):
    pass


class StateMachine:
    """Validates and records orchestration state transitions."""

    def __init__(self) -> None:
        self._state: State | None = None
        self.history: list[State] = []

    @property
    def state(self) -> State | None:
        return self._state

    def can_transition(self, target: State) -> bool:
        if self._state is None:
            return target == State.FRAME
        return target in _TRANSITIONS[self._state]

    def transition(self, target: State) -> State:
        if not self.can_transition(target):
            raise InvalidTransitionError(f"{self._state} -> {target} is not allowed")
        self._state = target
        self.history.append(target)
        return target
