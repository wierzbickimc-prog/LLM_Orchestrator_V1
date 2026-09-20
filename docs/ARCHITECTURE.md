# Architecture

## Current design (superseded the original "the chat extension stays the agent" hypothesis)

This project started from: "an editor chat extension should remain the
tool-using coding agent; the router only normalizes model access." Running the
four-phase pipeline that way in practice hit real fragility -- such an
extension's system prompt carries an unconditional "always present a plan"
instruction that fights a summarizer's actual job, its environment/context
block and tool-schema overhead inflate context every turn, and mtplx's
`--tool-prompt-mode hybrid` bridge (needed to translate an OpenAI-style `tools`
field into this model family's native call format) produced a malformed
`<tool_call>` that stalled a whole turn. See the README's "The routed pipeline"
section for the full account.

The router still normalizes model access exactly as originally designed --
it's a thin gateway that doesn't know or care whether a request came from an
editor extension or from `scripts/*_report.py`. What changed is that the
four-phase pipeline's orchestration now lives in standalone scripts and the
Model Deck Reports tab instead of inside a chat conversation. An external
client is still fully supported for ad-hoc coding against `model: local`.

## Model roles

### Scout
Model: Qwen3.6-35B-A3B Balance FP16, sustained/D1, thinking enabled.

Reads whatever target path it's given directly (no tools -- the *script*
walks the tree), and writes `.ai/scout-report.md`: relevant files/symbols,
existing tests, observed behavior vs. hypotheses, and open questions for
Planner. No prior context assumed; this is the entry point.

For large trees, Scout ranks explicit paths first, then related tests and
importers, then task terms found in paths and file contents. The policy is
language- and repository-agnostic; it does not contain this project's
filenames. Unrelated tests and prose are supplied as compact name/heading
inventories so the model knows they exist without spending the implementation
context budget on their full bodies.

### Planner
Local by default: structured Scout/Diagnose complexity plus deterministic risk
checks select Balance for simple plans or Quality with medium thinking for
complex/uncertain plans. Both routes keep the same plan/work-item contract.
Cloud planning remains an explicit backend option.

Reads `.ai/scout-report.md` plus *only the specific files it cites*
(`report_common.referenced_files`), independently verifies those claims
against the actual file contents, and writes `.ai/implementation-plan.md`:
objective, exact files/contracts to change, ordered steps, scope
boundaries, tests/acceptance criteria, and any discrepancies found in
Scout's report. Does not read the whole tree -- that would just re-pay
Scout's own prefill cost a second time.

### Builder
Model: Qwen3.8-27B MTPLX speed-optimized.

The one role with real, multi-step tool use: `read_file`, `write_file`,
`run_command`, via a small custom loop (`scripts/builder_agent.py`) that
parses its own fenced ` ```tool ` JSON convention rather than the OpenAI
`tools` field, for the same tool-call-fragility reasons above. Reads
`.ai/implementation-plan.md`, implements only what it calls for, and writes
`.ai/builder-report.md`. Not sandboxed beyond staying inside the target
path -- it genuinely edits files and runs shell commands with your
permissions.

### Auditor
Model: Qwen3.8-27B MTPLX speed-optimized (a second, separate instance/role
from Builder, so the audit isn't reviewing its own work with the same
context).

Reads `.ai/implementation-plan.md` and the *whole* current tree (not scoped
to cited files -- part of its job is catching changes the plan never called
for, which a narrower scope would hide), and writes `.ai/audit-report.md`:
plan compliance, test coverage, error handling, unintended scope, findings
ordered by severity.

Alongside the prose it emits `.ai/findings.json`: one record per finding,
each carrying a `demonstrated` flag that separates a defect a failing check
actually exhibited from one reached by reading. A REJECT with no parseable
finding is a protocol error at this boundary, not a Builder failure.

### Verifier
Model: the dense Planner role, reused (`scripts/verifier_report.py`).

Read-only by construction -- it is given no tools at all, which is what
lets it be trusted to contradict an audit. It adjudicates only the findings
that earn it: high severity and not demonstrated, a risky category
(concurrency, persistence, auth, protocol), or disputed by the Renovator.
All of them go in one batched pass, so on sequential residency this costs
one model swap per audit rather than one per finding. Each finding comes
back `confirmed`, `refuted`, or `needs_more_evidence`, with evidence.

Refuted findings are dropped from the repair scope and keep their record,
which is the point: without it, a wrong finding gets implemented, correct
code is edited to match the mistaken claim, and the re-audit passes because
the code now matches. A Renovator dispute reopens exactly one verification
for that finding and never a second.

### Work items and the ledger
The Planner emits `.ai/work-items.json` beside its prose plan: ordered
units with `depends_on`, `establishes`, `consumes`, and a declared
verification each. The graph is validated before any file is edited -- a
cycle or a dangling dependency is a Planner defect, attributed there rather
than surfacing later as a Builder failure.

The Builder walks that graph one unit at a time. Each unit gets its own
conversation: the unit, its dependencies' `establishes` strings, the files
it names, and the previous unit's verification result -- explicitly not the
accumulated tool output of prior units. That is what stops prompt size
growing monotonically across a run, and `establishes` is what makes the
reset safe: an interface decision survives as a stated contract rather than
as a transcript nobody can afford to keep.

`.ai/builder-progress.json` records each unit's status. A unit reaches
`verified` only when its declared verification actually ran and passed,
recorded by the runner -- `completed` is deliberately not a status the
model can assert. A run killed partway resumes at the first unverified
unit instead of redoing everything.

Only one MTPLX model is resident at a time by default (each role has its own
port; Model Deck stops the previous role's process before launching the
next), so phases are sequential rather than simultaneously resident.

## Non-goals, revisited

The original v0.1 non-goals list included "autonomous multi-agent
orchestration" and "Pi/Hermes/OpenCode integration," reconsidered "only if
a chat extension + thin routing proves insufficient." It did: Builder is now exactly
this kind of autonomous, multi-step agent loop, deliberately scoped small
(three tools, no OpenAI `tools` field, hard step cap) rather than adopting
an existing framework -- see the README for why a purpose-built loop was
chosen over Pi/OpenClaw/similar. RAG/vector search,
a persistent task queue, and a general policy engine remain non-goals; none
of tonight's work needed them.
