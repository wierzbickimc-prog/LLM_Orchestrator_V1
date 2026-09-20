# LLM Orchestrator

Local router and phase launcher for a two-model coding workflow on an M1 Max
64 GB MacBook Pro.

## Model Deck desktop app

Double-click **Model Deck** on the macOS Desktop, or launch it from a terminal:

```bash
./scripts/run_model_deck.sh
```

The desktop app has two tabs:

- **Deck** -- keeps any OpenAI-compatible chat client on one stable configuration
  (`http://127.0.0.1:8100/v1`, model `local`), selects the GPT-5.6 Sol Planner or
  one locally resident MTPLX role, displays live request telemetry, and stores
  the OpenAI key in macOS Keychain. Still useful for ad-hoc coding in an editor.
- **Reports** -- runs Scout/Planner/Builder/Auditor directly against the
  router, with no chat extension involved. See "The routed pipeline" below for
  why this exists and how it differs from the Deck tab's copyable prompts.

## The routed pipeline

The Reports tab runs purpose-built Scout/Planner/Builder/Auditor scripts.
Scout (or Diagnose for troubleshooting) emits an evidence-backed complexity
assessment. The tram map shows two routes from that first station:

- Simple: Qwen3.6 Balance Planner → Builder → Auditor.
- Complex or uncertain: Qwen3.8 Quality Planner → Builder → Auditor.

Both routes require the same implementation-plan and work-item handoff.
Builder never has to invent a missing plan. Sensitive planned paths, broad
changes, open questions, incomplete coverage, and prior failures override a
simple assessment. Missing/malformed assessments select the complex route.
Operator overrides remain explicit, and a failed simple plan can escalate once.
The selected route, model, assessment, and reason are recorded in the run journal
and displayed on desktop/mobile. Step mode shows the decision after Scout while
waiting for Continue. Cloud planning is optional and is labelled separately.

Scout and Planner are tool-free report calls. Builder and Renovator use native
tool calls by default; the older fenced-tool protocol remains available for
compatibility. The native runtime path avoids the legacy hybrid bridge that
motivated moving the workflow out of a chat extension. Builder executes dependency-ordered
work items in fresh conversations and records verification in a progress ledger.
Auditor uses the run diff, source context, and actual checks. Disputed/high-risk
findings can trigger the read-only Verifier before a bounded repair pass.

Artifacts live under the target project's `.ai/`, including `scout-report.md`,
`implementation-plan.md`, `work-items.json`, `builder-report.md`, and
`audit-report.md`. An external chat client remains supported for ad-hoc coding
through `local`.

The deployment uses FP16-compatible MTPLX builds on the M1 Max. Their main
weights are quantized: Balance is mostly 6-bit and Quality is 8-bit. Only one
local model is resident at a time.

## Intended architecture

```text
VS Code
  |
  v
Chat extension (optional, ad-hoc coding only)
  |
  v
Local OpenAI-compatible Router
  |
  v
Active phase endpoint
  |-- scout   -> Qwen3.6-35B-A3B
  `-- builder -> Qwen3.8-27B

Only one MTPLX model is resident at a time.

Model transitions are serialized across the desktop app, web service, and
`scripts/stack.sh`. A launch is recorded before its health endpoint opens, and
the next launch is not allowed to begin until the previous MTPLX process has
exited. This also makes Stop effective while model weights are still loading.
```

## Project status

The router and Model Deck desktop controller are implemented. The router exposes
OpenAI-compatible model discovery and streaming/non-streaming chat completions.
Model Deck discovers validated MTPLX models, switches one local model into RAM at
a time, selects the GPT planner without reconfiguring any client, and displays
MTPLX health and per-request telemetry.

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

Fresh-install defaults match the reviewed local deployment baseline:

| Role | Artifact | Thinking / effort | Profile / MTP depth | KV | Context ceiling |
| --- | --- | --- | --- | --- | ---: |
| Scout | Qwen3.6 Balance FP16 | on / unsupported | sustained / 1 | off | 131,072 |
| Simple Planner | Qwen3.6 Balance FP16 | on / unsupported | sustained / 1 | off | 131,072 |
| Complex Planner / Verifier | Qwen3.8 Quality FP16 | on / medium | turbo / 3 | Q8 | 131,072 |
| Builder / Renovator | Qwen3.8 Quality FP16 | off / inactive | turbo / 3 | Q8 | 131,072 |
| Auditor / Diagnose | Qwen3.6 Balance FP16 | on / unsupported | sustained / 1 | off | 131,072 |

Planner defaults to a scoped input target of approximately 24K tokens (character
estimate), including Scout's report and the task; explicit `--char-budget` can
override it. Planner/Verifier completions are capped at 16K/8K tokens respectively.
Qwen3.6 has no effort tiers. Qwen3.8 exposes low/medium/xhigh, and its effort
control is inactive when thinking is off. Internal mode changes select the
corresponding sampling preset; same-mode calls retain operator sampling edits.

All roles retain serial/latency scheduling, Smart fans, 2,048-token prefill
chunks, and RAM session-bank limits of 16G total / 12G per session / eight entries.
SSD caching is off; its configured 10G ceiling is inactive. These are cache limits,
not a measured total-memory requirement. Chat/Prompt development use Speed with
D1 and Q8 KV. Ornith is optional and has its own .6/.95/20 thinking preset;
neutral penalties are local choices, not a claimed publisher recommendation.

Saved settings in `~/Library/Application Support/Model Deck/state.json` override
fresh defaults across the GUI, web, and shell launchers. Existing operator model,
context, and sampling choices are retained when loading; unsupported effort tiers
normalize to auto. Use the Deck editors (including Simple Planner), or inspect
resolved serving settings and overrides:

```bash
.venv/bin/python -m modeldeck.launch show --json
```

A missing optional simple-planner artifact selects the configured complex planner
for automatic routing. An explicit forced route does not silently change class;
preflight reports the missing artifact. Launchers check availability before
stopping a resident model and do not download models implicitly.

The model-card comparison, local measurements, and limitations are recorded in
[the tuning review](docs/MODEL_TUNING_REVIEW_2026-09-19.md).

The implemented desktop control plane is described in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); superseded proposals are kept
under `docs/history/`.

## Chat client configuration

Use an OpenAI-compatible provider and point it at:

```text
http://127.0.0.1:8100/v1
```

Permanent model name:

```text
local
```

The router resolves `local` to the phase selected in Model Deck. Legacy `scout`
and `builder` aliases remain available for direct testing, but a client should
stay on `local` so phase changes require no settings edits.

## Important design principle

This started as "a chat extension remains the agent; stay a thin model gateway
unless testing demonstrates otherwise." Testing demonstrated otherwise: running
the four-phase pipeline through an extension hit tool-call fragility, an
unavoidable "always present a plan" bias in the extension's own system prompt,
and per-turn context overhead that a summarizer role shouldn't be paying. The
router stays a thin gateway either way -- it doesn't know or care whether a
request came from an editor extension or from `scripts/*_report.py` -- but the
orchestration for the four-phase pipeline itself now lives in those scripts and
the Model Deck Reports tab. See "The routed pipeline" above.

One-shot report calls do not declare tools. Agentic phases use MTPLX's native
tool-call path with explicit parsing and verification. The old hybrid bridge
is not the default for these phases.

## Remote Tailnet Access (mobile / phone)

The router serves a responsive, dependency-free web UI (`router/static/index.html` + `app.js`) for the Deck, Reports, and Admin tabs. Its Deck exposes the same workflow roles, model inventory, reasoning/serving/sampling controls, presets, activation, and shutdown operations as the desktop Deck; both call shared transition logic and persist one configuration. When the desktop GUI owns a pipeline run, the phone automatically attaches to that same run: streamed text, phase state, completion output, and questions are replayed from one sequenced journal rather than starting a second session. Reconnecting resumes at the last received event. All pipeline operations and file I/O remain local.

### How to enable

1. Set the bind address by adding to your .env (or exporting in your shell):
   ROUTER_HOST=0.0.0.0   # or your exact Tailscale IP, e.g. 100.x.y.z

   The default is 127.0.0.1 (loopback only). Changing it to 0.0.0.0 makes the server reachable on all local interfaces. This is safe because the router's source-IP allowlist middleware rejects any request not coming from 127.0.0.1, the Tailscale CGNAT range (100.64.0.0/10), or CIDRs you configure via TAILSCALE_CIDRS.

2. Start the web server in its own terminal:
   python -m router.main

   (This is separate from scripts/run_model_deck.sh, which launches the desktop PySide6 GUI. You can run both concurrently if desired.)

3. Open the URL on your phone's browser:
   http://<your-tailscale-ip>:8100/

   Replace <your-tailscale-ip> with the machine's Tailscale address (visible in the Tailscale dashboard or tailscale status).

### Security notes

- Allowlist gate: Requests from non-Tailscale, non-loopback IPs receive 403 Access denied. The allowlist is the authoritative security boundary; binding on 0.0.0.0 is acceptable only because of it.
- Firewall: If you do not want direct LAN/WAN access to port 8100, configure your host firewall to block it. The allowlist mitigates remote exposure, but a local attacker on the same network could reach the socket before the middleware checks the source IP.
- Cleartext over the tunnel: Tailscale encrypts the tunnel end-to-end, so cleartext HTTP inside the tunnel is fine. No TLS reverse proxy is required for this use case.
- Optional CIDRs: If your Tailscale network uses a custom CGNAT range, set TAILSCALE_CIDRS=100.100.0.0/16,100.101.0.0/16 (comma-separated) to extend the allowlist.
