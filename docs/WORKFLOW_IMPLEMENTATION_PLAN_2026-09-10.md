# Implementation plan: reliable MoE-first project workflow

Date: 2026-09-10. Companion evidence: [workflow review](WORKFLOW_REVIEW_2026-09-10.md).

## Objective and boundaries

Enable local MoE models to investigate, implement, and initially review most project work, with a dense model handling focused planning, disputed diagnoses, difficult verification, and repairs. Success means a verified change with attributable evidence and a clear stopped/blocked state when work fails.

Preserve the stable OpenAI-compatible endpoint, desktop launch experience, selectable models, existing project changes, and readable Markdown handoffs. Keep sequential model residency on this machine. No automatic cloud calls for local-only runs. No arbitrary report line limit. Do not add a second model just to monitor the first; deterministic checks own routine stall detection.

This is an implementation handoff, not a claim these changes are already shipped. Execute in the order below, with small reviewable commits. Preserve the existing uncommitted web/UI/test work when applying changes. Do not feed the entire plan to one accumulating Builder session; each numbered increment is a separate work unit with its own acceptance criteria.

## Proposed workflow

```text
Task + target + project execution profile
    |
    v
MoE Scout / Diagnose -> numbered evidence + coverage + open questions
    |
    v
Plan (MoE for routine scoped work; dense for consequential decisions)
    |
    v
MoE Builder: dependency-aware work units + durable progress ledger
    |
    v
Runner-owned verification -> MoE audit of task + plan + actual diff
    |
    +-- checks pass and review is sufficient -> complete
    |
    +-- disputed/high-risk finding -> focused dense verification
    |                                 |
    +-- reproducible defect -----------+-> dense Renovator -> verify/re-audit
    |
    +-- protocol/environment/cancellation problem -> explicit stopped state
```

Dense review can correct an unsupported finding as well as code. Failed infrastructure checks must not be represented as verified defects. Final acceptance remains tied to the original requirement, not only compliance with a generated plan.

## 1. Repair request and tool-mode wiring — P0, small

**Files:** `scripts/report_common.py`, `modeldeck/pipeline.py`, `router/main.py`, `modeldeck/prompts.py`, relevant router/pipeline tests; add a small shared contract module if needed.

**Changes:**

- Share the `X-Model-Deck-Skip-Injection` name between producer and consumer; accept the historical alternate spelling during migration.
- Forward `--native-tool-calling` for Builder/Renovator when enabled. Use the same resolved role snapshot for model launch and script arguments.
- Validate native arguments against the tool schema locally: known name, object arguments, required fields/types. Feed a structured protocol error back for a bounded retry; unknown tools must not reach an AssertionError.
- Make `local` resolve the selected role's reasoning/sampling contract just like its explicit alias.
- Keep report prompts/tool-loop prompts distinct, with their effective text and hash available for inspection. Do not unify them into the chat prompt by concatenation.

**Acceptance:**

- An actual script-shaped HTTP request reaches a mocked upstream with no injected chat instructions. A normal chat request still receives its configured phase prompt.
- Builder and Renovator each send an actual tools array only when native mode is enabled; launch arguments agree with client protocol.
- `local` and its active role produce equivalent effective model controls.
- Invalid native calls yield typed errors without executing a tool or crashing the run.

## 2. Introduce truthful phase results and verification gates — P0, medium

Implement this phase as three independently reviewable increments. Keep three concepts separate throughout:

- **execution status:** whether a process or generation completed normally;
- **review disposition:** what an Auditor concluded about the implementation;
- **effective acceptance:** what the runner permits after applying mandatory verification and policy.

A successfully executed Auditor may return `REJECT`; that is not a failed Auditor process. Conversely, a model-written `PASS` is not effective acceptance when mandatory checks did not pass.

### 2A. Structured generation and explicit agent termination

**Files:** `scripts/report_common.py`, `scripts/builder_agent.py`, `scripts/renovator_agent.py`, native/fenced tool schemas and tests.

**Changes:**

- Return the same structured generation shape from both stream readers: content, optional reasoning, tool calls, finish reason, terminal state, error details, and usage when supplied. Distinguish receipt of the transport terminator from the model's finish reason and from semantic task completion.
- Handle CRLF SSE, explicit stream error events, malformed required data, premature EOF, token/content-filter truncation, and cancellation. Preserve partial content and error metadata when the transport supplied usable data; do not reduce these cases to an exception that discards the partial response.
- Add an explicit `finish_task` action with `completed` and `blocked` requests. Ordinary progress prose is never completion. `finish_task(completed)` asks the runner to evaluate completion; it does not certify its own claim.
- Step exhaustion, repeated empty output, truncation, disconnection, watchdog termination, and cancellation produce distinct incomplete/error outcomes. Save partial reports separately and never replace a previously valid completed handoff with partial work.
- Validate native and fenced calls against one tool contract before dispatch: known name, object arguments, required fields, field types, and allowed enum values. Return a typed protocol error for a bounded retry; never execute a partially validated call.

**Acceptance:**

- Progress-only prose, nonempty length-truncated text, zero-byte responses, stream error after partial text, premature EOF, and max-step exhaustion cannot finish a phase successfully.
- Partial content and the terminal reason remain inspectable after an interrupted or malformed stream.
- `finish_task(completed)` before verification is rejected without ending the agent loop; `finish_task(blocked)` preserves the reason, partial work, and incomplete requirements without appearing completed.
- Unknown tools and invalid argument types execute nothing and return a typed, recoverable protocol error.

### 2B. Runner-owned verification and audit decisions

**Files:** shared verification code, `scripts/auditor_report.py`, `scripts/diagnose_report.py`, Builder/Renovator completion gates, `modeldeck/pipeline.py`, project execution-profile schema and tests.

**Verification policy:** resolve this before editing begins and keep it immutable for the phase. At minimum it records:

```text
command / structured argv
resolved cwd and interpreter
required or optional
timeout
expected test collection or other success evidence, when known
environment overrides (with secrets redacted)
```

**Changes:**

- The runner, not the model, selects and executes mandatory verification from the resolved project profile. A model-selected command and an arbitrary exit-zero command are useful evidence but cannot satisfy a different mandatory check.
- Record structured verification results: command, cwd, interpreter, start/end time, exit code, timeout/launch status, test counts where available, bounded display output, and a reference to the complete local log.
- Classify `passed`, `code_failed`, `infrastructure_failed`, `timed_out`, and `not_run` separately. Infrastructure failure prevents acceptance but is not automatically a demonstrated source defect and must not fabricate a Renovator scope.
- Require mandatory checks to run after the latest source mutation. A later edit invalidates earlier verification. Where an expected test count is configured, an exit-zero run that collects too few tests is not sufficient.
- Parse model audit dispositions as exact `PASS`, `REJECT`, or `UNKNOWN`. Preserve the model's raw report and verdict; record the runner's effective decision separately rather than rewriting the raw verdict.
- Effective acceptance requires all mandatory verification to pass, a structurally valid review, and no unresolved required coverage. A valid `REJECT` may become `needs_repair` only when it contains an actionable, evidence-backed Fix List. `UNKNOWN`, malformed review, missing context, and infrastructure failure stop without invoking Renovator.

**Decision record:**

```text
model_verdict: PASS | REJECT | UNKNOWN
verification_status: passed | code_failed | infrastructure_failed | timed_out | not_run
effective_decision: accepted | needs_repair | incomplete | stopped
reason: runner-owned explanation
```

**Acceptance:**

- A mocked Auditor `PASS` paired with a failing, timed-out, missing, or under-collected mandatory check is never accepted.
- A successful unrelated command cannot satisfy the configured verification policy.
- A code failure plus a valid, evidence-backed Fix List may queue repair; an environment failure, `UNKNOWN`, or malformed REJECT cannot.
- Editing after a green check invalidates that check and requires verification to run again.
- The original model verdict remains available for model-quality measurement even when the effective decision differs.

### 2C. Versioned phase-result transport and handoff validation

**Files:** new `modeldeck/results.py`, all report/repair entrypoints, `modeldeck/pipeline.py`, result/handoff tests.

**Minimal result identity:** introduce only the identity needed to prevent cross-run or stale handoffs here; Phase 3 expands it into the full immutable run manifest.

```text
schema_version
run_id
phase
attempt
execution_status
review_disposition (Auditor only)
effective_decision
input/config hash
artifacts: path + content hash + role
verification references
incomplete requirements
error details
```

**Changes:**

- Transport the result through an atomic `--result-path` JSON file or a dedicated file descriptor/control channel. Human-readable stdout/stderr remains presentation-only; a prefixed line mixed into stdout is not the authoritative result transport.
- Validate the schema version, run/phase/attempt identity, expected input hash, artifact role, artifact existence, and content hash before publishing or consuming a handoff.
- Treat a missing, malformed, duplicate, stale, wrong-phase, wrong-run, or hash-mismatched result as a protocol error. Never recover a success result by parsing human prose such as `Wrote ...`.
- Validate report structure per role. Examples: an Auditor REJECT requires an actionable Fix List; a Planner requires stable work items and verification criteria; Scout/Diagnose require evidence, coverage, and open questions. Length may be a truncation signal but is not structural validity by itself.
- Write completed and partial artifacts atomically. A partial artifact uses the current run/attempt identity and cannot be mistaken for the canonical completed artifact.

**Acceptance:**

- A valid result is consumed without inspecting human stdout. Arbitrary stdout containing result-like text cannot forge completion.
- Missing/nonempty but structurally invalid handoffs stop before downstream execution.
- Stale artifacts, wrong-run results, mismatched phases/attempts, altered artifact content, and duplicate terminal results are rejected.
- Two concurrent or resumed tasks targeting the same directory cannot adopt each other's result.
- Existing completed artifacts survive any later failed, blocked, cancelled, or truncated attempt.

## 3. Record immutable run evidence and a baseline — P1, medium

**Files:** new `modeldeck/runs.py`, `scripts/report_common.py`, `scripts/builder_agent.py`, `scripts/renovator_agent.py`, `scripts/auditor_report.py`, `modeldeck/pipeline.py`.

**Artifact contract:**

```text
<target>/.ai/runs/<run-id>/
  manifest.json             # task, target, revisions, runtime/config/prompt hashes
  baseline.json             # tracked + dirty + untracked starting state
  events.jsonl              # timestamped structured phase/tool transitions
  evidence/                 # selected source versions, diffs, reproduction logs
  verification/             # exact commands, outcomes, full output
  scout-report.md or diagnosis-report.md
  implementation-plan.md + work-items.json
  builder-progress.json + builder-report.md
  audit-1.md + findings-1.json
  renovator-report.md
  audit-2.md + result.json
```

**Changes:**

- Expand Phase 2's minimal run identity into the immutable manifest and evidence layout above; do not introduce a second, competing run identifier or result format.
- Capture a baseline without committing, reverting, or discarding the operator's dirty files. For a non-Git project, use a file-hash/content snapshot sufficient to establish changes and recover edited files.
- Atomically publish artifact versions; maintain current `.ai/*.md` names as convenience copies/pointers only. Downstream phases read the explicit run manifest.
- Journal tool requests/results, command exit codes, source hashes, token usage, and decisions. Avoid recording secrets; full source/log evidence stays local with explicit retention.
- Derive changes from before/after state, including command edits, deletions, renames, and new files. Preserve both Builder and Renovator changes rather than replacing a touched list.
- Require an explicit matching run/plan/baseline on resume. Stale reports from other tasks are never silently adopted.

**Acceptance:** Restart/re-audit retains the original rejection, all repair evidence, and partial Builder progress. No-touch runs cannot inherit old touched files. Two tasks on the same target remain distinguishable. Pre-existing dirty edits are preserved and distinguished from agent edits.

## 4. Put execution and cancellation under one owner — P1, medium/large

**Files:** `modeldeck/pipeline.py`, `modeldeck/mtplx.py`, `router/main.py`, `router/web.py`, `modeldeck/gui.py`, web UI, pipeline/web tests.

**Changes:**

- Let the local service own a single state machine and residency lock. GUI/web send start, continue, stop, answer, and inspect requests; they do not create competing pipeline controllers.
- Transition internally when a phase finishes, exactly once, before broadcasting its result. Full mode advances without a connected UI; step mode pauses durably.
- Implement Continue and forward/validate `feature` versus `troubleshoot` consistently. Reject concurrent incompatible runs/config changes with a clear busy response.
- Replace blocking queue waits in async endpoints with asynchronous queues or a thread bridge. Add replayable event sequence IDs and bounded backpressure so reconnection does not lose the latest state.
- Replace fixed-size stdout reads and question parsing with a dedicated JSONL event channel plus independent output streaming. Questions must be delivered as soon as the complete event arrives. Keep Phase 2C's atomic terminal-result transport authoritative; the event stream reports live progress and must not create a second completion contract.
- Propagate cancellation during model load, request generation, shell/test execution, and questions. Use owned process groups and graceful-then-forced cleanup with explicit time bounds. Confirm backend activity drains before permitting a conflicting launch.
- Dispatch all Qt widget changes on the Qt thread. Dismissing a question means cancelled/pending, not selection of its first option.

**Acceptance:** A fake complete feature flow and troubleshoot flow run through the web API without a GUI. Disconnect/reconnect leaves execution intact. Continue advances once. An open SSE subscriber does not delay a second health/inference request. Stop works during loading, short-question waiting, and a command with a child process; descendants cannot continue writing after cancellation.

## 5. Give the MoE efficient, atomic editing tools — P1, medium

**Files:** `scripts/builder_tools.py`, both Builder prompt templates and loops, tool tests.

**Changes:**

- Add `read_file(path, start_line, end_line)`, literal search, directory listing, and bounded diff inspection.
- Add `replace_text`/patch with expected file hash and exact expected match count. Reject stale or ambiguous edits without mutation.
- Commit writes atomically. For large new files, stage chunks outside the active source path and explicitly finalize after validation; cancellation discards only uncommitted staging data.
- Normalize all tool results as success/error plus useful evidence. Record touched files only after confirmed mutation.
- Use structured command arguments for configured build/test tools. Arbitrary shell execution has full user authority today; `cwd` is not a sandbox. Use an isolated working copy for recovery, and add OS-level execution restrictions separately if commands must be prevented from accessing paths outside it.
- Bound displayed output while retaining full logs and allowing targeted retrieval; include failure tail as well as initial output.

**Acceptance:** A one-line edit in a large file does not regenerate that file. Ambiguous replacements, symlink escapes, stale hashes, and protected paths fail without damage. Interrupted multi-part writes leave the original source intact. Failed writes are not recorded as successful changes.

## 6. Make context selection and work units explicit — P1, medium/large

**Files:** `scripts/report_common.py`, `scripts/scout_report.py`, `scripts/planner_report.py`, `scripts/diagnose_report.py`, `scripts/builder_agent.py`, pipeline run artifacts.

**Changes:**

- Count the complete rendered request using the active runtime/tokenizer where available. When unavailable, use a conservative estimate, report that it is estimated, and refuse impossible budgets. Include plan/report/schema/test overhead plus an explicit reasoning/output reserve.
- Rank source by task relevance, changed paths, symbols, imports, and required tests. Supply numbered excerpts, hashes, and a coverage manifest. A whole-file size excess should trigger chunking rather than silent loss of the file.
- Let Scout ask for a bounded additional evidence selection through the runner, or deterministically investigate chunks and consolidate. Keep file writing runner-owned to avoid the earlier chat-extension write loop.
- Produce stable work-item IDs, dependencies, expected files/contracts, and verification criteria in the plan. Reject contradictory/missing mandatory decisions before editing.
- Run small plans in one context; checkpoint/reset on work-unit boundaries or context pressure. Carry decisions, interface contracts, remaining work, and verification references forward, not entire prior tool output.
- Preserve the validated model-specific reasoning-history policy and test it explicitly; native responses currently collect reasoning but do not put it back in assistant history. Do not assume this is either beneficial or harmful without comparison.

**Acceptance:** Small context settings never fall back to a huge budget. A relevant oversized file remains reviewable through excerpts. A multi-file task can resume after context reset without forgetting a prior interface decision. Total report size remains driven by relevant evidence, not a fixed line count.

## 7. Add a deterministic progress watchdog — P1, medium; depends on structured tools/events

**Files:** new runner watchdog module, `scripts/builder_agent.py`, `router/loop_guard.py`, `router/main.py`, event/UI adapters.

**Signals:** repeated normalized tool calls with unchanged results and source versions; repeated identical failure signatures; recurring short read/edit/error cycles; consecutive invalid/empty generations; context growth without new evidence or successful work. Keep prefill, generation, tool execution, and waiting-for-user states distinct.

**Behavior:** warn with evidence, permit one targeted recovery, then pause or produce `needs_repair` with a checkpoint if the same failure persists. Initial thresholds should be configurable and tested against captured failures; long elapsed time, a long report, or a single repeated read alone is insufficient. Known polling/wait commands require explicit allowances.

Repair the stream detector's fixed-window periodicity blind spot and inspect reasoning separately where available. Avoid treating repeated code inside a valid file payload as a reasoning loop. Keep alerts keyed by run/request and retained until acknowledged. A manual stop must propagate as cancellation, not a normal model stop.

**Acceptance:** Replay the repeated `dirty` repair and original report-writing loop: bounded detection with the offending calls/results visible. Healthy large writes, repeated tests after changed code, long prefill, and expected polling continue. No second model is involved in detection.

## 8. Strengthen diagnosis and focused dense review — P1/P2, medium

**Files:** `scripts/diagnose_report.py`, `scripts/auditor_report.py`, `scripts/renovator_agent.py`, shared evidence/verification code, project profile schema, pipeline policies.

**Changes:**

- Project profile declares target interpreter, cwd, verification commands, expected test collection, and execution deadlines. Resolve and quote paths correctly; exclude dependency/build directories during discovery.
- Accept last-known-good/known-bad revisions, a selected commit range, reproduction commands, and runtime logs. Include committed patches for a clean regression, not only commit titles.
- Give Auditor the original requirement, baseline-to-current diff, selected code/tests, Builder ledger, and real verification artifacts. Require each finding to cite its evidence and distinguish demonstrated cause from hypothesis.
- Represent coverage gaps explicitly; add bounded evidence acquisition or mark review incomplete. A whole-tree scan truncated by budget cannot certify the missing area.
- Give dense review/repair a compact packet with finding IDs, relevant before/after excerpts, failed checks, prior attempts, and allowed scope. Permit it to dismiss a contradicted finding with evidence; do not force an unsupported edit.
- Make dense verification selectable for high-risk contracts and repeated unresolved defects, even after MoE PASS. Keep repair attempts bounded and re-run checks after any repair.

**Acceptance:** A committed JS regression receives its introducing patch and a browser/module check. Wrong audit attribution is challenged rather than blindly implemented. An unrelated environment failure is distinguished from a code defect. Missing required context cannot yield an unconditional accepted run.

## 9. Unify configuration and validate the actual workload — P2, medium

**Files:** `modeldeck/state.py`, `modeldeck/mtplx.py`, `scripts/stack.sh`, CLI entrypoints, GUI/web settings, documentation, benchmark harness.

**Changes:**

- Replace duplicate shell defaults with delegation to the Python launcher/config. Add schema validation/migrations and expose stored versus effective settings. Preserve deliberate user choices.
- Compare resident model/runtime/config fingerprints before reusing a healthy port. Show stale router code and restart only through the idle/cancellation-aware owner.
- Support same-artifact residency reuse across compatible logical roles once correctness tests pass; keep each task's messages separate. Key reuse by the full serving contract, not just a model label.
- Keep cache experiments isolated. Shared exact-prefix reuse alone does not establish cross-task semantic contamination; verify model/template/prefix identity and output parity before attributing corruption to SSD caching or changing policy.
- Reassess memory using measured weights, live KV, retained sessions, allocator peaks, OS pressure, and swap. Session-bank caps are not total process-RAM caps. A larger configured context is not itself proof of extra prefill; actual tokens and allocation policy matter.
- Retain current Builder/Auditor precise-thinking sampling as the initial baseline. Compare Scout D1/D3, MoE thinking/instruct, and dense repair reasoning settings one variable at a time. Do not infer that dense is always more accurate or that a “Speed” label implies a faster successful task.
- Add reproducible benchmark fixtures: small feature, multi-file interface change, clean committed regression, UI/runtime bug, large-file edit, bad audit finding, and forced tool error. Fix baseline, runtime/artifact revisions, prompts, checks, and cache policy; retain all attempts and repeat runs.
- Report first-pass acceptance, final acceptance, repair scope, regressions, human interventions, total wall time, uncached prefill, decode/reasoning tokens, context peaks, model load/swap time, and memory. Report the MoE/dense fraction separately; never substitute model-estimated BUILDER_ACCURACY for acceptance tests.

**Acceptance:** Desktop, web, CLI, and compatibility shell launcher resolve the same role configuration. Benchmark results are attributable and repeatable. Choose defaults from median verified-task time and failure rate across the fixture set, with tail behavior visible.

## Release gate

Before calling the workflow reliable for unattended projects, demonstrate one feature run and one regression run end to end through the actual service, with real local models and independently checked acceptance criteria. Include at least one repair/re-audit, one cancelled tool process, and one checkpoint/resume. Confirm original dirty files survive and every result references the correct run, code state, and test evidence.

The immediate implementation slice is steps 1–2. They correct observable behavior without requiring a new framework, larger models, larger contexts, or a broad GUI redesign. Steps 3–8 make long tasks recoverable and reduce wasted model work. Step 9 measures whether the intended 85% MoE economics actually hold.
