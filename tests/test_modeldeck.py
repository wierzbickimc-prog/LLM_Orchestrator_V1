from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from modeldeck.mtplx import model_command
from modeldeck.state import (
    activate_local,
    activate_planner,
    default_state,
    load_state,
    save_state,
)


class StateTests(unittest.TestCase):
    def test_round_trip_and_default_merge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = default_state()
            state["planner"]["model"] = "gpt-test"
            save_state(state, path)
            loaded = load_state(path)
            self.assertEqual(loaded["planner"]["model"], "gpt-test")
            self.assertEqual(loaded["roles"]["scout"]["kv_quantization"], "q8")
            self.assertEqual(loaded["roles"]["scout"]["preserve_thinking"], "auto")
            # Builder is now the MoE-family choice (Qwen3.6 Balance), kv off
            # by design -- see the benchmark-backed comment in state.py.
            self.assertEqual(loaded["roles"]["builder"]["kv_quantization"], "off")
            self.assertEqual(loaded["roles"]["builder"]["preserve_thinking"], "auto")

    def test_phase_activation_builds_expected_backend(self) -> None:
        state = default_state()
        activate_local(state, "builder")
        self.assertEqual(state["active"]["phase"], "builder")
        self.assertEqual(state["active"]["base_url"], "http://127.0.0.1:8002/v1")
        activate_planner(state)
        self.assertEqual(state["active"]["kind"], "openai")
        self.assertEqual(state["active"]["model_id"], "gpt-5.6-sol")

    def test_planner_activation_uses_the_planners_own_role_when_local(self) -> None:
        # Not another phase's role: the planner has its own model, port and
        # sampling, so it can be tuned without dragging a tool-loop phase
        # along with it.
        state = default_state()
        state["planner"]["kind"] = "local"
        activate_planner(state)
        self.assertEqual(state["active"]["phase"], "planner")
        self.assertEqual(state["active"]["kind"], "local")
        self.assertEqual(state["active"]["model_id"], "planner")
        self.assertEqual(state["active"]["base_url"], "http://127.0.0.1:8008/v1")

    def test_agentic_roles_carry_a_turn_budget_and_one_shot_roles_do_not(self) -> None:
        # max_steps is what the pipeline passes as --max-steps, and 0 is the
        # sentinel for "this role has no tool loop at all".
        roles = default_state()["roles"]
        self.assertGreater(roles["builder"]["max_steps"], 0)
        self.assertGreater(roles["renovator"]["max_steps"], 0)
        for phase in ("scout", "planner", "auditor", "chat"):
            self.assertEqual(roles[phase]["max_steps"], 0, phase)

    def test_every_role_has_a_distinct_port(self) -> None:
        # Two roles on one port silently means "launching B killed A".
        ports = [role["port"] for role in default_state()["roles"].values()]
        self.assertEqual(len(ports), len(set(ports)))


class CommandTests(unittest.TestCase):
    def test_model_command_contains_memory_and_cache_contract(self) -> None:
        role = default_state()["roles"]["scout"]
        command, environment = model_command("scout", role, Path("/mtplx"))
        self.assertIn("131072", command)
        self.assertIn("10G", command)
        self.assertEqual(environment["MTPLX_SESSION_BANK_MAX_BYTES"], "16G")
        self.assertEqual(environment["MTPLX_SESSION_BANK_PER_SESSION_BYTES"], "12G")
        self.assertEqual(environment["MTPLX_SESSION_BANK_MAX_ENTRIES"], "8")
        # Both stay at the shared defaults -- see modeldeck/state.py for why
        # deviating from either regressed badly for this model/runtime combo.
        quant_index = command.index("--paged-kv-quantization")
        self.assertEqual(command[quant_index + 1], "q8")
        preserve_index = command.index("--preserve-thinking")
        self.assertEqual(command[preserve_index + 1], "auto")

    def test_builder_keeps_shared_cache_and_thinking_defaults(self) -> None:
        role = default_state()["roles"]["builder"]
        command, _ = model_command("builder", role, Path("/mtplx"))
        # kv off, not q8: Builder is the MoE-family choice (Qwen3.6 Balance),
        # benchmarked to prefer full-precision KV -- see state.py's comment.
        quant_index = command.index("--paged-kv-quantization")
        self.assertEqual(command[quant_index + 1], "off")
        preserve_index = command.index("--preserve-thinking")
        self.assertEqual(command[preserve_index + 1], "auto")

    def test_profile_derives_from_model_family_not_a_flat_default(self) -> None:
        # This used to be a single hardcoded "turbo" for every role
        # regardless of model -- silently wrong for every MoE role
        # (scout/chat/prompt_dev included) until caught. Pin the fix: MoE
        # families get "sustained" (their own recommended_profile, per
        # mtplx's installed_models()), the dense family gets "turbo".
        roles = default_state()["roles"]
        self.assertEqual(roles["scout"]["profile"], "sustained")
        self.assertEqual(roles["builder"]["profile"], "sustained")
        self.assertEqual(roles["auditor"]["profile"], "sustained")
        self.assertEqual(roles["renovator"]["profile"], "turbo")
        self.assertEqual(roles["planner"]["profile"], "turbo")


if __name__ == "__main__":
    unittest.main()
