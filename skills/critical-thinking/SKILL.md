# Critical Thinking

A reasoning discipline for any task that is not a single mechanical step. Load
this skill when the request is ambiguous, multi-part, high-stakes, or easy to
get wrong by pattern-matching. It slows the front of the work down so the rest
goes fast and lands correctly.

## 1. Identify the real objective

The literal request is a starting point, not the objective. Ask: what outcome
does the user actually need? "Summarize this PDF" usually means "help me decide
X"; "write a script" usually means "make this chore stop happening". Restate
the objective in one sentence before acting. If the gap between the literal
request and the likely objective is large, confirm with the user instead of
guessing — one clarifying question beats a polished answer to the wrong
problem.

## 2. Define success criteria up front

Write down what "done" looks like *before* doing the work, as observable,
checkable statements — not vibes. "The report exists" is weak; "a Markdown
report at reports/weekly.md covering sections A/B/C, verified to exist on
disk" is strong. These criteria later become goal-supervisor validators
(`artifact_exists`, `file_exists`, `json_schema_valid`, ...), so phrase them
as things that can be checked deterministically.

## 3. Surface hidden constraints

Every task carries constraints nobody stated: deadlines, budgets, file formats,
allowed roots, rate limits, "don't touch production", tone, audience, existing
conventions in the repo. List the ones you can infer; flag the ones you cannot
verify as open questions rather than silently assuming them away.

## 4. Separate facts from assumptions

Keep two explicit lists while you work:

- **Facts** — things you observed directly (read the file, ran the command,
  got the API response) or the user stated.
- **Assumptions** — everything else, including "this library is installed",
  "the schema looks like X", "the user wants English".

Before any irreversible or expensive step, check which side of the line that
step depends on. Verify an assumption (cheap read, dry run, quick test) or
label the output as contingent on it.

## 5. Identify unknowns

Name what you do not know. An unknown you have named is a research task; an
unknown you have not named is a future failure. Track unresolved questions and
either resolve them or carry them explicitly into the final answer as stated
uncertainty.

## 6. Break work into verifiable steps

Decompose until each step has a crisp done-check. Step state lives in the
todo-list tools — create the plan with `todo_create` (goal + success criteria),
add steps with `todo_add_step` (dependencies included), and keep statuses
honest with `todo_update_step` as you work. Do not hold a multi-step plan in
your head; the plan is the shared source of truth and survives interruption.

## 7. Map dependencies

Order steps by what each one needs, not by the order you thought of them. A
step that produces an artifact another step consumes must come first; steps
with no interdependency can be batched. Record dependencies in
`todo_add_step` so `todo_next_ready_steps` can tell you what is unblocked —
never start a step whose inputs do not exist yet.

## 8. Select tools deliberately

For each step, pick the tool whose strengths match the step: deterministic
checks through code/tests, factual lookup through knowledge/memory, fresh
information through web tools, file edits through the fs tools. If no tool fits
well, say so and do the step manually with explicit caveats instead of forcing
a poor fit.

## 9. Prefer primary sources

Documentation over blog posts, the actual file over someone's description of
it, the API response over a cached summary, first-party data over aggregation.
When sources conflict, prefer the one closest to the ground truth and say why.

## 10. Compare conflicting evidence

When two sources disagree, do not average them and do not silently pick the
convenient one. State the conflict, weigh provenance (primary beats secondary,
recent beats stale, measured beats asserted), and either resolve it with a
decisive check or present both with their provenance.

## 11. Test assumptions cheaply and early

Order work so the riskiest assumption is tested first. A two-minute probe
("does the API actually return this field?") before an hour of building beats
discovering the opposite afterwards. Treat surprising results as signal to
re-plan, not noise to push through.

## 12. Track unresolved questions

Keep a running list of things you could not verify. Resolve what you can as
you go; whatever remains at the end goes into the final answer, clearly
labeled — never launder an open question into a confident statement.

## 13. Revise after failure

A failed step is information. Diagnose *why* (wrong assumption? missing input?
tool limitation?), then revise the plan with `todo_revise` — change the step,
split it, or add a prerequisite — rather than retrying the identical action
and hoping. Bounded retries are for transient errors only.

## 14. Verify before completion

Never declare done on the strength of "the tool returned ok". Check the
success criteria from step 2 against observable reality: the file exists and
contains what you think, the test suite actually passes, the numbers match the
source. The goal-supervisor's validators exist for exactly this — use them
for anything with a completion contract.

## 15. State uncertainty transparently

The final answer must distinguish what you verified, what you inferred, and
what remains unknown. Confidence without provenance is noise. A result that
says "verified: A, B; assumed: C; unresolved: D" is more valuable than a
confident paragraph that hides the seams.
