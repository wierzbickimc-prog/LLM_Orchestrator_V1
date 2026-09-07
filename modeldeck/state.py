from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_SCOUT = "Youssofal/Qwen3.6-35B-A3B-MTPLX-Optimized-Speed-FP16"
DEFAULT_BUILDER = "Youssofal/Qwen3.8-27B-MTPLX-Optimized-Speed-FP16"

# Officially published sampling parameters per model card -- these are not
# guesses, and generic defaults (e.g. temperature=0.7/top_p=0.9 for
# everything) are measurably wrong for either model in either mode. "instruct"
# is the non-thinking preset both models publish; "thinking" is each model's
# general-purpose thinking preset; "thinking_precise" is Qwen3.6's separate
# coding-tuned thinking variant (3.8 does not publish a separate one).
SAMPLING_PRESETS: dict[str, dict[str, dict[str, float]]] = {
    "Qwen3.8-27B": {
        "instruct": {
            "temperature": 0.7, "top_p": 0.80, "top_k": 20, "min_p": 0.0,
            "presence_penalty": 1.5, "repetition_penalty": 1.0,
        },
        "thinking": {
            "temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
            "presence_penalty": 0.0, "repetition_penalty": 1.0,
        },
    },
    "Qwen3.6-35B": {
        "instruct": {
            "temperature": 0.7, "top_p": 0.80, "top_k": 20, "min_p": 0.0,
            "presence_penalty": 1.5, "repetition_penalty": 1.0,
        },
        "thinking": {
            "temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
            "presence_penalty": 1.5, "repetition_penalty": 1.0,
        },
        "thinking_precise": {
            "temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
            "presence_penalty": 0.0, "repetition_penalty": 1.0,
        },
    },
}

_GENERIC_SAMPLING_FALLBACK = {
    "temperature": 0.7, "top_p": 0.9, "top_k": 40, "min_p": 0.0,
    "presence_penalty": 0.0, "repetition_penalty": 1.0,
}


def model_family(model_repo_id: str) -> str | None:
    if "Qwen3.8-27B" in model_repo_id:
        return "Qwen3.8-27B"
    if "Qwen3.6-35B" in model_repo_id:
        return "Qwen3.6-35B"
    return None


def sampling_preset(model_repo_id: str, mode: str) -> dict[str, float] | None:
    """The officially published sampling parameters for this model+mode, or
    None if this model doesn't publish one for that mode (e.g. 3.8 has no
    "thinking_precise") -- callers should tell the user rather than silently
    substituting something else in that case."""
    family = model_family(model_repo_id)
    if family is None:
        return None
    return SAMPLING_PRESETS.get(family, {}).get(mode)


def config_dir() -> Path:
    override = os.getenv("MODEL_DECK_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "Model Deck"


def state_path() -> Path:
    return config_dir() / "state.json"


def default_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "active": {
            "phase": "scout",
            "kind": "local",
            "base_url": "http://127.0.0.1:8000/v1",
            "model_id": "scout",
        },
        "planner": {
            # "kind": "local" reuses one of the local roles below (via
            # local_phase) instead of the cloud GPT backend -- a fallback
            # for when the OpenAI account has no credits, or you'd rather
            # not spend them on planning.
            # Points at "renovator" (the dense model), not "builder", since
            # Builder moved to the MoE: planning is the step with the least
            # downstream error correction in this pipeline (Builder
            # implements the plan faithfully and Auditor checks compliance
            # *with* the plan, so nobody questions the plan's premises),
            # which makes it the worst place to economize on capability.
            "kind": "openai",
            "local_phase": "renovator",
            "model": "gpt-5.6-sol",
            "base_url": "https://api.openai.com/v1",
            "reasoning_effort": "high",
        },
        "roles": {
            # scout is intentionally left at the shared kv_quantization="q8"
            # and preserve_thinking="auto" defaults below. We tried deviating
            # from both while chasing a late-session coherence drift (the
            # model re-deriving an already-resolved conclusion past ~50
            # rounds / 55k tokens):
            #   - preserve_thinking="on" overrode Qwen 3.6's trained "scoped"
            #     reasoning contract and caused garbled output on the very
            #     first generation of a brand-new session ("stop_token_
            #     boundary_mismatch"). Reverted.
            #   - kv_quantization="off" (motivated by ample free RAM, on the
            #     assumption unquantized == safer) instead triggered mtplx's
            #     own internal repetition mitigation ("repetition stream
            #     holdback engaged") -- degenerate token-repeat gibberish --
            #     on more than one fresh session. Reverted; q8 is evidently
            #     the well-exercised path for this model's speculative-MTP
            #     decode/verify pipeline, not just a memory optimization.
            # Don't change either without isolated, repeated testing outside
            # a live task -- both "improvements" caused worse regressions
            # than the drift they were meant to fix.
            # sampling_mode picks each role's officially published sampling
            # preset (see SAMPLING_PRESETS) -- "instruct" pairs with
            # reasoning="off" (no thinking, so use the non-thinking preset),
            # "thinking"/"thinking_precise" pair with reasoning="auto"/"on".
            # Toggle per-role in the GUI (Deck tab -> role editor -> Apply
            # preset) if a task calls for a different mode than the default.
            # context_window 131_072 (128K): was ~100K, raised after a live
            # Builder run hit 95K/104K prompt tokens by step 25 on a
            # multi-file task (several full-file reads plus their own large
            # write_file bodies accumulate fast). Confirmed real RAM
            # headroom (64G box, ~20G resident for weights+context) before
            # raising. Deliberately not raised further than this: a bigger
            # window costs more prefill time per request regardless of
            # whether it's filled, so this is a real speed/room tradeoff,
            # not a free upgrade -- 128K covers the observed case with
            # headroom without chasing an unbounded ceiling.
            "scout": _role(
                DEFAULT_SCOUT, 8000, "auto", "medium", 131_072, 3,
                sampling_mode="thinking",
            ),
            # Builder on the MoE (~3B active params/token): the bulk of a
            # build is mechanical volume -- read a file, write it back,
            # run the tests -- and Builder's errors are among the cheapest
            # in the pipeline to catch, since Auditor reads the whole tree
            # afterward and a REJECT costs one Renovator pass, not a
            # rebuild. Paired with the dense model on Renovator below: fast
            # bulk pass, expert cleanup. UNVALIDATED as of this change --
            # every tool-call pathology seen so far (missing/stripped
            # ```tool fences) was on the dense model's "tokenizer" chat
            # template, and we have no evidence either way about how the
            # MoE's "local_qwen36" template behaves in a long tool-use
            # loop, because scout/auditor never emit tool calls. Benchmark
            # before trusting it -- see docs/IMPROVEMENTS_TODO.md.
            "builder": _role(
                DEFAULT_SCOUT, 8002, "off", "auto", 131_072, 3,
                sampling_mode="instruct",
            ),
            # Renovator gets its own role rather than reusing Builder's, so
            # the two can run different models. Deliberately left on
            # reasoning="off" + instruct, matching Builder's proven
            # tool-loop config: the point of this split is to test the
            # *model* variable on its own. Thinking mode here is a separate
            # question worth its own benchmark -- reasoning tokens in a
            # tool-call loop are exactly the kind of interaction that has
            # bitten this project before, so don't change both at once.
            "renovator": _role(
                DEFAULT_BUILDER, 8006, "off", "auto", 131_072, 3,
                sampling_mode="instruct",
            ),
            # Auditor's actual job is breadth (scan the whole tree for
            # out-of-scope changes), not narrow depth on a few files -- that's
            # exactly what scout's MoE model (~3B active params/token) is
            # built for, and it's markedly faster at a full-tree scan than
            # the dense 27B model here previously. Reuses scout's model on a
            # separate port/session-bank so scout and auditor can still run
            # as distinct resident processes if ever needed concurrently.
            # sampling_mode="thinking_precise": auditor's job is close code
            # review, which is exactly what Qwen3.6's coding-tuned thinking
            # preset is for, as opposed to scout's more general investigation.
            "auditor": _role(
                DEFAULT_SCOUT, 8004, "auto", "medium", 131_072, 3,
                sampling_mode="thinking_precise",
            ),
        },
        "router": {"host": "127.0.0.1", "port": 8100},
        # Operator edits to the chat-client phase-injection prompts below (Admin
        # tab) -- keyed by phase, only present once a prompt has actually
        # been edited and saved. Empty/absent means "use the built-in
        # default in modeldeck/prompts.py" -- see effective_prompt(). This
        # has no effect on scripts/*_report.py, which carry their own
        # hardcoded system prompts and never go through phase injection
        # (they send X-Model-Deck-Skip-Injection).
        "prompt_overrides": {},
    }


def _role(
    model: str,
    port: int,
    reasoning: str,
    reasoning_effort: str,
    context_window: int,
    depth: int,
    kv_quantization: str = "q8",
    preserve_thinking: str = "auto",
    sampling_mode: str = "thinking",
) -> dict[str, Any]:
    sampling = sampling_preset(model, sampling_mode) or dict(_GENERIC_SAMPLING_FALLBACK)
    return {
        "model": model,
        "port": port,
        "context_window": context_window,
        "kv_quantization": kv_quantization,
        "depth": depth,
        "reasoning": reasoning,
        "reasoning_effort": reasoning_effort,
        "preserve_thinking": preserve_thinking,
        # Sent as per-request sampling parameters by the router (see
        # router/main.py's local-role backend resolution) -- not a launch
        # flag, so changing these doesn't require restarting the model.
        # Values come from each model's officially published preset for
        # sampling_mode (see SAMPLING_PRESETS) rather than generic guesses.
        "sampling_mode": sampling_mode,
        "temperature": sampling["temperature"],
        "top_p": sampling["top_p"],
        "top_k": sampling["top_k"],
        "min_p": sampling["min_p"],
        "presence_penalty": sampling["presence_penalty"],
        "repetition_penalty": sampling["repetition_penalty"],
        # 4G per-session held ~61k tokens of KV at the default 65536 B/token
        # density (4e9 / 65536), just short of the 100k context window --
        # long scout/builder runs would blow past it, evict, and lose the
        # cache. 12G covers the full window with headroom.
        "ram_cache_total": "16G",
        "ram_cache_per_session": "12G",
        "ram_cache_entries": 8,
        "ssd_cache": "10G",
        # Off by design, not a placeholder -- confirmed empirically that
        # mtplx's SSD/cold-tier session cache (~/.mtplx/session-bank) is
        # global and content-addressed, not scoped per conversation or per
        # process. Scout/Planner/Builder/Auditor/Renovator all read
        # overlapping repository files with near-identical formatting, so a
        # brand-new request can get an "exact_prefix" match against a
        # completely unrelated earlier conversation's cached tokens and
        # silently continue from someone else's context -- reproduced live:
        # a fresh Planner call inherited a 35,943-token prefix from an
        # unrelated prior Builder/Renovator session and stopped after 53
        # garbled tokens. RAM-only session reuse (this process's own
        # lifetime, cleared on every relaunch) doesn't have this problem.
        "ssd_session_cache": "off",
        "fan_mode": "smart",
        "profile": "turbo",
        "prefill_chunk_tokens": 2048,
    }


def _merge(default: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(default)
    for key, value in saved.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_state(path: Path | None = None) -> dict[str, Any]:
    target = path or state_path()
    try:
        saved = json.loads(target.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default_state()
    if not isinstance(saved, dict):
        return default_state()
    return _merge(default_state(), saved)


def save_state(state: dict[str, Any], path: Path | None = None) -> Path:
    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, target)
    return target


def activate_local(state: dict[str, Any], phase: str) -> dict[str, Any]:
    role = state["roles"][phase]
    state["active"] = {
        "phase": phase,
        "kind": "local",
        "base_url": f"http://127.0.0.1:{int(role['port'])}/v1",
        "model_id": phase,
    }
    return state


def activate_planner(state: dict[str, Any]) -> dict[str, Any]:
    planner = state["planner"]
    if planner.get("kind") == "local":
        local_phase = str(planner.get("local_phase") or "builder")
        role = state["roles"][local_phase]
        state["active"] = {
            "phase": "planner",
            "kind": "local",
            "base_url": f"http://127.0.0.1:{int(role['port'])}/v1",
            "model_id": local_phase,
        }
        return state
    state["active"] = {
        "phase": "planner",
        "kind": "openai",
        "base_url": str(planner["base_url"]).rstrip("/"),
        "model_id": str(planner["model"]),
        "reasoning_effort": str(planner["reasoning_effort"]),
    }
    return state
