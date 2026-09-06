# Launcher plan

The implemented desktop direction and phase contract are specified in
[`GUI_PROPOSAL.md`](GUI_PROPOSAL.md).

## Near term

Use `scripts/stack.sh` as the stable service-control layer. It owns model launch
commands, ports, health checks, logs, and process IDs. The router remains a
separate OpenAI-compatible service on port 8100.

## Desktop GUI

Build a small Python desktop application after the dual-model Cline workflow is
validated. The first version should be a control plane, not another inference
runtime.

Core controls:

- Scout model selector populated from `mtplx models --json`
- Builder model selector populated from the same cache inventory
- Per-role port, profile, reasoning, context-window, and KV-cache settings
- Launch, stop, restart, and health indicators for each model and the router
- Live log tail and current resident-memory display
- Saved phase presets, including the 35B-A3B Scout and 27B Speed Builder
- Mutual exclusion so Scout and Builder are not resident at the same time
- A memory estimate and warning before launch, based on model weights, context,
  KV quantization, and session-bank caps
- Copyable Cline base URL and a one-click API compatibility test

Implementation direction:

- Python service/process layer shared with a CLI
- PySide6 for a native macOS-friendly GUI, packaged only after behavior settles
- Persist settings in a small user configuration file, never in source code
- Track process ownership so the GUI never stops an MTPLX daemon it did not launch
- Keep runtime adapters behind one interface so MLX/Ollama/other backends can be
  evaluated without changing the UI or router contract

## Validation gate

Do not build the GUI until Cline has completed real tool-using tasks through both
aliases. Those tests determine which controls actually belong in the launcher.
