# Improvements to-do

Running list of things noticed during real pipeline runs, captured here
instead of getting lost in conversation. Add an entry the moment something
is spotted, even mid-run -- don't wait until a run finishes to write it
down. Mark it Done with a one-line note on what actually changed once it
ships; don't delete finished entries, they're a record of why something is
the way it is.

## Open

- **A test suite that writes to the operator's real config.** Found by
  reading a failing run's audit report, not by any test failing:
  `tests/test_web.py` patched `modeldeck.state.load_state`/`save_state`,
  but `router/web.py` imports both *by value* at module load, so the
  patches applied to nothing and the endpoint tests drove the real
  `~/Library/.../state.json`. A fixture's throwaway role dict silently
  rewrote scout's `context_window` from 131072 to 8192, and the only
  symptom would have been "scout got mysteriously worse". Fixed two ways
  (correct patch targets, plus an autouse fixture pinning
  `MODEL_DECK_CONFIG_DIR` at a tmp_path), but the general rule is worth
  keeping: any test that can reach `load_state()` must be sandboxed by
  environment, not by trusting a mock to be aimed correctly. Audit whether
  other test modules touching state are similarly exposed.

- **Renovator ran out of turns on a 4-item fix list (40-step cap).** The
  remote-access build's repair pass stopped at "Stopped after 40 steps
  without the model signaling completion" -- a safety cap, not a crash.
  Three of the four items were small test edits. The turn budget is now
  visible and editable in the Deck tab (`max_steps` per role), and the
  turn counter is live on the phase label during a run, so this failure
  mode is at least legible now. Still open: whether 40 is simply too low
  for a fix list that requires re-reading large test files, or whether the
  turns were spent chasing the audit's one *misdiagnosed* item (see below).

- **The Auditor can hand the Renovator a confidently wrong fix.** In the
  remote-access run, the audit attributed 14 of 16 test failures to wrong
  `@patch` namespaces. The patch targets were genuinely wrong -- but they
  were not why those tests failed: every one returned 403 because
  `TestClient`'s default client host is the literal string `"testclient"`,
  which the new Tailnet allowlist correctly rejects. A repair pass working
  that fix list would change the patch targets, re-run, and see all 14
  still failing, with no guidance about why. Worth considering: when the
  audit has real test output, require each fix-list item to quote the
  specific failure line it explains, so an item that explains nothing is
  visible as such.


- **The router process doesn't restart when the GUI does, and nothing
  flags that it's running stale code.** Hit live: added a new "renovator"
  router alias, restarted the GUI (which relaunches models but not the
  router itself -- it's a separate long-lived process, `router.main`,
  started once by `ProcessManager.ensure_router()` and left running
  across GUI restarts unless explicitly killed), and got a 404 "Unknown
  model alias: renovator" for a script that had been working seconds
  earlier in a different context. The router had been running since the
  previous day, entirely unaware any code had changed. Worth either: (a)
  a version/build marker in `/health` the GUI can compare against the
  code on disk and warn about, or (b) just always restarting the router
  alongside the GUI (matching the model relaunch behavior) rather than
  treating it as a separate lifecycle. Low cost either way, and this class
  of "silently stale long-lived process" bug is easy to lose an hour to.

- **Chunk Builder's work per plan-step instead of one long accumulating
  session.** Right now `builder_agent.py` runs the *entire* plan as one
  conversation: every file read, every write_file/tool result, stays in
  context for the rest of the run, so context grows monotonically and
  nothing is ever paged out. On a real multi-file task this hit 95K/104K
  prompt tokens by step 25 (see the 128K context_window bump below, a
  mitigation, not a fix for this). A task-chunked design would run one
  Builder invocation per plan step (or per logical group of steps) with a
  *fresh* context each time -- fed the specific step's requirements plus a
  short on-disk progress ledger (e.g. `.ai/builder-progress.md`, a running
  checklist, not a full transcript) instead of the whole prior conversation.
  Files are re-read fresh from disk each step regardless (correctness is
  fine -- the file-content cache in `report_common.build_context` already
  avoids the disk-I/O cost of re-reading unchanged files), so this is really
  about not re-sending an ever-growing transcript to the model every turn.
  Tradeoffs to weigh before building this:
  - Real benefit: bounded context per step regardless of total plan size,
    so this class of failure (context ceiling hit mid-task) stops scaling
    with plan size at all.
  - Real cost: per-step re-invocation overhead (fresh system prompt, model
    warmup already resident so that's cheap, but re-establishing situational
    awareness each step isn't free) -- likely not worth it for a plan with
    only 1-2 small steps.
  - Needs the progress ledger to carry forward decisions made in an earlier
    step that a later step depends on (e.g. "Step 1 chose the Worker-buffer
    approach over widening history" needs to be visible to Step 4 even
    though Step 4's fresh context never saw Step 1's reasoning play out).
  - Testing/verification probably still wants to happen once, after all
    steps land, not fragmented per-step -- a final "integration" pass.

- **Benchmark the Builder/Renovator model split (shipped, unvalidated).**
  Builder moved to the MoE (Qwen3.6-35B-A3B, instruct) and Renovator got
  its own role on the dense model (Qwen3.8-27B, instruct): fast bulk pass,
  expert cleanup. Nothing about this is measured yet, and there is one
  specific unknown that matters: every tool-call pathology seen so far
  (stripped/missing ```tool fences, the bare-JSON case, the
  complete-but-unfenced case) happened on the dense model's "tokenizer"
  chat template. The MoE uses "local_qwen36" and has never been asked to
  emit a tool call in a loop at all -- scout and auditor are both one-shot
  text. So this could be a clean win, or it could trade a reasoning
  bottleneck for a tool-call-reliability one. What to measure:
  - Wall-clock for Builder alone, vs. the ~2700s dense baseline.
  - Steps consumed, and how many were lost to tool-call parse retries.
  - Whether Auditor's verdict quality changes (does the fast pass leave a
    *cleanup*-sized fix list, or a rebuild-sized one? The whole economics
    of "95% fast, then expert" depends on that number being small).
  - Whether Renovator-on-dense actually closes a fix list it's handed.

## Done

- **Set Builder/Auditor to Qwen3.6 Balance and Planner/Renovator to
  Qwen3.8 Quality, backed by the full 5-model x 2-convention benchmark**
  (see the native-tool-calling and harder-task entries above). Neither
  "Speed" variant won: on the same task, same mode, the non-Speed variant
  of both families was faster AND had fewer self-inflicted errors to
  recover from -- not a tradeoff. Qwen3.6 Balance ran clean in one shot
  under native tool-calling (247s); Speed crashed on its first attempt
  (an `ask_question` call with no answerer wired up) and took 373s on
  retry. Qwen3.8 Quality was faster in both conventions tested
  (1,072s/772s vs. Speed's 1,963s/1,517s) and had zero self-correction
  incidents across both runs; Speed's native run corrupted three
  pre-existing tests during a rewrite and had to recover via `git diff`/
  `git checkout`.

  Builder and Renovator also get `native_tool_calling=True` now (Auditor
  and Planner deliberately don't -- both are one-shot `call_model()`
  requests with no tool loop to route through it, so the flag would be a
  no-op there). Renovator's native flag isn't independently benchmarked
  for the Renovator role specifically, but it reuses Builder's exact loop,
  and Qwen3.8 Quality's best Builder-role result in the whole matrix
  (772s, zero errors, found the actual root cause of a real test-runner
  bug) was under native mode -- revisit if a Renovator-specific run
  disagrees.

  Fixed a real bug caught while wiring this up: `_role()`'s `profile` was
  a single hardcoded `"turbo"` regardless of model -- silently wrong for
  every MoE role (scout/chat/prompt_dev included, not just the ones this
  change touches) since `"turbo"` is compiled/verified against the dense
  27B/9B flagships specifically. Now derived from `model_family()`
  (`_PROFILE_BY_FAMILY`), confirmed against mtplx's own
  `recommended_profile` per model rather than assumed, so it can't drift
  silently again the way the flat default did. Scout/Chat/PromptDev now
  correctly get `"sustained"` too, as a side effect of fixing the
  mechanism rather than a deliberate change to those roles.

  213 tests pass (2 pre-existing assertions updated to the new defaults,
  1 new test pinning the profile-derives-from-family fix).


- **Added an opt-in native tool-calling path, and it fixed a real Ornith
  failure the hand-rolled convention couldn't.** This project has always
  avoided sending an OpenAI `tools` field (see report_common.stream_chat's
  docstring) because that routes requests through mtplx's
  `--tool-prompt-mode hybrid` bridge, which produced real parse failures
  going through a chat-client extension early in the project. That finding
  didn't generalize to every model: Ornith-1.5 is specifically trained for
  tool use, and on the "hard" structural-save task (see below) it failed
  0/3 attempts under the hand-rolled convention -- not occasional
  hiccups, but a real, reproducible loop, repeatedly attempting a native
  `<tool_call>` despite being told on every single turn there's no
  tool-calling API here. Confirmed live that mtplx supports a proper
  native mode for this: launching with `--tool-prompt-mode native
  --reasoning-parser qwen3` produces clean, correctly-parsed
  `tool_calls` responses (`tool_parser_source: "native"`/
  `"streaming_translator"`, zero raw markup leaking through), with
  reasoning cleanly separated into its own `reasoning_content` delta
  channel instead of mixed into content.

  Added `native_tool_calling: bool` as a per-role flag (default `False`,
  so Qwen3.6/3.8 -- proven reliable on the hand-rolled convention -- are
  completely unaffected), `stream_chat_native` (report_common.py) sending
  the `tools` field and accumulating streamed `tool_calls` deltas by
  index, `OPENAI_TOOL_SCHEMA` (builder_tools.py) translating the existing
  five actions, and `_run_agent_native` (builder_agent.py) -- a parallel
  loop to the default one, not an inline branch, since the two message
  histories differ enough (OpenAI tool-role messages vs. a single fenced
  block in plain text) that interleaving them would hurt both. Re-ran
  Ornith on the exact task that failed 3/3 times: 19 clean steps, correct
  reasoned decision, even caught and fixed its own test bug mid-run.
  212 -> 224 tests (12 new, covering the native loop's tool-result
  message shape, ask_question-as-tool-result, truncated-argument retry,
  and the same empty-response/max-steps guards the default loop has).

  Not yet done: the corresponding test on Qwen3.6/3.8 under native mode
  (does it help, hurt, or not matter for models the hand-rolled
  convention already serves well?), and a Deck-tab control for the flag
  (currently state.json/CLI-flag only, same starting point kv_quantization
  had before it got a GUI control).

- **Designed a harder benchmark task specifically to stress judgment
  under ambiguity: the structural-save defect ("added/deleted steps not
  saved"), left deliberately less prescriptive than the earlier mm:ss
  task.** The mm:ss task's fully-scaffolded plan let every model succeed
  identically (a useful speed comparison, but not a completeness one).
  This one hands over the scout report's own honest conclusion --
  static analysis cannot reproduce the symptom -- and requires the
  builder to choose and defend one of three real paths (a defensible
  low-risk fix, a persistence-layer regression test, or "no safe action
  is justified") rather than follow a recipe. Two of three tested
  builders chose (b) independently and defended it well: Qwen3.6 Balance
  (453s, 16 steps) and Ornith under native tool-calling (651s, 19
  steps, plus a second, more thorough test than Qwen3.6 added). Both
  explicitly followed the plan's warning about a prior run's test being
  silently never executed by the self-runner, verifying via `pytest -k`
  independently rather than trusting the self-runner's count alone.


- **Added Ornith-1.5-35B-A3B as a benchmarkable model, and fixed a real
  crash its architecture exposed in the agentic loop.** Same MoE class as
  the existing Scout/Builder model (35B total, ~3B active), added via
  `DEFAULT_ORNITH` + `model_family()` + a `SAMPLING_PRESETS` entry aliased
  to Qwen3.6-35B's (confirmed numerically identical across all three modes
  from the published card, not assumed from shared lineage). One real
  published difference this app now has to live with: Ornith cannot
  disable reasoning at all (no instruct/off mode -- it always opens with
  `<think>`), unlike Qwen's `reasoning="off"` pairing for Builder/Renovator.

  That difference broke Builder on first contact: `MAX_COMPLETION_TOKENS`
  (12,000, sized against the JSON-escaping runaway from the previous
  incident) was too tight for a model that can't turn reasoning off --
  Ornith burned the entire budget thinking at a modest 31K-token prompt,
  got truncated (`finish_reason: "length"`) before producing any visible
  output, and `run_agent` mistook the resulting empty response for a
  legitimate "I'm done" signal, writing a blank report having touched
  nothing. Fixed two ways: raised the cap to 24,000 (still firmly bounds
  the original runaway, which never got close to 2x its own length), and
  `run_agent` now retries on a whitespace-only response instead of
  accepting it as completion -- an empty string is never a valid "done"
  signal regardless of which model or architecture produces it.

  Re-running after the fix surfaced a second, more serious finding:
  Ornith's Builder report described a specific test it never wrote
  (`test_protocol.py` untouched) and a specific file change it implemented
  then fully undid while fighting shell-quoting issues during its own
  self-correction attempt -- a fabricated completion claim, not an honest
  "couldn't finish." See the benchmark results below for the full
  three-role comparison (Scout/Builder/Auditor) against Qwen3.6-35B.


- **The Builder crash from turn 9 (see the entry below) is now actually
  mitigated, not just diagnosed.** Two fixes in scripts/builder_agent.py
  and scripts/report_common.py's stream_chat: (1) every agentic turn now
  sends `max_tokens=12000`, so a stuck generation gets truncated in
  seconds instead of running until mtplx's own repetition-holdback and
  300s stream-stall watchdog eventually kill it -- the incident's 798-
  second, 17,329-token turn would never have gotten past 12k under this
  cap; (2) after 2 consecutive tool-call parse failures, the feedback
  message stops repeating the generic "try again" (which visibly didn't
  work -- the incident had three identical failures in a row before the
  runaway turn) and names the fix directly: split the write into a
  write_file call plus one or more append_file calls. Renovator reuses
  run_agent() unchanged, so both fixes cover it automatically. Neither
  fix addresses *why* the model breaks JSON escaping on a large write in
  the first place -- that's still open -- but both shorten the failure
  from a 13-minute dead end to a fast, cheap one the loop recovers from.


- **Char budget now derives from the role's actual context_window, and
  report artifacts no longer overwrite their own history.** Two fixes to
  the same class of problem found analyzing the two most recent runs:
  DEFAULT_CHAR_BUDGET was a hand-typed constant that went stale for weeks
  after context_window moved 100k -> 131,072, and separately, a REJECT'd
  audit's original findings were unrecoverable by the time anyone went
  looking, because the re-audit had already overwritten them at the same
  fixed path. `char_budget_for_role(phase)` (scripts/report_common.py)
  reads the live role's context_window and derives the budget from it
  (~3.7 chars/token, 34k tokens reserved for prompt+response), falling back
  to the constant only when the role can't be resolved -- an unknown
  phase, or a cloud-backed Planner, whose real context window this app
  doesn't track and would be a guess to derive from roles["planner"] while
  that role sits unused. `write_report(path, content)` archives whatever
  was already at a fixed artifact path (e.g. .ai/audit-report.md) into
  .ai/history/ before overwriting it, so every phase's write still lands
  at the exact path everything downstream expects, but a real, diffable
  run history now accumulates instead of erasing itself. Wired into all
  five report-writing sites (scout/planner/auditor/builder/renovator).

- **A from-Planner rerun of the remote-Tailnet-access plan crashed Builder
  on turn 9 of its 80-turn budget, not from running out of turns.**
  Diagnosed from .run/builder.log: the model got stuck re-attempting a
  large write_file for modeldeck/state.py after earlier attempts broke on
  JSON escaping ("the tool block was not valid JSON"), spiraled into a
  798-second, 17,329-token generation, tripped mtplx's own repetition-
  holdback safety net (the same degenerate-repetition signature hit
  earlier trying kv_quantization="off" on Scout), and was killed by
  mtplx's stream-stall watchdog (idle >300s). builder-report.md ended up
  0 bytes -- the process never reached a finishing move, plain-text or
  otherwise -- so the resulting REJECT/0% wasn't "wrote code, didn't test
  it," it was "wrote almost nothing, in one file, then choked." Renovator
  then inherited what was effectively the whole original plan rather than
  a narrow fix list, and correctly hit its own 80-step cap without
  finishing either. Nothing to fix here beyond what already shipped (the
  test-gate language in builder_agent.py's SYSTEM_PROMPT_TEMPLATE); it's a
  real failure mode worth recognizing on sight next time a run reports a
  suspiciously low turn count alongside a suspiciously low accuracy score.


- **Scout was reading roughly half the tree, and the missing half was
  always the same half.** `build_context` fills its char budget in list
  order and drops the remainder; `collect_files` returned plain
  alphabetical order, so a shortfall amputated whole directories rather
  than trimming evenly. Measured on this repo: 60 files found, 27
  included, 33 dropped -- the cutoff landed inside `scripts/`, so every
  file in `scripts/` and `tests/` was absent from every Scout context ever
  built. Scout had never read the code that actually runs the pipeline.
  Three fixes: (1) `sandbox/` is ignored when it sits inside the scanned
  tree (it was eating ~250k chars, 20% of the budget, on an unrelated
  thermocycler project) while still being scannable when targeted
  directly -- ignore names are now matched relative to the scan root, not
  anywhere in the path, which was a latent bug for any checkout living
  under a directory named `build`/`dist`; (2) `DEFAULT_CHAR_BUDGET` 260k
  -> 360k, since the old number was sized for the 100k context window and
  was never updated when roles moved to 131,072 -- a third of the window
  was going unused while files were being dropped over the old budget;
  (3) `collect_files` now round-robins across top-level directories, so a
  future shortfall costs every area its tail instead of erasing one area.
  Result on this repo: 40 files found, 39 included, 1 skipped
  (`scripts/stack.sh`). Worth remembering that `describe_skipped` was
  already telling the model which files it couldn't see -- that is the
  only reason this degraded quality quietly instead of producing obvious
  hallucinations about missing files.


- **Reports tab: replaced the "Last completed request" telemetry box (fed
  by `/metrics`, which only updates once a request finishes -- frozen for
  the entire duration of whatever's actually running) with genuinely live
  data folded into the "Current request" box** (phase, live tok/s, prompt/
  generated token counts, elapsed time, prefill ETA estimate, MTP depth
  acceptance -- all from `/v1/mtplx/flight`, which updates *during* a
  request). Removed the now-dead `MetricCard` class and its styling.
- **Raised `context_window` from ~100K to 131,072 (128K) for scout/builder/
  auditor**, after a live Builder run hit 95K/104K prompt tokens by step 25
  on a multi-file task. Confirmed real RAM headroom first (64G box, ~20G
  resident for weights+context) rather than guessing. Deliberately capped
  at 128K rather than raised further: a bigger window costs more prefill
  time per request regardless of whether it's filled, so this is a real
  speed/room tradeoff, not a free upgrade.
