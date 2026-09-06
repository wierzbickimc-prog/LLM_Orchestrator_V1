# Local Cline Router

Local router and phase launcher for a two-model coding workflow on an M1 Max
64 GB MacBook Pro.

## Model Deck desktop app

Double-click **Model Deck** on the macOS Desktop, or launch it from a terminal:

```bash
./scripts/run_model_deck.sh
```

The desktop app has two tabs:

- **Deck** -- keeps Cline on one stable configuration (`http://127.0.0.1:8100/v1`,
  model `local`), selects the GPT-5.6 Sol Planner or one locally resident MTPLX
  role, displays live request telemetry, and stores the OpenAI key in macOS
  Keychain. Still useful for ad-hoc coding with Cline.
- **Reports** -- runs Scout/Planner/Builder/Auditor directly against the
  router, with no chat extension involved. See "The four-phase pipeline,
  without Cline" below for why this exists and how it differs from the Deck
  tab's copyable prompts.

## The four-phase pipeline, without Cline

Running Scout/Planner/Builder/Auditor *through* Cline turned out to be
fragile in ways specific to going through a general-purpose coding-agent
chat extension: Cline's own system prompt carries an unconditional "always
present your plan first" instruction that fights a summarizer's actual job,
its `environment_details` block and tool-schema overhead inflate context on
every turn, and mtplx's `--tool-prompt-mode hybrid` bridge (needed to
translate Cline's OpenAI-style `tools` field into this model family's native
call format) can emit a malformed `<tool_call>` that stalls the whole turn.

`scripts/{scout,planner,builder,auditor}_report.py` are small, purpose-built
alternatives, runnable from the CLI or the Model Deck **Reports** tab:

- **Scout, Planner, Auditor** (`scout_report.py`, `planner_report.py`,
  `auditor_report.py`) are one-shot, tool-free calls: the *script* walks the
  file tree and reads files directly, hands the content to the model as
  plain text, and saves whatever comes back. No `tools` field is ever sent,
  so mtplx's hybrid tool-call bridge never activates for these -- there's
  nothing for it to intercept.
- Planner reads only the specific files Scout's report cites (see
  `report_common.referenced_files`), not the whole tree -- re-ingesting
  everything Scout already read would defeat the point of Scout being a
  cheaper summarizer. Auditor still scans the whole tree deliberately: part
  of its job is catching changes the plan never called for, which requires
  seeing files a narrower scope would never surface.
- **Builder** (`builder_agent.py`) is the one script that actually edits
  files and runs commands, so it needs real multi-step tool use. It still
  avoids the OpenAI `tools` field and mtplx's hybrid bridge: it uses a
  simple convention the *script* parses itself -- one fenced ` ```tool `
  JSON block per turn for `read_file`/`write_file`/`run_command`, plain text
  with no such block to signal completion. Not sandboxed beyond staying
  inside the target path -- read `.ai/implementation-plan.md` before running
  it, and try it on a low-stakes target first.

Each artifact (`.ai/scout-report.md`, `.ai/implementation-plan.md`,
`.ai/builder-report.md`, `.ai/audit-report.md`) is written inside the
*target project*, not this repo, regardless of where Model Deck itself is
installed.

Planner can also run entirely locally instead of against the cloud GPT
backend -- set the Deck tab's GPT Planner "Backend" to "Local model" and
pick which resident role (Scout/Builder/Auditor) to route through. Useful
when the OpenAI account has no credits, or you'd rather not spend them on
planning.

The Deck tab's copyable Scout/Planner/Builder/Auditor prompts (see
[`docs/GUI_PROPOSAL.md`](docs/GUI_PROPOSAL.md)) still work for ad-hoc coding
with Cline; they're just no longer the primary way to run the four-phase
pipeline.

## Goal

Use:
- **Scout / Explore model**: Qwen3.6-35B-A3B MTPLX speed-optimized
- **Builder / Coding model**: Qwen3.8-27B Q4 / MTPLX speed-optimized
- **MTPLX** as the preferred inference runtime
- **Cline in VS Code** as the coding agent UX
- A small local **OpenAI-compatible routing layer** only if needed to bridge multiple local model servers/endpoints

## Intended architecture

```text
VS Code
  |
  v
Cline
  |
  v
Local OpenAI-compatible Router
  |
  v
Active phase endpoint
  |-- scout   -> Qwen3.6-35B-A3B
  `-- builder -> Qwen3.8-27B

Only one MTPLX model is resident at a time.
```

## Project status

The router and Model Deck desktop controller are implemented. The router exposes
OpenAI-compatible model discovery and streaming/non-streaming chat completions.
Model Deck discovers validated MTPLX models, switches one local model into RAM at
a time, selects the GPT planner without changing Cline, and displays MTPLX health
and per-request telemetry.

The remaining validation work is to exercise a paid OpenAI request with the
user's Keychain-stored API key and benchmark the complete four-phase workflow on
a real repository.

## Quick start

```bash
cd local-cline-router
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m router.main
```

Default router URL:

```text
http://127.0.0.1:8100/v1
```

## Start and manage the local stack

After creating `.venv` and installing the requirements, launch one role at a
time. The selected model starts together with the router:

```bash
./scripts/stack.sh start scout
./scripts/stack.sh stop scout
./scripts/stack.sh start builder
```

For a one-command phase transition between launcher-owned models, use:

```bash
./scripts/stack.sh switch scout
./scripts/stack.sh switch builder
```

The launcher refuses to start one role while the other model is resident. It
reuses a healthy server on the selected role's port and records and stops only
processes that it launched.

```bash
./scripts/stack.sh status
./scripts/stack.sh logs
./scripts/stack.sh stop builder
./scripts/stack.sh stop all
```

Model references and ports can be overridden with environment variables such as
`SCOUT_MODEL`, `BUILDER_MODEL`, `SCOUT_PORT`, and `BUILDER_PORT`.

The defaults use the sequential workflow validated on this 64 GB Mac:

| Role | Model | Context | KV | RAM bank total/per session | SSD bank |
| --- | --- | ---: | --- | ---: | ---: |
| Scout | Qwen3.6-35B-A3B Speed | 100K | Q8 | 16 GB / 12 GB | 10 GB |
| Builder | Qwen3.8-27B Speed | 100K | Q8 | 16 GB / 12 GB | 10 GB |

(12 GB per session covers the full 100K context window's KV cache at the
default density; smaller values evict mid-session on long agentic runs and
force expensive full reprocessing instead of a cache hit.)

Both roles use Smart fan control, explicit MTP depth 3, serial/latency
scheduling, 2048-token prefill chunks, and automatic medium-effort reasoning.

Override them with `SCOUT_CONTEXT_WINDOW`, `BUILDER_CONTEXT_WINDOW`,
`SCOUT_KV_QUANTIZATION`, `BUILDER_KV_QUANTIZATION`,
`SCOUT_SESSION_BANK_MAX`, `BUILDER_SESSION_BANK_MAX`,
`SCOUT_SESSION_BANK_PER_SESSION_MAX`, `BUILDER_SESSION_BANK_PER_SESSION_MAX`,
`SCOUT_SSD_SESSION_CACHE_MAX`, and `BUILDER_SSD_SESSION_CACHE_MAX`.
Performance controls can be changed with `SCOUT_DEPTH`, `BUILDER_DEPTH`,
`FAN_MODE`, `PREFILL_CHUNK_TOKENS`, and `SESSION_BANK_MAX_ENTRIES`.

The longer-term model-selecting desktop control panel is described in
[`docs/LAUNCHER_PLAN.md`](docs/LAUNCHER_PLAN.md).

## Cline target configuration

Use an OpenAI-compatible provider and point it at:

```text
http://127.0.0.1:8100/v1
```

Permanent model name:

```text
local
```

The router resolves `local` to the phase selected in Model Deck. Legacy `scout`
and `builder` aliases remain available for direct testing, but Cline should stay
on `local` so phase changes require no settings edits.

## Important design principle

This started as "Cline remains the agent; stay a thin model gateway unless
testing demonstrates otherwise." Testing demonstrated otherwise: running the
four-phase pipeline through Cline hit tool-call fragility, an unavoidable
"always present a plan" bias in Cline's own system prompt, and per-turn
context overhead that a summarizer role shouldn't be paying. The router
stays a thin gateway either way -- it doesn't know or care whether a request
came from Cline, from `scripts/*_report.py`, or from Continue -- but the
orchestration for the four-phase pipeline itself now lives in those scripts
and the Model Deck Reports tab, not in Cline. See "The four-phase pipeline,
without Cline" above.

Every script in `scripts/*_report.py` and `builder_agent.py` avoids the
OpenAI `tools` field entirely, on both the read-only and the file-editing
side: mtplx's `--tool-prompt-mode hybrid` bridge (needed to translate that
field into this model family's native call format) is what produced the
malformed-tool-call stalls that motivated this whole redesign in the first
place. Prefer a convention your own code parses and recovers from over a
bridge you don't control, for any future phase added here.
