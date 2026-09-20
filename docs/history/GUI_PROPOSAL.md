# Historical Model Deck desktop workflow

> This describes the original **Deck** tab (still accurate, below). Model
> Deck later gained a second **Reports** tab that runs the four-phase
> pipeline directly against the router with no chat extension involved -- see
> the repository README's "The routed pipeline" section and
> `../ARCHITECTURE.md` for the current design.

Model Deck is a native macOS control plane for a chat client, MTPLX, and an
OpenAI GPT Planner. The client is configured once with the local gateway and
never needs to know
which inference backend is active.

```text
Client: http://127.0.0.1:8100/v1 · model local
                         |
                    Model Deck
       +-----------------+------------------+
       |                 |                  |
  GPT-5.6 Sol       MTPLX Scout        MTPLX Builder/Auditor
    Planner          35B-A3B                 selected model
```

## Phase contract

| Phase | Default engine | Required output |
| --- | --- | --- |
| Scout | Qwen3.6-35B-A3B, medium reasoning | `.ai/scout-report.md` |
| Planner | GPT-5.6 Sol, high reasoning | `.ai/implementation-plan.md` |
| Builder | Qwen3.8-27B, reasoning off | code, tests, `.ai/builder-report.md` |
| Auditor | Qwen3.8-27B, medium reasoning | `.ai/audit-report.md` |

Only one local model is resident. Activating Planner also releases the local
model so GPT planning does not compete for unified memory. Every local role can
be assigned any valid MTPLX pack discovered by `mtplx models --json`.

## Main window

The left side contains:

- the stable client endpoint and current phase;
- one-click Scout, GPT Planner, Build, and Audit transitions;
- the requested **Launch Models** button;
- GPT model/reasoning selection and Keychain credential status;
- Scout, Builder, and Auditor model selectors;
- context, reasoning, and MTP-depth settings;
- live prefill/decode rates, TTFT, memory, token cache, context use, per-depth
  acceptance probabilities, session cache, thermal status, and KV mode.

The right side is permanent workflow memory. It shows the phase diagram, the
document expected from each role, and a copyable request block for every phase.

## Data sources

- Model inventory: `mtplx models --json`
- Launch-time capabilities and settings: `/health` and `/v1/mtplx/settings`
- Completed request statistics: `/metrics`
- In-flight request status: `/v1/mtplx/flight`
- OpenAI Planner: proxied Chat Completions through the same local gateway
- OpenAI credential: macOS Keychain, service `Model Deck OpenAI API`
- Saved workflow state: `~/Library/Application Support/Model Deck/state.json`

## Safety and ownership

- Model Deck binds the router and model servers to loopback only.
- It stops a model only when the listener PID matches a PID recorded by this
  project.
- It refuses to replace an unknown process occupying a configured port.
- GPT credentials never enter client payloads, project files, or logs.
- Restart-required settings are applied during phase launch, never silently in
  the middle of a request.

## Delivery stages

1. Desktop shell, selectors, phase switching, Keychain, stable `local` routing,
   telemetry, and workflow prompts.
2. Live in-flight charts and gateway-side GPT usage/cost telemetry.
3. Presets, repo/document readiness checks, and explicit handoff validation.
4. Signed/notarized standalone packaging after the workflow stabilizes.
