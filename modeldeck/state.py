from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_SCOUT = "Youssofal/Qwen3.6-35B-A3B-MTPLX-Optimized-Speed-FP16"
DEFAULT_BUILDER = "Youssofal/Qwen3.8-27B-MTPLX-Optimized-Speed-FP16"
# Same MoE class as DEFAULT_SCOUT (35B total, ~3B active), benchmarked
# head-to-head against it -- see docs/IMPROVEMENTS_TODO.md for the protocol.
DEFAULT_ORNITH = "philipjohnbasile/ornith-ai-Ornith-1.5-35B-A3B-V2-MTPLX"
# Chosen for Builder/Auditor and Planner/Renovator respectively after the
# full 5-model x 2-convention benchmark on the deliberately harder
# structural-save task (see docs/IMPROVEMENTS_TODO.md). Not the "Speed"
# variant of either family: on the same task, in the same mode, the
# non-Speed variant of both families won by a real margin -- faster AND
# fewer self-inflicted errors to recover from, not a tradeoff between the
# two. Qwen3.6 Balance ran clean in one shot (247s); Speed needed a retry
# after an ask_question crash and took 373s. Qwen3.8 Quality was faster in
# both tool-calling conventions tested (1,072s/772s vs. Speed's
# 1,963s/1,517s) and had zero self-correction incidents across both runs;
# Speed's native run corrupted three pre-existing tests during a rewrite
# and had to recover via git diff/git checkout.
DEFAULT_QWEN36_BALANCE = "Youssofal/Qwen3.6-35B-A3B-MTPLX-Optimized-Balance-FP16"
DEFAULT_QWEN38_QUALITY = "Youssofal/Qwen3.8-27B-MTPLX-Optimized-Quality-FP16"

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

# Ornith-1.5-35B-A3B publishes its own preset numbers, but they are
# numerically identical to Qwen3.6-35B's across all three modes (confirmed
# by comparing the published values directly, not assumed from the shared
# Qwen lineage). Aliased to the same dict object rather than duplicated, so
# the two can never silently drift apart the way DEFAULT_CHAR_BUDGET did
# when nothing forced it to track context_window -- see
# scripts/report_common.py's char_budget_for_role docstring for that
# incident. sampling_preset()/model_family() never mutate these dicts, so
# sharing one object across two families is safe.
#
# One real published difference, not captured by these three named modes:
# Ornith has no way to fully disable reasoning (it always opens with
# <think>), so there is no equivalent of an "off"-paired instruct mode the
# way Qwen's instruct preset pairs with reasoning="off". Builder/Renovator
# roles assigned to Ornith will run with reasoning on regardless of which
# sampling_mode is selected -- a real cost/quality tradeoff to watch in the
# benchmark, not a configuration bug to fix.
SAMPLING_PRESETS["Ornith-1.5-35B"] = SAMPLING_PRESETS["Qwen3.6-35B"]

_GENERIC_SAMPLING_FALLBACK = {
    "temperature": 0.7, "top_p": 0.9, "top_k": 40, "min_p": 0.0,
    "presence_penalty": 0.0, "repetition_penalty": 1.0,
}


def model_family(model_repo_id: str) -> str | None:
    if "Qwen3.8-27B" in model_repo_id:
        return "Qwen3.8-27B"
    if "Qwen3.6-35B" in model_repo_id:
        return "Qwen3.6-35B"
    if "Ornith-1.5-35B" in model_repo_id:
        return "Ornith-1.5-35B"
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
            # "kind": "local" runs the planner on its own role below
            # (roles["planner"], its own model/port/sampling), rather than
            # borrowing another phase's role as it used to via
            # "local_phase". Planning is the step with the least downstream
            # error correction in this pipeline -- Builder implements the
            # plan faithfully and Auditor checks compliance *with* the plan,
            # so nothing downstream questions its premises -- which makes it
            # the worst place to economize, and the one most worth being
            # able to tune independently of any other phase.
            "kind": "openai",
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
            # Qwen3.6 Balance, native tool-calling, MoE-correct serving
            # parameters -- see DEFAULT_QWEN36_BALANCE's comment for the
            # benchmark this is based on. profile="sustained" (not "turbo":
            # this model's own recommended_profile, confirmed via mtplx's
            # installed_models() -- turbo is compiled/verified against the
            # dense 27B/9B flagships specifically, not this architecture).
            # kv_quantization="off": MoE models are reportedly more
            # sensitive to KV-cache quantization than dense ones, and the
            # cost is cheap here -- this architecture's hybrid
            # linear/full-attention design (full_attention_interval=4, only
            # 2 KV heads) keeps full-precision KV under ~2.5GB even at
            # 131K context, not the tens of GB a classic dense-attention
            # model would need. depth=1: mtplx's own `tune` data (see
            # docs/DEPTH_TUNING.md) shows deeper speculation actively hurts
            # this model -- depth 3's third-position acceptance craters to
            # ~1-30%, making depth 3 slower than plain autoregressive
            # decode, not just slower than depth 1.
            # native_tool_calling=True: this model is trained for tool use,
            # and forcing it onto the hand-rolled ```tool convention meant
            # for a different model is a real cost, not a neutral default
            # -- see the native_tool_calling comment in _role() below.
            "builder": _role(
                DEFAULT_QWEN36_BALANCE, 8002, "auto", "medium", 131_072, 1,
                kv_quantization="off", sampling_mode="thinking_precise",
                max_steps=80, native_tool_calling=True,
            ),
            # Renovator gets its own role rather than reusing Builder's, so
            # the two can run different models -- Qwen3.8 Quality here,
            # same benchmark as Builder's Qwen3.6 Balance choice (see
            # DEFAULT_QWEN38_QUALITY's comment). profile="turbo" (this
            # family's own recommended_profile, unlike Qwen3.6's), depth=3
            # (mtplx tune: Quality's third-position acceptance holds at
            # 92%, unlike the MoE models' collapse -- deeper speculation
            # actually pays off here). kv_quantization="q8": dense models
            # weren't the ones flagged as KV-quant sensitive, and q8 halves
            # the KV footprint for no observed quality cost.
            # native_tool_calling=True: not explicitly benchmarked for
            # Renovator specifically, but Renovator reuses Builder's exact
            # loop, and Qwen3.8 Quality's own best Builder-role run in the
            # whole matrix (772s, zero self-correction incidents, found
            # the actual root cause of a real test-runner bug) was under
            # native mode. Revisit if a Renovator-specific run disagrees.
            # max_steps=80, matching Builder: 40 was not enough. A repair
            # pass hit the cap on a four-item fix list and reported
            # "stopped after 40 steps without the model signaling
            # completion" -- a repair is not intrinsically cheaper than the
            # original build, because it starts by re-reading files it did
            # not write in this session.
            "renovator": _role(
                DEFAULT_QWEN38_QUALITY, 8006, "off", "auto", 131_072, 3,
                kv_quantization="q8", sampling_mode="instruct",
                max_steps=80, native_tool_calling=True,
            ),
            # Auditor's actual job is breadth (scan the whole tree for
            # out-of-scope changes), not narrow depth on a few files -- that's
            # exactly what the MoE model (~3B active params/token) is
            # built for. Same model as Builder (Qwen3.6 Balance) for the
            # same benchmark-backed reasons -- see DEFAULT_QWEN36_BALANCE's
            # comment and the Builder role above. sustained/kv-off/depth=1
            # for the same MoE-architecture reasons as Builder.
            # native_tool_calling is meaningless here and deliberately left
            # unset: auditor_report.py is a one-shot call_model() request,
            # never the agentic tool loop -- there's no tool-calling
            # convention for this role to use at all, native or otherwise.
            # sampling_mode="thinking_precise": auditor's job is close code
            # review, which is exactly what Qwen3.6's coding-tuned thinking
            # preset is for, as opposed to scout's more general investigation.
            "auditor": _role(
                DEFAULT_QWEN36_BALANCE, 8004, "auto", "medium", 131_072, 1,
                kv_quantization="off", sampling_mode="thinking_precise",
            ),
            # Planner's own role (port 8008), no longer borrowing another
            # phase's. Qwen3.8 Quality for the same benchmark-backed reasons
            # as Renovator -- see DEFAULT_QWEN38_QUALITY's comment. Planning
            # is the step with the least downstream error correction in
            # this pipeline (Builder implements the plan faithfully,
            # Auditor checks compliance *with* the plan, so nothing
            # downstream questions its premises), which makes the fastest,
            # most error-free model of the pair the right one here too --
            # even though "fewer self-correction incidents" isn't directly
            # observable for a one-shot call the way it is for an agentic
            # loop, Quality's better result held in every mode tested.
            # turbo/kv-q8/depth=3 for the same dense-architecture reasons
            # as Renovator. native_tool_calling deliberately unset: like
            # Auditor, this is a one-shot planner_report.py/call_model()
            # request with no tool loop to route through it.
            "planner": _role(
                DEFAULT_QWEN38_QUALITY, 8008, "off", "auto", 131_072, 3,
                kv_quantization="q8", sampling_mode="instruct",
            ),
            # Not a pipeline phase: this backs the Chat tab, where a prompt
            # gets talked through and sharpened *before* it is handed to
            # Scout. Its own role and its own port because the whole value of
            # the tab is being able to think out loud against a fast,
            # conversational model without disturbing whatever the pipeline
            # roles are currently tuned to. reasoning="auto" + thinking
            # because drafting a spec is reasoning work, not tool work --
            # the opposite of the tool-loop roles above.
            "chat": _role(
                DEFAULT_SCOUT, 8010, "auto", "medium", 131_072, 3,
                sampling_mode="thinking",
            ),
            # Backs the Prompt development tab -- a pure prompt-drafting tool
            # with no filesystem awareness. Separate from the normal Chat tab
            # (roles["chat"]) so each can have independent model/port/sampling.
            "prompt_dev": _role(
                DEFAULT_SCOUT, 8012, "auto", "medium", 131_072, 3,
                sampling_mode="thinking",
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


# mtplx serve profiles: "turbo" is the compiled/verified kernel path built
# for the quantized dense 27B/9B flagships specifically; "sustained" is the
# long-context MTP path (chunked prefill, request-sized KV) that's the
# actual default for everything else, MoE included. Confirmed directly
# from mtplx's own installed_models() `recommended_profile` field per
# model, not inferred: every Qwen3.6-35B-A3B and Ornith-1.5-35B variant
# reports "sustained", every Qwen3.8-27B variant reports "turbo". This
# used to be a single hardcoded "turbo" default below regardless of model
# -- silently wrong for every MoE role (scout/chat/prompt_dev included)
# until that was caught. Deriving it from the family instead of a literal
# means it can't drift the same way again as new roles get added.
_PROFILE_BY_FAMILY = {
    "Qwen3.6-35B": "sustained",
    "Ornith-1.5-35B": "sustained",
    "Qwen3.8-27B": "turbo",
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
    max_steps: int = 0,
    native_tool_calling: bool = False,
    profile: str | None = None,
) -> dict[str, Any]:
    sampling = sampling_preset(model, sampling_mode) or dict(_GENERIC_SAMPLING_FALLBACK)
    resolved_profile = profile or _PROFILE_BY_FAMILY.get(model_family(model) or "", "turbo")
    return {
        "model": model,
        "port": port,
        # How many tool-loop turns this role gets before the safety cap
        # stops it. 0 means "not an agentic role" (scout/planner/auditor are
        # one-shot calls with no loop, so the number is meaningless there).
        # Surfaced in the Deck tab because running out of turns is a real,
        # observed failure mode that looks like a quality problem but isn't:
        # a Renovator pass hit its 40-turn cap mid-repair and reported
        # "stopped after 40 steps without the model signaling completion".
        "max_steps": max_steps,
        # False (default) keeps the hand-rolled ```tool convention this
        # project normally uses -- see report_common.stream_chat's
        # docstring for why native tool-calling isn't the default: it
        # broke going through mtplx's --tool-prompt-mode *hybrid* bridge
        # once already. True launches this role with --tool-prompt-mode
        # native --reasoning-parser qwen3 (see mtplx.py's model_command)
        # and routes Builder/Renovator through stream_chat_native instead
        # -- for a model actually trained on tool-calling (confirmed
        # working live for Ornith-1.5), the hand-rolled convention can be
        # the worse fit, not a neutral default. Meaningless for the
        # one-shot roles (scout/planner/auditor), same as max_steps.
        "native_tool_calling": native_tool_calling,
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
        "profile": resolved_profile,
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
        # Planner runs on its own role/port now, not on another phase's.
        role = state["roles"]["planner"]
        state["active"] = {
            "phase": "planner",
            "kind": "local",
            "base_url": f"http://127.0.0.1:{int(role['port'])}/v1",
            "model_id": "planner",
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
