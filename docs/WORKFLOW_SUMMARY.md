# LLM Orchestrator workflow: summary

Date: 2026-09-19. A one-page account of the local coding workflow: what each
step does, why it is shaped that way, and what it optimizes for. The detailed
reasoning and evidence are in the documents linked at the end. For the
current configured values, use `.venv/bin/python -m modeldeck.launch show --json`.
Saved operator settings override the defaults listed here.

## What it is

A pipeline of purpose-built scripts that takes a task and a target
repository to a verified change. It runs on one M1 Max (64 GB) using local
MTPLX models. Only one model is loaded at a time. The router
(`http://127.0.0.1:8100/v1`) is a thin OpenAI-compatible gateway. The
pipeline logic lives in `modeldeck/pipeline.py` and `scripts/*`. The desktop
Reports tab and the mobile web UI both drive the pipeline.

An external chat extension can still use the router for ad-hoc coding
through model `local`. The pipeline itself does not run inside one, because
running it that way caused three problems:

- Tool calls broke. The hybrid tool-call bridge produced malformed `<tool_call>` output.
- The extension's own system prompt always asks the model to present a plan, which conflicts with Scout's job of reporting findings.
- Every turn carried context overhead that a one-shot report does not need.

## The flow

```text
Task + target + project profile
    |
    v
Scout (feature)  |  Diagnose (troubleshoot)      MoE, tool-free report
    |  evidence, coverage, open questions, routing assessment
    v
Deterministic router ── simple ──> MoE Planner (Qwen3.6 Balance)
    |                                  | fails validation -> escalate once
    └── complex / uncertain / risky ─> Dense Planner (Qwen3.8 Quality, thinking)
    |  implementation-plan.md + work-items.json (validated graph)
    v
Builder                                          dense, native tools
    |  one work item at a time, fresh context each, progress ledger
    v
Runner-owned checks -> Auditor                   MoE, diff-anchored review
    |
    ├── checks pass + valid PASS ───────────────> accepted
    ├── high-risk / undemonstrated finding ─────> Verifier (dense, read-only, batched)
    │                                                 refuted -> dropped
    │                                                 confirmed -> repair scope
    ├── actionable, evidence-backed REJECT ─────> Renovator (dense) -> re-audit (max 1)
    └── UNKNOWN / malformed / env failure ──────> stopped, with reason
```

Flows are defined in `FLOW_ORDERS` in `modeldeck/pipeline.py`:

- **feature:** `scout → planner → builder → auditor`
- **troubleshoot:** `diagnose → planner → builder → auditor`

The Verifier and the Renovator are not fixed steps in either flow. The
pipeline adds them only when an audit result calls for them.

## Each step

### Scout (feature flow)
- **Model:** Qwen3.6-35B-A3B Balance (MoE), thinking on, sustained profile, MTP depth 1.
- **Job:** Survey the target and write `.ai/scout-report.md`. The report covers the relevant files and symbols, the existing tests, observed behavior (kept apart from hypotheses), the coverage achieved, open questions, and a structured `## Routing assessment` (`simple | complex | uncertain`, with reasons, affected files, risks, and a verification path).
- **Tools:** None. The script walks the file tree, not the model. Files are ranked in this order: paths named explicitly, then related tests and importers, then files whose names or contents match task terms. Tests and prose that do not relate to the task are listed only by name, so they use little of the context budget.

### Diagnose (troubleshoot flow)
- **Model:** Qwen3.6 Balance with its own role and port. It no longer borrows the Auditor's role.
- **Job:** Find the cause of a regression, starting from the diff. It writes `.ai/diagnosis-report.md` in the same shape as the Scout report.
- **Batch sweep (`modeldeck/triage.py`):** When a change set is too big for the context window, Diagnose reads it in batches. For each batch it keeps a short verdict and discards the file contents. Every changed file gets read, but never all at once. The alternatives were worse. Truncating the change set means the cause is included only by luck. An agent loop with read tools keeps every result in the conversation, so the context grows without limit.

### Deterministic router (`modeldeck/routing.py`)
- **Job:** Choose the dense planner or the MoE planner. The choice is a pure function of recorded evidence: no model calls, no I/O, and no clock.
- **Signals that force the dense planner:**
  - the routing assessment is missing or invalid, or is `complex` or `uncertain`
  - open questions are missing or unresolved
  - more than 5 files changed
  - a sensitive path is touched (persistence, schema, auth, protocol, `.sql`, `.prisma`, `.graphql`)
  - a new public interface
  - a re-plan after a rejected audit or a failed run
  - incomplete coverage
- **Simple route:** The MoE planner is used only when the assessment is `simple` and no dense signal fires. If its plan fails structural validation or declares an unresolved decision, the router escalates to the dense planner **exactly once**. A dense plan is never downgraded.
- **Operator override:** A per-run override takes precedence over the global override, and both take precedence over the signals.
- **Record:** The decision, the signals that fired, and a readable reason go into the run journal. The tram-map diagram on desktop and mobile also shows them.

### Planner
- **Models:** For complex work, Qwen3.8-27B Quality (dense), with thinking on at medium effort, turbo profile, depth 3, and Q8 KV cache. For simple work, Qwen3.6 Balance. A cloud planner (GPT-5.6 Sol) is available as an explicit option, never as an automatic fallback.
- **Inputs:** The Scout or Diagnose report, plus only the files that report cites. It checks those citations against the actual file contents. It does not re-read the whole tree, because that would pay Scout's prefill cost again. Input is scoped to about 24K tokens, and output is capped at 16K tokens.
- **Outputs:**
  - `implementation-plan.md`: the objective, the exact files and contracts to change, ordered steps, scope limits, and acceptance tests
  - `work-items.json`: units that each carry `depends_on`, `establishes`, `consumes`, and a declared verification
- **Validation before any edit:** The work-item graph must have no cycles, every dependency must resolve, and every `consumes` must be provided by some dependency's `establishes`. If validation fails, the error is recorded against the Planner, not the Builder.

### Builder (`scripts/builder_agent.py`)
- **Model:** Qwen3.8 Quality, thinking off, instruct sampling, native tool calls, up to 80 steps, and at most 24K completion tokens per turn.
- **Tools (`scripts/builder_tools.py`):** `read_file` (can read a line range), `search_text`, `list_files`, `replace_text` (anchored: it takes an expected file hash and an exact match count), `write_file` and `append_file` (both atomic), `run_command`, `ask_question`, and `finish_task`.
- **Work items:** The Builder handles work items one at a time, in dependency order. Each item starts a fresh conversation containing the item itself, the `establishes` contracts of its dependencies, the files it names, and the previous item's verification result. It does **not** get the accumulated tool output from earlier items. This is what keeps the prompt from growing across a run. In one earlier run the prompt had grown from 52K to 123K tokens.
- **Ledger:** `builder-progress.json` records each item's status. An item becomes `verified` only when the runner itself ran the item's declared check and it passed. The model cannot mark an item complete. A killed run resumes at the first item that is not verified.
- **Completion:** `finish_task(completed)` asks the runner to evaluate the work; it does not prove anything by itself. None of these ends a phase successfully: prose describing progress, output cut off by the length limit, running out of steps, or cancellation.
- **Watchdog (`modeldeck/watchdog.py`):** Plain rule-based tracking. It warns when the same tool call keeps returning the same result with no source change in between (warning at 3 repeats, stop at 5). It also stops after 4 invalid generations in a row. Repetition alone does not count as a stall, because running tests again after an edit is normal.

### Runner-owned verification (`modeldeck/profile.py`)
- **What runs:** The project profile (`.ai/project-profile.json`) declares the checks. Each check has a command, cwd, interpreter, environment, timeout, a `required` flag, and optionally `expect_min_tests`. Smoke checks run for languages that the main suite does not exercise.
- **Results:** Each result is one of `passed`, `code_failed`, `infrastructure_failed`, `timed_out`, or `not_run`. An environment failure blocks acceptance but is never reported as a code defect.
- **Invalidation:** Any edit after a passing check makes that check result stale. A check that exits with code zero but collects too few tests does not count as passing.

### Auditor (`scripts/auditor_report.py`)
- **Model:** Qwen3.6 Balance, thinking, precise-coding sampling.
- **Inputs:** The original requirement (not only the plan), the diff from the baseline to the current state, the related code and tests, the Builder ledger, and the actual verification results.
- **Outputs:** `audit-report.md`, plus `findings.json` with one record per finding. The `demonstrated` flag separates two kinds of finding: a defect that a failing check actually showed, and a hypothesis reached by reading the code.
- **Three separate outcomes:**
  - `model_verdict`: exactly `PASS`, `REJECT`, or `UNKNOWN`
  - `verification_status`
  - `effective_decision`: set by the runner

  A model-written PASS combined with a failing required check is never accepted. A REJECT that contains no parseable finding is a protocol error.

### Verifier (`scripts/verifier_report.py`)
- **Model:** The dense Planner role, reused so that the second opinion comes from a stronger model.
- **When it runs:** For findings that are high severity and not demonstrated, findings in a risky category (concurrency, persistence, auth, protocol), and findings the Renovator disputed.
- **Read-only:** The Verifier has no tools. The code enforces this; the prompt does not merely ask for it. Because it cannot edit anything, it can be trusted to contradict an audit.
- **Batched:** All findings that need checking go into one pass. That costs one model swap per audit instead of one per finding.
- **Verdicts:** Each finding comes back `confirmed`, `refuted`, or `needs_more_evidence`, with evidence. Refuted findings are removed from the repair scope, and their record is kept.

### Renovator (`scripts/renovator_agent.py`)
- **Model:** Qwen3.8 Quality, native tools, up to 80 steps.
- **Job:** Fix only the confirmed findings, working from a small packet: the finding IDs, excerpts, failed checks, and the allowed scope.
- **Disputes:** The Renovator can dispute a finding. A dispute reopens verification of that finding **once**, never twice.
- **Retry limit:** One repair pass followed by one re-audit (`MAX_RENOVATOR_RETRIES = 1`).

## Main design decisions

1. **The pipeline decides; the models do not.** Routing, acceptance, stall detection, and the choice of which findings to verify are all pure functions of recorded inputs. No model is asked to rate its own confidence or difficulty. `BUILDER_ACCURACY` showed that such self-ratings are opinions, not measurements.
2. **No model supervises another model.** Watchdogs, gates, and routing are deterministic. Adjudication uses a phase that already exists (the Verifier reuses the Planner role).
3. **Three outcomes are kept separate:** whether the process ran, what the model concluded, and what the runner accepted. A completed process is not completed work.
4. **Every run leaves a record.** Each run gets `.ai/runs/<run-id>/` containing a manifest, a baseline (including files that were already dirty or untracked), an event journal, verification logs, and hash-bound artifacts. Stale reports or reports from another run cannot be picked up by mistake. The operator's existing uncommitted edits are preserved and kept distinct from agent edits.
5. **Each handoff is a contract.** Every phase writes a readable Markdown report and a machine-readable file next to it (`work-items.json`, `findings.json`, `builder-progress.json`). Each is validated at the boundary, so a failure is attributed to the phase that caused it.
6. **One owner per run.** `modeldeck/ownership.py` makes sure only one process drives a run. The desktop and phone UIs attach to the same replayable event journal. Questions from the model are written to the run directory, so an answer from any client unblocks the run (`modeldeck/questions.py`).
7. **The orchestrator owns the pipeline, not a chat extension.** Hosting it inside one caused tool-call fragility and prompt conflicts (see the start of this document). The router stays a thin gateway that works the same for every client.
8. **The Builder has its own small tool loop.** It is a short, purpose-built loop with a hard step cap, not an agent framework. Its edits are anchored and atomic, so a one-line fix does not rewrite a large file.

## Optimization priorities, in order

1. **Verified correctness.** Success means an accepted change backed by evidence. When work cannot be completed, the run stops in an explicit stopped or blocked state. Tokens per second is not the success measure.
2. **Total time to a verified change, not decode speed.** A cheaper model that needs retries or makes bad edits is slower overall. In testing, the non-"Speed" variants of both model families finished faster and made fewer mistakes they then had to recover from. Qwen3.6 Balance took 247 s against 373 s for Speed. Qwen3.8 Quality took 772 to 1,072 s against 1,517 to 1,963 s for Speed.
3. **As few model swaps as possible.** Only one model is loaded at a time, so every added phase has to justify a swap. The Verifier batches its work. The residency check compares the live server's `/health` fingerprint so it can reuse a correctly configured server.
4. **A bounded context per unit of work.** Work items and fresh per-item conversations, the batched diagnosis sweep, and citation-scoped planning all serve this. The context window is treated as a limit, not as a size to fill.
5. **MoE models for breadth, the dense model where a mistake costs most.** Scout, Diagnose, Auditor, and simple planning run on the fast MoE model. Complex planning, building, verification, and repair run on the dense model. Planning is where the least error correction happens downstream, so it is the worst place to save compute.
6. **Speculative-decode (MTP) depth tuned per architecture.** MoE models run best at depth 1: acceptance at the third speculative position falls to 1 to 24%. Dense models run best at depth 3, with 81 to 92% acceptance and up to 2.79× the speed of plain decoding (see `DEPTH_TUNING.md`).
7. **Model-card sampling as the baseline.** The Qwen presets match the published recommendations. Changing the thinking mode also switches the sampler. Ornith has its own preset (0.6 / 0.95 / 20). Qwen3.6 exposes no effort tiers, so its effort control shows as unsupported.
8. **Conservative memory settings.** KV cache is off for the Balance MoE model (hybrid attention, about 2.5 GiB at 131K tokens) and Q8 for the dense Quality model. The RAM session bank is capped at 16G. SSD caching stays off until a cross-session contamination issue is resolved. There are no Q4 KV or lower-bit weight changes without accuracy tests.

## What is not yet proven

- No complete end-to-end benchmark has been run with live models since these changes were made.
- **Release gate:** Before the workflow is called reliable for unattended projects, it must complete one feature run and one regression run through the real service. Those runs must include a repair, a refuted finding, a cancelled tool process, and a checkpoint resume.
- **Metrics to measure:**
  - first-pass and final acceptance
  - repair scope
  - wall time
  - uncached prefill
  - model swap time
  - Verifier refutation rate
  - prompt tokens per completed work item

  The split of work between MoE and dense models is reported as a metric, not a target. Because the dense model decodes at roughly half the MoE speed, 15% of tokens on the dense model is about 26% of decode time.

## Further reading

- [WORKFLOW_REVIEW_2026-09-10.md](WORKFLOW_REVIEW_2026-09-10.md): the findings that motivated the redesign (R1 to R12)
- [WORKFLOW_IMPLEMENTATION_PLAN_2026-09-10.md](WORKFLOW_IMPLEMENTATION_PLAN_2026-09-10.md): phase 1, the evidence and safety foundation
- [WORKFLOW_PHASE2_PLAN_2026-09-10.md](WORKFLOW_PHASE2_PLAN_2026-09-10.md): phase 2, covering profiles, routing, work items, the Verifier, and the live fingerprint
- [MODEL_TUNING_REVIEW_2026-09-19.md](MODEL_TUNING_REVIEW_2026-09-19.md): sampling, effort, quantization, and memory
- [DEPTH_TUNING.md](DEPTH_TUNING.md): measured MTP depth for each model
- [ARCHITECTURE.md](ARCHITECTURE.md) and the [README](../README.md): the current architecture and configuration
