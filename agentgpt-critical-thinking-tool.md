# Critical Thinking Tool for AgentGPT

## Overview

The **Critical Thinking** tool should be implemented as a higher-level orchestration capability that temporarily launches a structured multi-agent workflow.

It should not simply append phrases such as “think step by step” or “think harder” to the model prompt. Its value should come from:

- Breaking a complex request into smaller subproblems
- Investigating those subproblems independently
- Gathering and evaluating evidence
- Challenging preliminary conclusions
- Resolving contradictions
- Producing a concise, defensible final synthesis

## Recommended Workflow

```text
User request
    ↓
1. Problem framing
    ↓
2. Subproblem decomposition
    ↓
3. Parallel investigation
    ↓
4. Evidence and assumption review
    ↓
5. Adversarial critique
    ↓
6. Contradiction resolution
    ↓
7. Final synthesis
```

## 1. Problem Framing

The coordinator first determines:

- What is actually being asked?
- What would constitute a good answer?
- What constraints did the user provide?
- What information is missing?
- Which claims require external research?
- Is this a factual, technical, strategic, mathematical, debugging, or decision-making problem?

This prevents the system from researching or solving the wrong interpretation of the request.

The framing stage should produce a structured problem definition:

```json
{
  "objective": "Determine whether Electron or Tauri is the better desktop framework for AgentGPT.",
  "success_criteria": [
    "Supports Windows, macOS, Linux, and ARM",
    "Keeps idle and long-session memory usage bounded",
    "Integrates well with Python workers and native processes",
    "Supports embedded browser and Android-container panels"
  ],
  "constraints": [
    "Local-first architecture",
    "Premium desktop user experience",
    "Cross-platform packaging",
    "Low memory overhead"
  ],
  "unknowns": [
    "Actual memory usage under representative workloads",
    "WebView compatibility across supported operating systems"
  ]
}
```

## 2. Subproblem Decomposition

The coordinator converts the original request into a small set of independently answerable subproblems.

For example:

> Should AgentGPT use Electron or Tauri?

Could become:

1. Compare runtime memory usage.
2. Compare Windows, macOS, Linux, and ARM support.
3. Evaluate Python and native-process integration.
4. Compare packaging and automatic update support.
5. Evaluate browser and Android-container embedding.
6. Compare development complexity.
7. Identify long-term architectural risks.

Each subproblem should include:

```json
{
  "id": "memory-comparison",
  "question": "How does Electron compare with Tauri for idle and sustained RAM usage?",
  "purpose": "Determine whether either framework violates AgentGPT's memory budget.",
  "requires_research": true,
  "dependencies": [],
  "priority": "high",
  "recommended_tools": [
    "web_search",
    "code_execution"
  ]
}
```

The decomposition stage should avoid creating too many overlapping tasks. Subproblems should be:

- Distinct
- Relevant to the final conclusion
- Small enough for one worker to investigate
- Large enough to justify a separate investigation

## 3. Parallel Investigation

The coordinator launches isolated worker agents for independent subproblems.

Workers should receive only:

- The original problem framing
- Their specific assigned subproblem
- Relevant constraints
- The tools required for that assignment
- A structured output schema

Possible worker tools include:

- Web research
- GitHub inspection
- Code execution
- File inspection
- Mathematical calculation
- Documentation search
- Browser automation
- Internal knowledge-base search

Isolation is important. If every worker sees the other workers' conclusions too early, they may converge prematurely and repeat the same assumptions.

A worker result could use this schema:

```json
{
  "subproblem_id": "memory-comparison",
  "summary": "Tauri generally has lower shell overhead, but total application memory may still be dominated by Python workers, browser processes, and local model runtimes.",
  "claims": [
    {
      "claim": "Tauri usually has a smaller baseline runtime footprint than Electron.",
      "classification": "supported_inference",
      "confidence": 0.82,
      "evidence_ids": [
        "source-1",
        "benchmark-2"
      ]
    }
  ],
  "assumptions": [
    "The application does not require Chromium-specific behavior unavailable in the operating system WebView."
  ],
  "limitations": [
    "Available benchmarks may not represent AgentGPT's actual workload."
  ],
  "recommended_follow_up": [
    "Build equivalent idle-shell prototypes and measure private working set over eight hours."
  ]
}
```

## 4. Evidence and Assumption Review

A reviewer agent examines every worker result.

The reviewer should identify:

- Supporting evidence
- Source quality
- Unsupported claims
- Assumptions
- Missing information
- Confidence
- Conflicting findings
- Whether the conclusion actually follows from the evidence

Every important statement should be classified as one of:

```text
Verified fact
Supported inference
Unverified assumption
Opinion or recommendation
Unknown
```

Suggested review structure:

```json
{
  "claim_id": "claim-27",
  "claim": "Tauri will materially reduce total AgentGPT memory usage.",
  "classification": "unverified_assumption",
  "evidence_quality": "medium",
  "logical_support": "partial",
  "issues": [
    "The comparison only measures shell overhead.",
    "Python, browser automation, and model runtimes may dominate total memory."
  ],
  "required_follow_up": "Measure full-application memory using representative workloads."
}
```

The reviewer should prioritize primary and authoritative sources, including:

- Official documentation
- Source repositories
- Technical specifications
- Reproducible benchmarks
- Direct code inspection
- Controlled tests

## 5. Adversarial Critique

A separate critic agent should attempt to disprove or weaken the emerging conclusion.

Its role should be explicitly adversarial:

> Identify weaknesses, unsupported assumptions, overlooked alternatives, misleading comparisons, hidden dependencies, and conditions under which the proposed conclusion would be wrong.

This is more useful than asking another agent to simply “review the answer.”

Depending on the problem, the runtime could launch specialized critics:

- General skeptic
- Security reviewer
- Cost reviewer
- Implementation reviewer
- Performance reviewer
- Reliability reviewer
- User-experience reviewer
- Legal or compliance reviewer

A critic result could include:

```json
{
  "target_conclusion": "AgentGPT should use Tauri.",
  "strongest_objections": [
    {
      "objection": "The operating-system WebView introduces inconsistent behavior across platforms.",
      "severity": "high",
      "conditions": [
        "AgentGPT depends on browser APIs that behave differently across WebView2, WebKit, and WebKitGTK."
      ],
      "recommended_test": "Run the browser panel and streaming UI test suite on every supported operating system."
    }
  ],
  "overlooked_alternatives": [
    "Electron with aggressive process isolation and memory budgets",
    "A native shell with a separately hosted web UI"
  ],
  "confidence": 0.86
}
```

## 6. Contradiction Resolution

The coordinator compares competing findings instead of hiding disagreements.

For every material contradiction, it should record:

```json
{
  "claim_a": "Tauri will use substantially less memory.",
  "claim_b": "The difference will be limited once Python workers are running.",
  "resolution": "Both can be true. Tauri can lower shell overhead while the total application's memory remains dominated by tool workers and model runtimes.",
  "remaining_uncertainty": "The actual percentage improvement is unknown without an AgentGPT-specific benchmark.",
  "confidence": 0.87
}
```

Possible contradiction-resolution strategies:

1. Determine whether both claims apply under different conditions.
2. Compare the quality and recency of the supporting evidence.
3. Launch a focused follow-up investigation.
4. Run a reproducible experiment.
5. Preserve the disagreement in the final answer if it cannot be resolved.

The system should not force false certainty.

## 7. Final Synthesis

The synthesis agent should produce a concise final result containing:

- Direct conclusion
- Executive summary
- Key findings
- Important assumptions
- Strongest counterarguments
- Risks and tradeoffs
- Confidence level
- Unresolved questions
- Recommended next actions
- Sources or evidence references

It should not dump every worker's internal reasoning into the main chat. Instead, it should expose a readable decision record.

Suggested result schema:

```python
class CriticalThinkingResult:
    conclusion: str
    executive_summary: str
    findings: list[Finding]
    assumptions: list[Assumption]
    counterarguments: list[Counterargument]
    contradictions: list[Contradiction]
    unresolved_questions: list[str]
    recommended_actions: list[str]
    confidence: float
    sources: list[Source]
    execution_summary: ExecutionSummary
```

## Suggested Tool Interface

```python
from typing import Literal


def critical_thinking(
    task: str,
    mode: Literal[
        "analyze",
        "research",
        "compare",
        "decide",
        "debug",
        "plan",
    ] = "analyze",
    depth: Literal[
        "quick",
        "standard",
        "deep",
    ] = "standard",
    max_subproblems: int = 8,
    max_iterations: int = 3,
    allow_web_research: bool = True,
    allow_code_execution: bool = True,
    allow_file_access: bool = True,
    require_adversarial_review: bool = True,
    require_source_verification: bool = True,
    model_strategy: Literal[
        "same_model",
        "mixed_models",
        "strongest_available",
    ] = "mixed_models",
    token_budget: int | None = None,
    time_budget_seconds: int | None = None,
) -> CriticalThinkingResult:
    ...
```

## Agent Structure

A practical internal structure would use four main roles:

```text
Coordinator
├── Investigator workers
├── Evidence reviewer
├── Adversarial critic
└── Synthesis agent
```

### Coordinator

Responsible for:

- Framing the problem
- Creating the investigation plan
- Assigning workers
- Tracking dependencies
- Detecting contradictions
- Deciding whether follow-up work is justified
- Enforcing budgets
- Producing the final execution record

### Investigator Workers

Responsible for:

- Answering a narrowly scoped subproblem
- Using only the tools needed for that assignment
- Returning claims, evidence, assumptions, limitations, and confidence
- Avoiding conclusions outside their assigned scope

### Evidence Reviewer

Responsible for:

- Evaluating source quality
- Checking whether claims follow from evidence
- Finding unsupported assumptions
- Detecting duplicate or circular evidence
- Identifying missing information

### Adversarial Critic

Responsible for:

- Challenging the leading conclusion
- Finding failure conditions
- Identifying overlooked alternatives
- Surfacing hidden costs and risks
- Proposing tests that could falsify the conclusion

### Synthesis Agent

Responsible for:

- Combining accepted findings
- Preserving meaningful uncertainty
- Explaining resolved and unresolved contradictions
- Producing a direct recommendation
- Avoiding repetition and raw reasoning dumps

## Runtime State Machine

The orchestration should be implemented as a deterministic state machine around model calls:

```text
FRAME
  → DECOMPOSE
  → INVESTIGATE
  → REVIEW
  → CRITIQUE
  → RESOLVE
  → SYNTHESIZE
  → COMPLETE
```

Possible transitions:

```text
REVIEW
  → INVESTIGATE
    when evidence is insufficient

CRITIQUE
  → INVESTIGATE
    when the critic identifies a material unanswered question

RESOLVE
  → INVESTIGATE
    when a contradiction requires new evidence

Any state
  → SYNTHESIZE
    when the user selects "Stop and synthesize now"

Any state
  → FAILED
    when the runtime reaches a fatal error and has no usable partial results
```

The state machine should control retries, recursion, and spending instead of allowing the language model to create an unbounded loop.

## Depth Presets

### Quick

- Two to three subproblems
- One investigation pass
- One critic
- No follow-up research unless essential
- Small token and execution budget

Best for:

- Moderately complex questions
- Quick comparisons
- Requests where latency matters

### Standard

- Up to six subproblems
- Parallel workers
- Evidence review
- One contradiction-resolution pass
- Adversarial critique
- Moderate budget

Best for:

- Architecture questions
- Technical comparisons
- Business decisions
- Multi-cause debugging

### Deep

- Up to ten or twelve subproblems
- Multiple independent investigators for critical claims
- Source verification
- Specialist critics
- Up to three focused follow-up iterations
- Detailed confidence and uncertainty report
- Larger budget

Best for:

- High-impact decisions
- Broad research
- Complex system design
- Security or compliance reviews
- Difficult root-cause investigations

## Invocation Rules

The primary AgentGPT agent could invoke the tool automatically when it detects:

- A high-impact decision
- Multiple plausible answers
- Conflicting evidence
- A broad research request
- Architecture or strategy work
- Debugging with several possible root causes
- Requests explicitly asking for deep or critical analysis
- Decisions where incorrect advice would be expensive or difficult to reverse

It should normally avoid invoking the tool for:

- Simple factual questions
- Basic calculations
- Casual conversation
- Straightforward rewriting
- Requests already answerable with one direct tool call

The user should also be able to manually enable it through:

- A **Critical Thinking** tool toggle
- A `/critical` command
- A depth selector
- A “Think more critically” action on an existing answer

## UI Behavior

In AgentGPT, the right-side activity panel could display:

```text
Critical Thinking
✓ Framed the problem
✓ Created 6 investigations
● Investigating framework compatibility
● Reviewing memory benchmarks
○ Adversarial review
○ Contradiction resolution
○ Final synthesis
```

Each subproblem could be expandable and show:

- Assignment
- Status
- Assigned model
- Tools used
- Sources
- Concise finding
- Confidence
- Errors or retries

Useful controls:

- Stop
- Pause
- Stop and synthesize now
- Increase depth
- Add a question
- Retry failed investigation
- Open evidence
- Export decision record

The interface should distinguish between:

- Active work
- Completed findings
- Failed tasks
- Waiting dependencies
- Unresolved contradictions

## Model Strategy

The runtime should support several model-allocation strategies.

### Same Model

Use the currently selected model for every role.

Advantages:

- Predictable behavior
- Simple configuration
- Works fully locally

Disadvantages:

- Workers may share the same blind spots
- Less independent analysis

### Mixed Models

Use different available models for investigators, reviewer, critic, and synthesis.

Example:

```text
Coordinator: strongest reasoning model
Investigators: fast local models
Evidence reviewer: strong factual model
Adversarial critic: different model family
Synthesis: strongest reasoning model
```

Advantages:

- Greater diversity
- Lower cost when workers use smaller models
- Reduced correlated failure

### Strongest Available

Use the strongest configured model for every critical stage.

Advantages:

- Highest expected answer quality

Disadvantages:

- Highest latency and token cost
- May be unnecessary for simple subproblems

## Budget and Safety Controls

The runtime must enforce hard limits:

- Maximum subproblems
- Maximum workers
- Maximum iterations
- Token budget
- Time budget
- Tool-call budget
- Maximum source count
- Maximum worker output size
- Maximum concurrent processes

When a limit is reached, the runtime should synthesize the best available partial result rather than failing silently.

Example execution policy:

```json
{
  "max_subproblems": 8,
  "max_parallel_workers": 4,
  "max_iterations": 3,
  "max_tool_calls": 40,
  "token_budget": 120000,
  "time_budget_seconds": 600,
  "on_budget_exhausted": "synthesize_partial"
}
```

## Recursion Prevention

Agents should not be allowed to recursively invoke the `critical_thinking` tool.

Without this restriction:

```text
Main agent
  → critical_thinking
      → worker
          → critical_thinking
              → more workers
```

This could create unbounded recursion, runaway token usage, excessive processes, and confusing execution trees.

Recommended controls:

- Mark the runtime context with `critical_thinking_active=true`
- Hide the tool from child workers
- Reject nested calls at the tool boundary
- Allow only the coordinator to create worker tasks
- Enforce a maximum orchestration depth of one

## Memory Management

Because AgentGPT is intended to remain lightweight over long sessions, the Critical Thinking runtime should use bounded memory.

Recommended behavior:

- Store large worker outputs on disk rather than permanently in RAM
- Keep only summaries and references in the coordinator context
- Stream worker results
- Unload completed worker contexts
- Destroy idle worker processes
- Use bounded caches
- Avoid copying the full source corpus into every worker prompt
- Persist a compact decision record for later reopening
- Allow the user to delete the full execution trace while retaining the final answer

Suggested execution record:

```json
{
  "run_id": "ct_01JXYZ",
  "task": "Should AgentGPT use Electron or Tauri?",
  "status": "complete",
  "created_at": "2026-07-23T06:00:00-07:00",
  "subproblem_count": 7,
  "worker_count": 4,
  "iterations": 2,
  "token_usage": {
    "input": 48200,
    "output": 12900
  },
  "artifacts": {
    "decision_record": "runs/ct_01JXYZ/decision.json",
    "evidence_index": "runs/ct_01JXYZ/evidence.json"
  }
}
```

## Failure Handling

A failed worker should not necessarily fail the entire run.

The coordinator should:

1. Record the failure.
2. Determine whether the subproblem is material.
3. Retry with a corrected prompt or another model when justified.
4. Reassign the task if another worker is available.
5. Continue with partial evidence when the missing result is non-critical.
6. Clearly disclose unresolved gaps in the synthesis.

Possible worker states:

```text
QUEUED
RUNNING
WAITING_FOR_TOOL
COMPLETE
FAILED_RETRYABLE
FAILED_FINAL
CANCELLED
```

## Source and Evidence Tracking

Every sourced claim should reference an evidence object:

```json
{
  "evidence_id": "source-17",
  "type": "official_documentation",
  "title": "Tauri Architecture Documentation",
  "location": "https://example.com",
  "retrieved_at": "2026-07-23T06:12:00-07:00",
  "source_quality": "high",
  "used_by": [
    "worker-2",
    "reviewer-1"
  ],
  "supports_claims": [
    "claim-11"
  ]
}
```

The runtime should distinguish between:

- Primary sources
- Secondary sources
- User-provided files
- Direct experiments
- Model knowledge
- Inferences produced by the agents

Model knowledge without verification should not be represented as sourced fact.

## Example Execution

User request:

> Should we add an Android container to AgentGPT for automating native Android applications?

The coordinator might create:

1. Determine viable Android container technologies.
2. Evaluate Windows, macOS, Linux, and ARM support.
3. Evaluate GPU and display streaming requirements.
4. Design persistent named profiles.
5. Evaluate input automation through ADB and accessibility APIs.
6. Evaluate security isolation.
7. Estimate download size and dependency management.
8. Evaluate memory and CPU overhead.

Workers investigate independently.

The reviewer finds that some proposed Android container technologies do not support every target platform.

The critic argues that bundling a full Android runtime could violate the application's lightweight design goals.

The contradiction resolver concludes:

- The capability is valuable.
- It should be optional and disabled by default.
- Dependencies should download only after explicit user enablement.
- Named profiles should persist application state and logins.
- The runtime should execute in an isolated process or container.
- The UI should show dependency download progress.
- Idle instances should automatically stop to reclaim memory.

The synthesis then gives a direct architectural recommendation rather than dumping all worker conversations.

## Implementation Recommendation

Internally, Critical Thinking should be implemented as a **skill or orchestration runtime backed by several low-level tools**, but exposed to the main Strands agent as one callable tool:

```text
Main Agent
    ↓ calls
critical_thinking
    ↓ internally orchestrates
Coordinator
    ├── Investigator agents
    ├── Research and execution tools
    ├── Evidence reviewer
    ├── Adversarial critic
    └── Synthesis agent
```

This keeps the primary agent's tool list simple while allowing the Critical Thinking runtime to become sophisticated independently.

The central design principle should be:

> More reasoning is not automatically better reasoning. The system should improve independence, evidence quality, criticism, and contradiction resolution—not merely generate more tokens.
