# Historical status and next steps

> Archived snapshot. It is retained as implementation history, not as the
> current project backlog. See the repository README and current workflow plans.

Everything in this file's previous version (MTPLX launch inspection, tool-call
verification, Scout/Builder testing, the file-based handoff) is done. See the
README's "The routed pipeline" section for the current design and why it
changed from the original chat-extension-centric plan.

## Done

- Router (`router/main.py`) with streaming/non-streaming proxying, phase
  detection, phase-instruction injection (skippable via
  `X-Model-Deck-Skip-Injection`), a request-capture endpoint
  (`.run/last-request.json`), and a repetition-loop guard
  (`/loop-alert`).
- Model Deck GUI (`modeldeck/gui.py`): Deck tab (phase switching, live
  telemetry, GPT/local Planner toggle) and Reports tab (run any phase
  directly, with live streaming output, an elapsed-time counter, and a Stop
  button).
- Four-phase pipeline as standalone scripts (`scripts/*_report.py`,
  `scripts/builder_agent.py`), each avoiding the OpenAI `tools` field to
  sidestep mtplx's `--tool-prompt-mode hybrid` bridge fragility. Builder is
  a small custom agent loop (`scripts/builder_tools.py` for the three
  actions, path-escape-tested).
- Planner reads only the files Scout's report cites
  (`report_common.referenced_files`), not the whole tree.
- Local fallback for Planner (route through Scout/Builder/Auditor's
  resident model instead of the cloud GPT backend).
- 55 unit tests across `tests/`, all passing (`python -m unittest discover
  -s tests`).

## Known gaps / open questions

- **Builder has not been run live end-to-end against a real target.** Its
  control logic (tool parsing, error recovery, dry-run, path containment) is
  unit-tested with a mocked model, but nobody has watched it actually edit a
  real file yet. Try it with `--dry-run` first, then for real on a
  low-stakes target.
- **The full pipeline (Scout -> Planner -> Builder -> Auditor) has never
  completed end to end in one run.** Individual phases have each succeeded
  in isolation at various points; a single unbroken run through all four
  hasn't been observed.
- Planner-via-cloud-GPT needs a funded OpenAI API account (a ChatGPT
  subscription does not include API credits -- they're billed separately).
- `docs/history/GUI_PROPOSAL.md`, `docs/history/LAUNCHER_PLAN.md`, and
  `docs/ARCHITECTURE.md` had not been reviewed against that night's changes
  and may still describe the earlier chat-extension-centric design.
- An embedded terminal panel for Claude Code / Codex inside Model Deck was
  discussed (real terminal via `QWebEngineView` + `xterm.js` + a pty bridge
  over `QWebSockets`, both confirmed available) but not built -- blocked on
  neither CLI being installed, and it's a separate capability from the
  four-phase pipeline, not an integration with it.
