# Local-model workflow review — 2026-09-10

## Assessment

Keep the MoE-first workflow. The project has useful foundations: model-specific serving settings, a dedicated regression-diagnosis path, explicit handoff documents, actual test execution before audit, and a bounded repair/re-audit cycle. The existing Python suite passed **196 tests with one skipped** during this review.

The scripts do not yet consistently set the models up for success. Several failures attributed to model quality can arise from contradictory injected instructions, a tool-mode setting that never reaches the agent, ambiguous completion handling, and repeated full-file context. Fix those contracts before drawing further conclusions from model comparisons.

The dense model should receive the original requirement, a focused diff, reproducible failures, and unresolved decisions. A long conversation or an unverified MoE diagnosis makes the dense phase expensive and can direct it toward the wrong repair. “85% MoE” is a useful operating target, not a demonstrated property or a reason to stop a correct implementation early.

## Scope and evidence

- Reviewed working tree at HEAD `b13429c`, including existing uncommitted changes to README, web UI/API, and router tests. Those changes were preserved.
- Read launch/configuration code, both agent protocols, all report scripts, router guards, shared pipeline, desktop/web integration, tests, current `.ai` reports, and tuning/incident documentation.
- Inspected the saved role configuration separately from defaults. Queried local health without submitting inference requests; only Builder answered among the local role ports checked, and it reported zero active requests at that moment.
- Ran the test suite with `MODEL_DECK_CONFIG_DIR` pointed at a temporary directory and Qt offscreen, so tests did not use the operator's saved configuration.
- Used isolated mocks and a short disposable subprocess to reproduce boundary failures. No paid API requests, model swaps, or target-code edits were performed.
- Existing reports are historical, model-generated evidence, not authoritative descriptions of the present source. For example, the root Builder report is empty while a later audit claims PASS/100; without a run identity these cannot establish which artifacts belong together.

## Actual model allocation

This table describes **saved configuration**, not just factory defaults. All contexts are token limits.

| Phase | Saved model | Context | Serving | Reasoning / intended protocol |
| --- | --- | ---: | --- | --- |
| Scout | Qwen3.6 35B-A3B Balance | 256,000 | sustained, D3, KV off | general thinking; one-shot report |
| Planner | Qwen3.8 27B Quality, local | 131,072 | turbo, D3, Q8 | off; one-shot plan |
| Builder | Qwen3.6 35B-A3B Balance | 131,072 | sustained, D1, KV off | precise coding thinking; native tools configured |
| Auditor / Diagnose | Qwen3.6 35B-A3B Balance | 108,544 | sustained, D1, KV off | precise coding thinking; one-shot report |
| Renovator | Qwen3.8 27B Quality | 131,072 | turbo, D3, Q8 | off; native tools configured |

Builder/Auditor's coding sampling matches Qwen's published precise-thinking preset: temperature 0.6, top-p 0.95, top-k 20, presence penalty 0.0. This is a sound starting point, not proof of best end-to-end performance for the optimized artifact. [Qwen model card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)

The dense artifact's publisher documents a turbo/D3 path and distinguishes its M5 measurements from unreported M1/M2 throughput. Use this project's hardware-specific measurements rather than treating model-card speeds as promises. [Quality FP16 artifact card](https://huggingface.co/Youssofal/Qwen3.8-27B-MTPLX-Optimized-Quality-FP16)

`docs/DEPTH_TUNING.md` reports D1 winning for the tested MoE artifacts and D3 for dense artifacts on a particular tuning suite. Scout still uses D3. Benchmark D1 against that Scout workload; do not silently replace the user's saved settings. The much larger Scout context versus Auditor also means the two stages can see materially different evidence.

## Prioritized findings

### R1 — P0: Standalone scripts receive conflicting chat-agent instructions

**Evidence:** `scripts/report_common.py:677` and `:769` send `X-Model-Deck-Skip-Injection`. `router/main.py:259` reads `x-modeldeck-skip-injection`. HTTP header case normalization does not remove a hyphen. A real Starlette Request carrying the script's header returned `False` from `wants_injection_skipped()`.

**Effect:** Scout/Planner/Auditor can receive instructions to use an editor and maintain a report incrementally even though their own prompts say they have no tools. Builder also gets extra chat-workflow requirements. This directly undermines the standalone-script architecture. The Admin description that these overrides do not affect scripts is currently incorrect because of this bug.

**Recommendation:** One shared header constant, a compatibility alias for the old spelling, and integration tests that inspect the exact upstream request produced by each script path. Keep separate prompt content for tool-free reports and agentic editing; make both inspectable in the GUI.

### R2 — P0: Native tool calling is configured on the server but omitted from the client

**Evidence:** `modeldeck/mtplx.py:150` applies native serving flags, but `modeldeck/pipeline.py:283` only forwards the role's step budget to the script. Both agent CLIs default `--native-tool-calling` to false. A mocked pipeline invocation with `native_tool_calling=True` omitted the argument. The last captured Builder request also had `tool_count: 0`.

**Effect:** Pipeline runs do not exercise the protocol selected in the GUI or the native-mode benchmark results cited in the defaults.

**Recommendation:** Resolve one immutable run configuration and use it for both model launch and agent invocation. Test both Builder and Renovator with native enabled and disabled, all the way to the HTTP payload. Record requested and effective protocol.

### R3 — P0: A completed response or process is confused with completed work

**Evidence:** `scripts/builder_agent.py:326` and `:421` accept any nonempty response without a tool call as completion. The native loop ignores its returned `finish_reason`. Both loops return ordinary report text on step exhaustion (`:358`, `:493`), and the CLIs save it and exit zero. The pipeline treats exit zero plus a readable `Wrote ...` path as success. `stream_chat()` discards finish reason and stream-error metadata; Scout/Planner/Auditor do not apply Diagnose's nonempty-report check.

**Reproductions:** “I will start by inspecting the files” was accepted as a final report with zero actions; native text with `finish_reason='length'` was accepted as completion.

**Recommendation:** Typed outcomes such as `completed`, `blocked`, `needs_repair`, `budget_exhausted`, `cancelled`, `protocol_error`, and `backend_error`. Keep partial reports, but do not publish them as completed artifacts. Require an explicit completion action and runner-owned verification evidence. Handle interrupted/error SSE streams explicitly.

### R4 — P0: Audit acceptance is not enforced by deterministic verification

**Evidence:** `scripts/auditor_report.py:191` runs tests, then places the output inside the prompt. It saves whatever verdict the model returns. `modeldeck/pipeline.py:447` trusts `parse_verdict(text)`. A mocked failed test result followed by model text `VERDICT: PASS` still exited zero and saved PASS. `parse_verdict()` also accepts any value beginning with PASS, rather than an exact enum.

**Effect:** A model can overrule a real failing test, or an incomplete audit can authorize completion. UNKNOWN also automatically sends work to Renovator even when there is no valid repair list.

**Recommendation:** A verification result with command, resolved cwd/interpreter, exit code, timeout, test counts where available, and log path. Effective acceptance must require the mandatory checks to pass plus a valid review result and sufficient coverage. Distinguish infrastructure failure from code failure and malformed audit from actionable rejection. Never send a fabricated repair scope downstream.

### R5 — P1: The editing tools encourage expensive and fragile whole-file regeneration

**Evidence:** `scripts/builder_tools.py:269` offers full-file read/write and append; there is no bounded read, search, diff, or anchored edit. Both Builder prompts explicitly recommend replacing a large file with its first part and appending the rest later. `write_file` immediately overwrites the live file.

**Effect:** A one-line fix can require thousands of generated tokens. Cancellation between chunks leaves a partial file. Repeated reads inflate every later request. Recent `.run/builder.log:4351` onward records the same attempted `dirty` declaration repair while prompts rise from 52,108 to 122,825 tokens; this establishes repetition/context growth, though the preview alone cannot prove every executed action.

**Recommendation:** Add line-range reads, literal search, bounded diff, and exact anchored replacement with an expected content hash and match count. Apply edits atomically; stage large new files until complete. Preserve full-file reads when needed. Return structured tool success/error results and only log successful mutations as touched files.

### R6 — P1: Context handling hides omissions and lets long tasks grow without a checkpoint

**Evidence:** `scripts/report_common.py:319` admits or drops whole files against a character estimate; it adds no line numbers. The estimate does not account exactly for report/task/test/schema overhead. At an 8,192-token configured window, `char_budget_for_role()` falls back to **360,000 characters**. Builder retains every read and generated file body for the entire plan. Diagnose can admit up to 2.4 million characters of changed files before adding other evidence.

**Effect:** Larger caps defer failure without solving repeated work. Critical large files can be absent while smaller files fit. The prompt assertion “every file you need is already given” is stronger than the collector can guarantee.

**Recommendation:** Token-aware admission for the complete request, output/reasoning reserve, explicit coverage records, relevant numbered excerpts, and a bounded evidence-request mechanism. Split large implementations into dependency-aware work units with a durable progress ledger. Do not cap the final report at an arbitrary line count; let coverage determine length and store large evidence separately.

### R7 — P1: Auditor and Renovator lack a reliable account of this run's changes

**Evidence:** Auditor reads the current tree and plan, not a baseline diff (`scripts/auditor_report.py:155`). Touched files are inferred only from write/append tool calls; shell edits and deletions are invisible. Failed writes can still be added to the touched set. No-touch runs leave the old touched-files artifact, and Renovator replaces rather than unions it. Reports are archived by time but not bound to a task, baseline, or input hashes (`write_report`, `read_required_artifact`).

**Effect:** The Auditor cannot reliably distinguish pre-existing code from out-of-scope changes or prove that its plan/report belongs to the current task. A stale handoff can look valid because it exists and is nonempty.

**Recommendation:** Per-run manifests and immutable artifacts, a baseline snapshot including pre-existing dirty/untracked files, and authoritative before/after diffs. Give Auditor the original user requirement as well as the plan. Give Renovator structured findings with failing evidence and authority to dispute a contradicted diagnosis. Current “do not revisit” repair wording is too rigid when the audit is wrong.

### R8 — P1: The loop detector misses the failures that matter most

**Evidence:** `router/loop_guard.py` watches exact repetition inside one response; `router/main.py:281` feeds only content deltas. Reasoning, native tool arguments/results, repeated calls across turns, and repeated errors are outside its view. Its 400-character tail requires the period to divide the whole window. A unique prefix followed by a 13-character phrase repeated 100 times was never detected; a 10-character phrase was detected. Stream stop emits ordinary `finish_reason='stop'`.

**Recommendation:** Keep an advisory streaming detector, but add the main watchdog in the agent runner: normalized tool-call/result hashes, target content versions, repeated failure signatures, repeated read/edit cycles, and progress checkpoints. Identify a pause as `stalled` or `cancelled`, never successful completion. Suppress expected polling and legitimate repeated code structures. Log the triggering evidence and allow an explicit resume.

### R9 — P1: Desktop and web do not share execution ownership

**Evidence:** `modeldeck/gui.py:587` constructs a Pipeline; `router/main.py:92` constructs another. The desktop event sink calls `on_phase_finished`, but the web sink merely broadcasts (`router/web.py:145`). Web Continue only fetches status and changes buttons (`router/static/app.js:124`); it never advances the queue. Web Start does not forward the troubleshoot flow. The SSE async generator calls blocking `queue.get(timeout=30)` (`router/web.py:264`).

**Effect:** A web full run has no wired completion transition after its first phase; desktop and web can independently stop/load models and start work against shared files. The SSE wait can block the same event loop serving the inference gateway, contributing to stalls.

**Recommendation:** One service owns the run state machine, model residency, queue, and transition logic. Desktop/web become clients. Complete/continue transitions belong to the service regardless of subscribers. Use asynchronous event delivery and replayable run events. Add concurrency rejection and explicit flow validation.

### R10 — P1: Stop/question behavior has gaps during long operations

**Evidence:** `pipeline.py:322` reads stdout in blocks of 64 characters; a short question can remain buffered while the child waits for stdin. A disposable child reproduced this. Stop returns without cancellation if the phase is still loading and no report child exists (`:410`). It terminates only the report process, not necessarily shell/test descendants. Desktop event handling also writes Qt widgets from the worker event sink. Dismissing a multiple-choice question chooses the first option (`gui.py:1436`).

**Recommendation:** Structured event messages over a dedicated channel, cancellation spanning loading/generation/tools/questions, process-group cleanup, explicit cancelled answers, and GUI updates through queued signals only. A dismissed question must not become an affirmative choice.

### R11 — P1/P2: Configuration has multiple sources and weak effective-state checks

**Evidence:** `scripts/stack.sh` still assigns Builder the dense Speed model, turbo/D3 to Scout, 100K contexts, and SSD cache on. Python configuration assigns Builder the MoE and SSD off. The `local` alias does not inherit role sampling, unlike `builder` (mocked temperature: None versus 0.6). Health reuse checks readiness but not model identity/configuration (`mtplx.py:191`). `ensure_router()` cannot detect a stale process. Default merge preserves previously saved values and has no migrations beyond schema_version 1.

**Recommendation:** One validated model/role configuration and launcher. Make the shell script delegate to it. Report effective model ID, runtime version, profile, context, KV, parser, sampling, and prompt hash. Detect incompatible saved settings and stale router code. Gate model-specific presets by capabilities and benchmark evidence; do not guess non-Qwen parsers from the presence of a native-tool flag.

### R12 — P1/P2: Diagnosis needs a reproducible symptom and a trustworthy test environment

**Evidence:** `git_repro_context()` provides commit titles but only uncommitted diffs; clean committed regressions receive no commit patch. Diagnosis assumes the latest change is the main suspect. Test detection handles Swift/Python but can find tests inside dependency directories, uses a shell command with an unquoted/non-resolved interpreter path, and truncates output to its first 8K characters. A relative target with its own venv can produce a doubly relative executable after cwd changes. The full suite may not exercise the affected browser behavior at all.

**Recommendation:** A project execution profile specifying interpreter, commands, cwd, test timeout, and environment. Preserve complete logs, show head/tail and failure summaries. Accept last-known-good revision, selected commit range, runtime logs, and reproduction steps. Require the causal mechanism to be demonstrated or labeled unresolved. Add browser/module smoke tests when a task changes JS runtime behavior; use test adapters appropriate to the target language.

## Recommendations for the intended MoE/dense split

1. **MoE performs broad investigation and implementation.** Start with the current Balance Builder and precise-thinking preset after fixing R1/R2. Compare Scout D1/D3 on actual repository prompts. A model's total parameter count or sparsity does not prove role suitability; use observed acceptance and task results.
2. **Planning can remain dense for consequential design work.** It is a relatively small, high-impact input once evidence selection works. For small, already-specified changes, allow the MoE to produce the plan; escalate ambiguity rather than forcing a dense pass for every task. Preserve an explicitly selected cloud planner as an option, not an automatic fallback for a local-only run.
3. **Run deterministic checks before asking for model review.** Separate execution failures from source defects. An environment repair must not consume a model's whole coding budget.
4. **Use MoE for broad first review, dense for focused verification/repair.** Builder and Auditor currently use the same artifact. Fresh context removes conversational anchoring but not shared blind spots. Route risky invariants, unresolved failures, and disputed audit findings to a dense verification pass even if the MoE says PASS. The dense model should first validate the finding, then edit only when the evidence supports it.
5. **Make escalation explicit and evidence-based.** An unchanged failure after bounded attempts, failed protocol recovery, context pressure without progress, or a substantive unresolved requirement should produce a small handoff packet. Malformed reports, process errors, and missing dependencies require different handling from “model could not fix the code.”
6. **Measure useful completion, not the Auditor's estimated percentage.** Track acceptance tests passed, first-pass completion, repairs required, regressions, human interventions, wall time, uncached prefill, decode/reasoning tokens, swap time, and peak memory. Keep BUILDER_ACCURACY labeled as a model opinion. Preserve baselines, revisions, prompts, and all benchmark attempts.

There is no guaranteed equivalence between “85% of generated tokens,” “85% of accepted requirements,” and “85% of time on MoE.” At half the MoE's throughput, a dense model producing 15% of tokens consumes about 26% of decode time even before prefill and swaps. Optimize total verified-task time and dense repair scope; use the percentage as a reported metric.

## Implementation order

Follow [WORKFLOW_IMPLEMENTATION_PLAN_2026-09-10.md](WORKFLOW_IMPLEMENTATION_PLAN_2026-09-10.md). Fix protocol and acceptance contracts first, establish run evidence and execution ownership next, then improve edits/context/watchdogs. Retune models and benchmark the full workflow only after those changes are in place.

Changes in this review are documentation only. The suite and mocks validate current behavior; no live end-to-end model benchmark was run. The historical benchmark claims in `IMPROVEMENTS_TODO.md` were inspected but not independently reproduced.
