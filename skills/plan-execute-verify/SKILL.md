# Plan → Execute → Verify

The operating loop for any task with more than one step. It exists because the
two classic failure modes of agent work are (a) drifting through a long task
with no record of what is done, and (b) declaring victory without checking.
This loop makes both structurally impossible: the plan is persisted state, and
completion is gated on deterministic validation.

The loop has four phases — **plan, execute, verify, revise** — and a strict
completion gate at the end.

## Phase 1: Plan

1. Open a goal contract with `goal_create_contract`. State the goal in one
   sentence and express success criteria as validator specs, e.g.
   `{"type": "file_exists", "path": "reports/weekly.md"}`,
   `{"type": "artifact_exists", "mime_type": "text/markdown"}`,
   `{"type": "plan_steps_completed", "plan_id": "..."}`. If you cannot name a
   checkable criterion, you do not yet understand the task — clarify first.
2. Create the plan with `todo_create` (goal, success criteria, constraints).
3. Decompose with `todo_add_step`: one step per verifiable unit of work, with
   `dependencies` recorded so execution order is computed, not remembered.
   Each step should say what it produces, not just what it does.

Rules of thumb: 3–10 steps is healthy; one giant step means you skipped the
thinking, thirty tiny steps means you are planning keystrokes. Keep steps at
the granularity where each has its own done-check.

## Phase 2: Execute

1. Ask `todo_next_ready_steps` what is unblocked; work only those steps.
2. Mark a step `in_progress` with `todo_update_step` before starting it —
   the plan must always reflect reality, because it is what you (and the
   goal-supervisor) consult after any interruption.
3. Do the step with the right tool for its nature: fs tools for files, web
   tools for fresh information, knowledge/memory for stored facts, dev-tools
   for builds and tests. Produce the step's declared output.
4. Record the outcome honestly: `completed` with a result summary when the
   done-check passes, `failed` with the reason when it does not, `blocked`
   when an external input is missing. Never mark a step completed because its
   tool call returned without error — mark it completed because its output
   exists and was checked.
5. Feed progress into the contract with `goal_record_progress` so the
   supervisor's view matches the plan.

## Phase 3: Verify

Verification is deterministic, not impressionistic:

1. Run `goal_evaluate` against the contract. Each validator checks observable
   state — files on disk, artifacts in the store, plan step statuses, JSON
   schema conformance — not your narration.
2. For deliverables, prefer existence-plus-content checks: an artifact that
   exists but is empty or truncated is a failure the validator should catch
   (size, mime type, schema), not a technicality to wave through.
3. Treat every failed criterion as a defect in the work, not in the check.

## Phase 4: Revise on failure

When a step fails or a criterion does not pass:

1. Diagnose before acting: wrong assumption, missing input, tool limitation,
   or genuine task infeasibility. One sentence, recorded in the step's failure
   payload.
2. Revise the plan with `todo_revise`: fix the step, split it, add a
   prerequisite, or — when the task itself is infeasible — mark the contract
   blocked with `goal_mark_blocked` and say exactly what is needed.
3. Re-execute only the affected part of the plan; then re-verify. Bounded
   retries (the step's `maximum_attempts`) are for transient failures —
   repeated identical failures mean the plan is wrong, not unlucky.
4. If recovery needs a fresh run or a different strategy, request it with
   `goal_request_recovery` instead of looping silently.

## The completion gate

Mark the contract complete with `goal_mark_complete` **only** when every
success criterion passes evaluation. The gate refuses completion when any
criterion is failing or was never evaluated — that refusal is the feature.
If you find yourself wanting to complete despite a failing criterion, either
the work is not done, or the criterion was written wrong (fix the contract,
re-verify, then complete).

## Anti-patterns this loop prevents

- Planning in your head — state dies on interruption; the plan table does not.
- Executing out of dependency order — ask `todo_next_ready_steps`, always.
- "It returned ok, ship it" — ok means the call succeeded, not that the
  outcome is correct.
- Retry storms — revise the plan after a diagnosed failure; retries are for
  transients.
- Declaring done from memory — completion is a validator result, not a
  feeling.
