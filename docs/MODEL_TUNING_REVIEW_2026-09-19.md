Review date: 19 September 2026. Target: M1 Max MacBook Pro, 64 GiB unified memory.

Implementation follow-up: the five numbered findings in the review's conversational
summary have now been addressed. Effort controls follow model capabilities;
mode transitions select the corresponding sampler; Ornith has an independent
preset; local Planner/Verifier use medium thinking; and fresh defaults,
configuration reporting, and deployment documentation have been updated.
The saved deployment was backed up before applying these settings. The absent
Ornith planning role now uses installed Balance. Existing Builder, weight/KV
quantizations, and custom Auditor context were retained. Chat/Prompt development
now use the locally measured D1 setting.

Scout/Diagnose now provide a structured complexity assessment. Deterministic risk
checks can escalate it, and malformed/missing assessments use the complex route.
Desktop/mobile tram diagrams show the selection and reason. Both lines retain
Planner → Builder → Auditor and the existing conditional verification/repair
steps. Simple Planner is editable in both Deck interfaces. No inference-quality
or speed improvement is claimed without a new model benchmark.

The original review below is a historical snapshot of the pre-change settings;
the README and `modeldeck.launch show --json` describe the current configuration.

The current model choices are defensible, but the configuration does not yet reliably express the intended tradeoff. Fix ineffective effort controls, sampling/mode coupling, and context accounting before reducing weight precision. The largest likely workflow gains are shorter task contexts and avoiding unnecessary model loads.

This review inspected the working tree, saved Model Deck state, installed model configurations and chat templates, MTPLX 2.11.2 source, local tuning records, and current upstream cards. No serving settings were changed and no models were loaded or benchmarked. The repository already contains substantial uncommitted work; this document is the only review change. Existing focused tests passed: 151 passed, 1 skipped across configuration/handoffs, context budgets, work items, routing, and router tests. Those tests do not establish inference quality or memory safety.

**What is actually configured**

Saved state in `~/Library/Application Support/Model Deck/state.json` overrides `modeldeck/state.py`. The saved Planner backend is local. Inspecting only the code defaults or README would give a materially different answer.

| Role | Saved artifact | Thinking / effort | Serving | Window |
| --- | --- | --- | --- | ---: |
| Scout | Qwen3.6 Balance | on / medium | sustained, D1, KV off | 131,072 |
| Builder | Qwen3.8 Quality | off / medium | turbo, D3, KV Q8 | 131,072 |
| Renovator | Qwen3.8 Quality | off / auto | turbo, D3, KV Q8 | 131,072 |
| Auditor | Qwen3.6 Balance | on / medium | sustained, D1, KV off | 108,544 |
| Diagnose | Qwen3.6 Balance | on / low | sustained, D1, KV off | 131,072 |
| Planner / Verifier | Qwen3.8 Quality | off / auto | turbo, D3, KV Q8 | 131,072 |
| Low-consequence planner | Ornith V2 | auto / medium | sustained, D1, KV off | 131,072 |
| Chat / Prompt development | Qwen3.6 Speed | auto / medium | sustained, D3, KV Q8 | 131,072 |

All roles have RAM session-bank limits of 16G total / 12G per session, eight entries, and SSD session caching disabled. Builder and Renovator use native tool calls and an 80-step ceiling. These are configured values, not proof of a currently resident server's effective settings.

**Model-card verification**

The Qwen entries in `SAMPLING_PRESETS` match the published recommendations. Here is the exact comparison; repetition penalty is 1.0 and min-p is 0.0 in every Qwen row.

| Model / mode | Temperature | Top-p | Top-k | Presence penalty | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Qwen3.6 general thinking | 1.0 | .95 | 20 | 1.5 | Matches |
| Qwen3.6 precise coding thinking | .6 | .95 | 20 | 0 | Matches |
| Qwen3.6 instruct | .7 | .80 | 20 | 1.5 | Matches |
| Qwen3.8 thinking | 1.0 | .95 | 20 | 0 | Matches |
| Qwen3.8 instruct | .7 | .80 | 20 | 1.5 | Matches |

Sources: [Qwen3.6 card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B#best-practices), [Qwen3.8 card](https://huggingface.co/Qwen/Qwen3.8-27B#best-practices). These are starting contracts, not evidence that every setting is optimal for every repository task. There is no basis here to replace all presets with greedy decoding or a generic low temperature.

Ornith is an exception. `modeldeck/state.py` aliases its entire preset table to Qwen3.6 and calls those presets publisher recommendations. The current [original Ornith card](https://huggingface.co/ornith-ai/Ornith-1.5-35B-A3B) recommends temperature .6, top-p .95, top-k 20 for general use; 1.0 is for reproducing benchmarks. The [V2 MTPLX conversion card](https://huggingface.co/philipjohnbasile/ornith-ai-Ornith-1.5-35B-A3B-V2-MTPLX) also records .6/.95/20. Neither cited recipe establishes the inherited presence penalty of 1.5 as its recommendation. Give Ornith an independent preset, use .6/.95/20 as its baseline, and explicitly identify any penalty choice as a local choice. Do not advertise an independently verified instruct preset for this reasoning artifact.

The FP16 siblings are appropriate for M1/M2. FP16 describes floating tensors around the packed weights, not full 16-bit weight storage. The [Balance conversion card](https://huggingface.co/Youssofal/Qwen3.6-35B-A3B-MTPLX-Optimized-Balance-FP16) specifies a 6-bit body, sustained profile, and D2. Local D1 is an intentional, measured departure. The [Quality conversion card](https://huggingface.co/Youssofal/Qwen3.8-27B-MTPLX-Optimized-Quality-FP16) specifies 8-bit weights and D3; its published performance measurements are on an M5 Max, not this M1 Max.

**Corrections that matter most**

1. **Qwen3.6 effort controls are ineffective as model effort tiers.** The installed Qwen3.6 templates use `enable_thinking` and `preserve_thinking`, but never `reasoning_effort`. In installed MTPLX, `ReasoningCodec.effort_levels` is empty for this family and `_reasoning_effort_for_state()` returns `None`. Consequently the comment claiming Diagnose's `low` separates it from Auditor's `medium` is not implemented by the model. The [MTPLX documentation](https://github.com/youssofal/MTPLX) likewise says Qwen3.6 exposes no effort tier. Disable that UI control for this family or display “unsupported.” Use smaller evidence packets, explicit completion limits, or actual non-thinking mode for cheap classification. A completion cap alone can still terminate thinking before any answer appears. The runtime's optional agent thinking guard is off by default and applies only to thinking requests declaring tools; it does not rescue one-shot reports.

2. **Turning thinking off does not switch sampling presets.** `router/main.py:prepare_payload()` honors the internal reasoning override, then writes the original role's temperature/top-p/penalties. Diagnose's non-thinking sweep therefore runs with .6/.95/presence 0, not Qwen's .7/.80/presence 1.5 instruct recipe. Resolve an effective mode and its preset together, while preserving explicitly supported custom overrides. Record both requested and effective settings.

3. **High-consequence Planner and Verifier currently run without thinking.** That is a supported Qwen3.8 mode, but not the designer's default thinking behavior. Qwen3.8 supports low, medium, and xhigh, with xhigh as the upstream default; the installed runtime resolves thinking-off to no effective effort. For difficult planning and disputed findings, trial thinking on with explicit medium effort and the thinking sampler, using a small packet. Reserve xhigh for unresolved hard cases. Do not infer that raising Builder's displayed effort does anything while thinking remains off. [Qwen3.8 thinking controls](https://huggingface.co/Qwen/Qwen3.8-27B)

4. **Reasoning preservation is incomplete in the client.** `stream_chat_native()` collects and returns `reasoning_content`, but `_run_agent_native()` rebuilds assistant history from content and tool calls without that field. `preserve_thinking=auto` cannot preserve history the client omitted. This is latent with the saved thinking-off Builder, but relevant to the code-default thinking Builder and future reasoning-enabled tool runs. Decide explicitly whether to retain reasoning within a work unit; test the resulting prompt and prefix-cache behavior. Drop prior-unit transcripts at a checkpoint.

5. **Context limits are being used as content targets.** `char_budget_for_role()` offers approximately 359,000 characters at 128K by subtracting a fixed 34K reserve. That encourages large prompts regardless of task difficulty. Planner appends Scout's report and the user task after filling its source budget without deducting them. Auditor deducts plan/task/diff but subsequently appends verification and ledger output. The fixed reserve may absorb some overhead; it is not a complete accounting guarantee. Count the actual serialized prompt with the model tokenizer, reserve an explicit output allowance, and add separate phase input targets. Merely lowering the configured context to 32K will not work well with the existing 34K reserve.

6. **Missing and stale deployment assumptions.** `mtplx models --json` lists four installed, valid Qwen artifacts, but no Ornith, despite the low-consequence route pointing to it. Preflight availability and fall back to installed Balance rather than discovering this during a run or relying on an implicit download. Chat and Prompt development still default to D3 although local Speed tuning selected D1. README still describes older model assignments, 100K windows, and uniform D3. Make the deployment manifest and documentation report the resolved saved configuration.

**Quantization and memory**

Installed `config.json` files and safetensor file sizes give the following. File sizes include sidecars and are not measured resident memory.

| Artifact | Main weight quantization | Safetensor files |
| --- | --- | ---: |
| Qwen3.6 Speed | 4-bit / group 64; selected 8-bit overrides | 19.54 GiB |
| Qwen3.6 Balance | 6-bit / group 64; selected 8-bit overrides | 27.61 GiB |
| Qwen3.8 Speed | 4-bit / group 32; selected 8-bit overrides | 19.26 GiB |
| Qwen3.8 Quality | 8-bit / group 64 | 27.90 GiB |

Keep single-model residency. Roughly 28 GiB of weight files leaves room on a 64 GiB system, but model buffers, live attention/recurrent state, session-bank retention, applications, and macOS all compete for unified memory. The 16G bank is a ceiling, not an upfront allocation or total inference-memory limit. It must not simply be added to live KV as though all data is necessarily distinct.

Both families use hybrid attention; “dense” describes Qwen3.8's feed-forward architecture, not all layers having full attention. From the installed dimensions, raw growing KV per sequence is approximately `2 × full_attention_layers × KV_heads × head_dim × tokens × bytes`. At 131,072 tokens this is 2.5 GiB for Qwen3.6 in FP16 and 4 GiB for Qwen3.8 at one byte per KV element. Quantization metadata, recurrent states, snapshots, speculative buffers, and allocator overhead are additional. These are geometry estimates, not peak-memory measurements.

Retain Balance's KV-off and Quality's KV-Q8 as present baselines; trial Q8 for MoE only if memory pressure warrants it. Do not default to Q4 KV or more aggressive weight quantization without accuracy tests. For short serial units, trial a smaller bank (for example 4–8 GiB total, one or two retained sessions), but size it from measured session footprints and check cache misses before promotion. Smaller caches can make a long run substantially slower. Keep SSD caching off until the previously documented cross-session contamination is independently resolved.

The local tuning file supports D1 for Balance (46.4 tok/s), D1 for Speed (64.4), and D3 for Quality (29.5). These are historical short-output decode measurements on MTPLX 2.10.2/2.11.1, thinking disabled, not present-version end-to-end task rates. Quality's depth tuning even used temperature 1.0 with thinking disabled, unlike its current instruct sampler. Keep these depths provisionally and retune with the actual sampler, representative prompt lengths, and the installed runtime. Do not generalize one MoE artifact's optimal depth to every MoE architecture.

The recorded structural-save task also favored Balance and Quality over their Speed siblings in completion time and recovery behavior. That supports keeping them, but one task and a handful of runs do not establish general superiority. Lower-bit weights can lose their speed advantage through retries and bad edits.

**Recommended workflow**

Use a small deterministic preflight to collect the requested behavior, file inventory, changed-file manifest, relevant symbols/importers, and baseline check results. Select a route using evidence about the proposed change, not only the current Git diff: a clean checkout can still be about to receive a sensitive auth or persistence change.

For a narrowly specified change: focused retrieval → compact implementation brief → Builder unit → targeted checks → focused review. Skip a separate broad Scout report and separate model-written plan when the brief already states the affected contracts and acceptance criteria. For ambiguous or cross-cutting work: focused Scout → Planner → dependency-ordered Builder units → integration checks → audit. Keep the existing conditional Verifier before disputed/high-risk repairs, followed by bounded Renovator and re-verification.

Start experiments with 8–16K input tokens for triage/Scout, 16–32K for planning and each Builder unit, and 16–32K for a focused audit; allow 32–64K for demonstrated cross-cutting needs. These are proposed working targets, not model-card context limits. Preserve the 128K capability as headroom initially. Trial 2–4K completion tokens for simple non-thinking tool actions and 4–8K for short reports; allocate larger, separate allowances for reasoning work. Do not impose those small limits on Ornith or thinking reports without a recovery path. Today Builder allows 24K per generation, while Scout/Planner/Auditor/Verifier do not pass an explicit completion cap.

Work units already exist in `modeldeck/work_items.py` and `scripts/builder_agent.py:run_work_items()`. Extend that implementation rather than adding another orchestration layer:

- Require a valid work-item graph for multi-step plans; the legacy fallback still permits one long conversation.
- Split by a testable behavioral contract, not by individual file. Keep an interface change and the callers/tests needed to make it valid in one unit.
- Carry the unit requirements, relevant excerpts, established dependency contracts, unresolved decisions, and selected check output. The existing brief contains the title/contracts/files but has no dedicated detailed unit-instructions field; avoid losing substantive plan requirements during subdivision.
- Reset at verified unit boundaries and add a token-aware checkpoint before a single unit itself grows too large. Current loops accumulate messages within each unit until their step limit.
- Implement small-unit batching: `should_batch()` exists but no production caller uses it, despite the runner docstring saying units are batched. Use estimated tokens and shared dependencies, not only its current four-file heuristic.
- Bind the progress ledger to the plan/run and relevant file hashes. The current loader reuses verified item IDs without establishing that the new plan and source state still match. Context reduction is safe only if carried-forward completion evidence is still valid.
- Run focused unit checks, then the integration profile after assembled changes. Reuse check evidence only when code and environment fingerprints match; avoid rerunning a complete suite redundantly inside every model loop and again after each unit.

Audit all changed paths deterministically, then send changed hunks plus enclosing code, affected interfaces/importers, and tests to the model. A whole-tree model reread is not necessary to discover out-of-scope edits: the run baseline already provides that manifest. Retain an explicit broader-review route when architecture changes or missing evidence demand it.

Keep roles logically separate but reuse a resident artifact across compatible phases. The current roles have separate ports, and residency reuse is checked on the requested role's port; selecting the same weights for two roles does not by itself eliminate reloads. Separate model-serving identity from per-request role prompts/sampling/thinking, verify supported runtime controls, and maintain one active model. Batch Verifier findings and compatible repairs while the dense model is loaded. Benchmark the default fast Balance Builder against the currently saved Quality Builder; promote the fast path only when accepted-patch time and failure rate justify it.

**Order of work and acceptance evidence**

First fix the unsupported effort UI, Ornith preset, reasoning/sampling coupling, full-prompt accounting, and unavailable-model routing. Then complete work-unit budgeting, plan-bound ledgers, focused auditing, and compatible residency reuse. Finally compare model assignments and cache sizes.

Use several representative tasks: small bug fix, multi-file feature, diagnosis with misleading evidence, sensitive state/interface change, and a larger repository review. Run multiple repetitions with the same task fixtures and thermal/power conditions. Measure time to a verified accepted patch, cold-load time, prefill time, generated reasoning/content tokens, retries, false audit findings, peak memory, swap growth, and session-cache hit/miss behavior. A preset should win on accepted outcomes and practical wall time, not only decode tokens per second. No new performance improvement is claimed by this review.
