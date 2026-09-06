# Architecture

## Current design (superseded the original "Cline stays the agent" hypothesis)

This project started from: "Cline should remain the tool-using coding agent;
the router only normalizes model access." Running the four-phase pipeline
through Cline in practice hit real fragility -- Cline's own system prompt
carries an unconditional "always present a plan" instruction that fights a
summarizer's actual job, its `environment_details` block and tool-schema
overhead inflate context every turn, and mtplx's `--tool-prompt-mode hybrid`
bridge (needed to translate Cline's `tools` field into this model family's
native call format) produced a malformed `<tool_call>` that stalled a whole
turn. See the README's "The four-phase pipeline, without Cline" section for
the full account.

The router still normalizes model access exactly as originally designed --
it's a thin gateway that doesn't know or care whether a request came from
Cline, Continue, or `scripts/*_report.py`. What changed is that the
four-phase pipeline's orchestration now lives in standalone scripts and the
Model Deck Reports tab instead of inside a Cline conversation. Cline is
still fully supported for ad-hoc coding against `model: local`.

## Model roles

### Scout
Model: Qwen3.6-35B-A3B MTPLX speed-optimized.

Reads whatever target path it's given directly (no tools -- the *script*
walks the tree), and writes `.ai/scout-report.md`: relevant files/symbols,
existing tests, observed behavior vs. hypotheses, and open questions for
Planner. No prior context assumed; this is the entry point.

### Planner
Cloud GPT-5.6 Sol by default, or a local fallback through any resident role
(Scout/Builder/Auditor) -- configurable in Model Deck's Deck tab.

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

Only one MTPLX model is resident at a time by default (each role has its own
port; Model Deck stops the previous role's process before launching the
next), so phases are sequential rather than simultaneously resident.

## Non-goals, revisited

The original v0.1 non-goals list included "autonomous multi-agent
orchestration" and "Pi/Hermes/OpenCode integration," reconsidered "only if
Cline + thin routing proves insufficient." It did: Builder is now exactly
this kind of autonomous, multi-step agent loop, deliberately scoped small
(three tools, no OpenAI `tools` field, hard step cap) rather than adopting
an existing framework -- see the README and `docs/NEXT_STEPS.md` for why a
purpose-built loop was chosen over Pi/OpenClaw/similar. RAG/vector search,
a persistent task queue, and a general policy engine remain non-goals; none
of tonight's work needed them.
