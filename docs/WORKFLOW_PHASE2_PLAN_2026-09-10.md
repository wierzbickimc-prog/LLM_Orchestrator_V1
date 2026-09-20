# Implementation plan, phase 2: the unbuilt decisions

Date: 2026-09-10. Follows [the workflow implementation plan](WORKFLOW_IMPLEMENTATION_PLAN_2026-09-10.md), whose steps 1-9 are now complete. Companion evidence: [the workflow review](WORKFLOW_REVIEW_2026-09-10.md).

## What this covers and why it is separate

Phase 1 built the evidence and safety substrate the proposed workflow assumes: typed outcomes, run baselines, derived change sets, runner-owned verification, atomic anchored edits, honest context budgets, a progress watchdog, and one resolved configuration. What it deliberately did not build is the part of the diagram that *decides* anything.

Four boxes remain unbuilt. They share a property worth naming up front: each replaces a guess with a declaration.

| Box | Today | After |
| --- | --- | --- |
| Project execution profile | Verification commands are detected heuristically | The project declares them and the run is held to them |
| Plan routing by consequence | The Planner is statically dense for every task | Evidence decides, and the decision is recorded |
| Dependency-aware work units | The Builder gets one prose plan and an accumulating transcript | Ordered units with contracts, a durable ledger, and resumability |
| Focused dense verification | A finding is prose; a dispute is more prose | Findings are records with dispositions, and a disputed one is adjudicated |

Order matters. Item 1 makes verification deterministic, which items 3 and 4 both depend on. Item 4 is next because it is where model effort is currently wasted on findings that may be wrong. Item 3 is the largest and benefits from both. Item 2 is last, because routing is an optimization and cannot be evaluated honestly until the rest is measurable.

Item 5 is a carried-over refinement to phase 1's residency check rather than one of the four boxes. It is independent of the others and can be done at any point.

Two constraints bind every design below. **Model residency is sequential** on this machine, so any new phase costs a model swap and must earn it; batch work to one swap where possible. **No new model is introduced to supervise another** -- routing and adjudication are either deterministic or done by a phase that already exists.

---

## 1. Project execution profile -- small/medium, do first

**Files:** new `modeldeck/profile.py`; `scripts/report_common.py`; `scripts/auditor_report.py`; `scripts/diagnose_report.py`; `scripts/builder_tools.py`; `modeldeck/runs.py`; GUI/web settings; tests.

### The problem

`detect_test_command()` guesses. It is now a careful guess -- it resolves and quotes the interpreter and skips dependency directories -- but a guess is still the wrong shape for the thing a run's acceptance depends on. Three failures follow from it. A project whose tests need an environment variable, a build step, or a non-default runner gets no verification at all, and "no test command detected" reads as an absence of risk rather than an absence of evidence. A project with more than one meaningful check gets only the first. And nothing distinguishes a check that *must* pass from one that is informational.

### The change

A profile is a declared file at `<target>/.ai/project-profile.json`, resolved in this order: an explicit `--profile` path, then that file, then detection, then none.

```json
{
  "schema_version": 1,
  "language": "python",
  "interpreter": ".venv/bin/python",
  "cwd": ".",
  "env": {"MODEL_DECK_CONFIG_DIR": "/tmp/md-test"},
  "exclude_dirs": ["vendor"],
  "checks": [
    {"name": "unit", "command": "{interpreter} -m pytest -q",
     "required": true, "timeout": 600, "expect_min_tests": 50},
    {"name": "typecheck", "command": "{interpreter} -m mypy .",
     "required": false, "timeout": 300}
  ],
  "smoke": [
    {"name": "import", "command": "{interpreter} -c 'import modeldeck'",
     "required": true, "timeout": 60}
  ]
}
```

- `{interpreter}` and `{cwd}` interpolate from the profile, resolved absolute, shell-quoted. No other interpolation, so a profile cannot smuggle in arbitrary substitution.
- `required` drives acceptance. `expect_min_tests` catches the collection failure that reports a green exit code having run nothing.
- `smoke` runs when a change touches a language whose full suite would not exercise it -- the browser/module case the review raised. Selection is by changed-file extension, taken from the run's derived change set.
- Detection is preserved and gains `--write-profile`, which emits the detected profile for the operator to edit rather than re-deriving it silently on every run.
- The profile's hash goes in the run manifest, so a result can prove which contract it was judged against.

### Behavioral consequences

A missing interpreter, a failed launch, or a timeout is an **environment failure**, reported distinctly from a code defect and never as a passing suite. A required check that did not run blocks acceptance the same way a failing one does. Both already have the plumbing: `VerificationResult` carries a status, and `enforce_audit_gate` is runner-owned.

### Acceptance

- A project with a profile runs exactly its declared checks, in order, with the declared environment and deadlines.
- A profile naming a missing interpreter yields an environment failure, not a code rejection, and not a pass.
- A required check that collects zero tests fails acceptance despite exit code zero.
- A change touching only JavaScript triggers the declared smoke check.
- `--write-profile` produces a valid profile for this repository that reproduces its current suite.
- The manifest records the profile hash; two runs under different profiles are distinguishable.

---

## 2. Plan routing by consequence -- medium, do last

**Files:** new `modeldeck/routing.py`; `modeldeck/state.py`; `modeldeck/pipeline.py`; `scripts/scout_report.py`; `scripts/diagnose_report.py`; `scripts/planner_report.py`; GUI/web; tests.

### The problem

The Planner is the dense model for every task, including a one-line change with an unambiguous plan. That is the most expensive model in the workflow doing its least differentiated work. The inverse also happens: a genuinely consequential design decision gets the same single dense pass as a rename, with no signal that it deserved more care.

### The change

Routing is **deterministic and evidence-derived**, not a model's opinion about its own difficulty. `classify_plan_consequence()` returns a decision, the signals behind it, and a human-readable reason.

Signals, in rough order of weight:

| Signal | Source | Direction |
| --- | --- | --- |
| Unresolved decisions declared by Scout/Diagnose | new structured field in those reports | dense |
| Changed-surface breadth (files, modules) | scout evidence, git changed set | dense above threshold |
| Touches persistence, schema, protocol, or auth paths | path and content patterns from the profile | dense |
| Introduces a new public interface | plan-adjacent heuristics on scout evidence | dense |
| Re-plan after a rejected audit or a stalled build | run journal | dense |
| Coverage was incomplete for the relevant area | phase 1 coverage manifest | dense |
| None of the above, and the task is scoped to named files | default | MoE |

This requires one small upstream addition: Scout and Diagnose gain an explicit `## Open questions` section with a structured list, which the phase-1 structural validator already has a place to enforce. That section is independently useful, and it is the highest-signal input to routing.

Mechanically, a `planner_moe` role is added. Sequential residency means routing to it swaps the model, which the phase-1 fingerprint check now handles correctly rather than silently reusing whatever is resident.

**Escalation, bounded.** If the MoE planner's output fails structural validation, or declares an unresolved mandatory decision, the run escalates to the dense planner exactly once and records why. A dense plan is never demoted.

**Operator override wins.** A per-run and a global setting force one class regardless of signals, because the operator has context the signals do not.

### Deliberate non-goals

No confidence score, and no model asked to rate its own difficulty. Both invite exactly the miscalibration that made `BUILDER_ACCURACY` a model opinion rather than a measurement.

### Acceptance

- A scoped single-file task with no open questions routes to MoE; the decision and its reasons appear in the manifest and the event journal.
- A task whose Scout report declares an unresolved decision routes dense.
- A task touching a persistence schema routes dense even when it is small.
- An MoE plan that fails structural validation escalates once, and only once.
- An operator override beats every signal.
- Routing is a pure function of recorded inputs: the same inputs always yield the same decision, and it is testable without a model.

---

## 3. Dependency-aware work units and a durable ledger -- large

**Files:** `scripts/planner_report.py`; `scripts/builder_agent.py`; new `modeldeck/work_items.py`; `modeldeck/runs.py`; `scripts/auditor_report.py`; tests.

### The problem

The Builder receives one prose plan and accumulates every read and every generated file body for the whole run. Three costs follow. The context grows monotonically whether or not the retained material is still relevant, which is what drove the observed climb from 52,108 to 122,825 prompt tokens. A run killed at 80% loses all of it, because nothing durable records which parts were finished. And the Auditor cannot tell which requirements were attempted from which landed.

### The change

The Planner emits `work-items.json` beside its prose plan. The prose stays; humans read it and it remains the model's reasoning surface.

```json
{
  "schema_version": 1,
  "items": [
    {"id": "W1", "title": "Add the profile schema and loader",
     "files": ["modeldeck/profile.py"], "depends_on": [],
     "establishes": ["ProjectProfile.load() returns a validated profile"],
     "verification": [{"check": "unit", "selector": "tests/test_profile.py"}]},
    {"id": "W2", "title": "Use the profile in the auditor",
     "files": ["scripts/auditor_report.py"], "depends_on": ["W1"],
     "consumes": ["ProjectProfile.load() returns a validated profile"],
     "verification": [{"check": "unit", "selector": "tests/test_auditor_report.py"}]}
  ]
}
```

**Validation before any edit.** The graph must be acyclic, every `depends_on` must resolve, every `consumes` must be `establishes`ed by a dependency, every item must name at least one verification, and every path must lie within the target. A plan failing any of these is a `protocol_error` at the Planner boundary, not a Builder failure -- which is where it currently surfaces, misattributed.

**Execution.** The Builder walks the graph in topological order, one unit at a time. Per unit, its context is: the unit, the `establishes` strings of its transitive dependencies, the files the unit names, and the verification result of the last unit. It is explicitly *not* the accumulated tool output of prior units. That is the checkpoint-and-reset mechanism step 6 called for, and the `establishes` contract is what makes it safe -- an interface decision survives as a stated contract rather than as a transcript nobody can afford to keep.

**The ledger.** `builder-progress.json` in the run directory, updated after every unit:

```json
{"W1": {"status": "verified", "changed": ["modeldeck/profile.py"],
         "verification": "verification/unit-W1.json", "attempts": 1},
 "W2": {"status": "in_progress", "attempts": 2}}
```

A unit reaches `verified` only when its declared verification actually ran and passed, recorded by the runner. `completed` is not a status the model can assert.

**Resume.** On restart with the same run id, verified units are skipped, the first non-verified unit resumes, and the ledger plus the baseline together reconstruct what happened. The phase-1 run directory and derived change set already provide the durable half of this.

**Audit.** The Auditor receives the ledger and checks each requirement's disposition against the diff, rather than inferring which parts were attempted.

### Risks

Over-decomposition is the real one: a plan split into thirty trivial units spends more on orchestration than on work, and inter-unit context resets cost prefill. Mitigate with a floor on unit size in the Planner prompt, and by letting the Builder run several units in one context when their combined file set is small. The reset is a tool for context pressure, not a ritual per unit.

### Acceptance

- A multi-file task killed mid-run resumes without redoing verified units.
- A cyclic or dangling-dependency plan is rejected before any file is edited, attributed to the Planner.
- An interface decision established in the first unit is present in a later unit's context without that unit's tool output.
- A unit whose verification never ran cannot reach `verified`.
- Prompt size does not grow monotonically across units on a multi-unit run.
- A small plan still runs in one context, with no unnecessary resets.

---

## 4. Focused dense verification for disputed and high-risk findings -- medium

**Files:** `scripts/auditor_report.py`; new `scripts/verifier_report.py`; `scripts/renovator_agent.py`; `modeldeck/pipeline.py`; `modeldeck/runs.py`; `modeldeck/state.py`; tests.

### The problem

This is the one branch of the diagram with nothing behind it. Findings live only as prose inside a Markdown report, so nothing can be routed, counted, or resolved. The Renovator may now dispute a finding, but a dispute is more prose in a report that no phase reads as a decision. The failure mode this permits is expensive and quiet: a wrong finding gets implemented, correct code is edited to satisfy it, and the re-audit passes because the code now matches the mistaken claim.

### The change

**Findings become records.** The Auditor emits `findings-N.json` beside `audit-N.md`:

```json
{"id": "F1", "severity": "high", "category": "correctness",
 "file": "router/web.py", "lines": [264, 271],
 "claim": "The SSE generator blocks the event loop for up to 30 seconds",
 "evidence": "queue.get(timeout=30) inside an async generator",
 "demonstrated": false,
 "fix": "Await the queue in a worker thread"}
```

`demonstrated` is the key field: a finding backed by a failed check that cites it is demonstrated; one derived by reading is a hypothesis. The phase-1 structural validator extends naturally -- a REJECT must carry at least one parseable finding, which is a stronger version of the Fix List check already in place.

**A finding is routed to dense verification when** it is high severity and not demonstrated, or its category is in the risky set (concurrency, persistence, auth, protocol), or the Renovator disputed it.

**The verifier is a new dense, read-only phase.** Its packet is small by construction: the finding records, the cited before/after excerpts from the run diff, the relevant failed check output, prior attempts, and the allowed scope. Not the tree. Per finding it returns `confirmed`, `refuted`, or `needs_more_evidence`, each with evidence. It never edits, which is what lets it be trusted to contradict an audit.

**One swap, not one per finding.** All findings needing verification go in a single batched pass. On sequential residency this is the difference between one model swap per audit and one per finding.

**The loop closes.** Refuted findings are dropped from the repair scope and recorded with the evidence that refuted them. Confirmed findings become the Renovator's scope. A Renovator dispute writes `disputes-N.json`, which reopens exactly one verification for that finding -- never a second. Existing retry bounds still cap the whole cycle.

### Acceptance

- A finding contradicted by evidence is refuted and never reaches the Renovator; the refutation and its evidence are recorded.
- A confirmed finding reaches the Renovator with its evidence attached.
- A dispute reopens exactly one verification for that finding and no more.
- Several findings needing dense verification cost one model swap, not several.
- A REJECT with no parseable finding is a protocol error, and no repair scope is fabricated from it.
- Every finding ends the run with a recorded disposition. None ends as prose alone.
- The verifier cannot edit files; this is enforced, not merely instructed.

---

## 5. Derive the residency fingerprint from the live server -- small, independent

**Files:** `modeldeck/mtplx.py`; `modeldeck/launch.py`; tests.

### The problem

Phase 1 stopped a healthy port from being reused on readiness alone: reuse now compares a serving fingerprint, so a server left over from a different window, profile, or artifact is no longer silently adopted. But the fingerprint it compares against is a file Model Deck wrote at launch, `.run/<phase>.fingerprint`. That has three consequences.

A server Model Deck did not start has no fingerprint file, so it is unverifiable by construction and must be stopped and relaunched even when it is already correct. That was observed immediately: after phase 1 landed, the resident Builder was serving precisely the saved role -- the same artifact, sustained profile, depth 1, a 131,072-token window, KV quantization off, the SSD cache disabled, and the qwen3 reasoning parser -- and would still have been torn down and reloaded, at the cost of a full 35B model load, because no file recorded it.

The file can also go stale in the other direction. A `.run` directory that is cleared loses the record while the server keeps running, and a file that survives a server the operator restarted by hand now asserts something untrue. In both cases the file describes what Model Deck *intended* at launch, not what is actually resident, which is the same category of claim the phase 1 review objected to elsewhere.

### The change

Ask the server. mtplx's `/health` already reports the fields that matter, and comparing them directly makes the check independent of who started the process and of anything on local disk.

Eight of the ten fingerprint keys are directly available today:

| Fingerprint key | Health field |
| --- | --- |
| `model` | `model_path` (basename, `--` to `/`) |
| `profile` | `runtime_mode` (for example "Sustained MTP") |
| `depth` | `depth` |
| `context_window` | `context_window` |
| `kv_quantization` | `paged_kv_quantization` |
| `reasoning` | `reasoning` |
| `native_tool_calling` | `reasoning_parser` |
| `ssd_session_cache` | `ssd_session_cache.enabled` |

`reasoning_effort` and `preserve_thinking` are not reported. Treat them as a documented, named gap rather than pretending to a full match: compare what the server actually exposes, record which keys were verified and which could not be, and keep the launch-written file as a secondary source for the unreported two. A partial verification that says so is worth more than a total one that is assumed, and this is exactly the distinction the gap exists to preserve.

Keep the file. It stays useful as corroboration and as a fallback when the server is reachable but does not report a field. What changes is precedence: the live server is authoritative, the file is a hint.

Health-field names are mtplx-specific, so put the mapping in one place, behind a function that returns "matches", "differs on these keys", or "cannot determine". A backend that reports nothing usable degrades to today's behaviour rather than failing.

### Acceptance

- A correctly configured server that Model Deck did not start is reused, with no reload, and the run records which keys were verified.
- A server differing on any comparable key is not reused, and the differing keys are named in the message.
- A server that reports nothing usable falls back to the file, and to relaunching when there is no file.
- The two unreported keys are never claimed as verified.
- A cleared `.run` directory does not by itself force a reload of a matching resident server.
- The health-to-fingerprint mapping lives in one function and is unit tested against a recorded health payload, with no live server needed.

## Sequencing and measurement

Recommended order: **1, 4, 3, 2.** Item 1 unblocks deterministic verification for the other three. Item 4 stops effort being spent on possibly-wrong findings and is self-contained. Item 3 is the largest and wants both in place. Item 2 is last because routing is an optimization whose value cannot be judged until the workflow is measurable.

Each item is a separate work unit with its own commits and its own acceptance criteria. None should be fed to one accumulating Builder session -- which is, not incidentally, the thing item 3 exists to fix.

Measure against the metrics phase 1's review already named: first-pass acceptance, final acceptance, repair scope, regressions, human interventions, wall time, uncached prefill, decode tokens, context peaks, and swap time. Two additions specific to this phase. For item 4, **the refutation rate** -- what share of findings sent to dense verification are refuted -- because a rate near zero means the routing is too eager and a high rate means the audit needs attention rather than the verifier. For item 3, **prompt tokens per completed work unit**, which is the number the context-growth problem actually shows up in.

The release gate from phase 1 still stands, and now has more to demonstrate: one feature run and one regression run end to end through the real service with real local models, including at least one repair, one refuted finding, one cancelled tool process, and one checkpoint/resume that skips verified work units.
