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
            self.assertEqual(loaded["roles"]["builder"]["kv_quantization"], "q8")
            self.assertEqual(loaded["roles"]["builder"]["preserve_thinking"], "auto")

    def test_phase_activation_builds_expected_backend(self) -> None:
        state = default_state()
        activate_local(state, "builder")
        self.assertEqual(state["active"]["phase"], "builder")
        self.assertEqual(state["active"]["base_url"], "http://127.0.0.1:8002/v1")
        activate_planner(state)
        self.assertEqual(state["active"]["kind"], "openai")
        self.assertEqual(state["active"]["model_id"], "gpt-5.6-sol")

    def test_planner_activation_can_fall_back_to_a_local_role(self) -> None:
        state = default_state()
        state["planner"]["kind"] = "local"
        state["planner"]["local_phase"] = "auditor"
        activate_planner(state)
        self.assertEqual(state["active"]["phase"], "planner")
        self.assertEqual(state["active"]["kind"], "local")
        self.assertEqual(state["active"]["model_id"], "auditor")
        self.assertEqual(state["active"]["base_url"], "http://127.0.0.1:8004/v1")


class CommandTests(unittest.TestCase):
    def test_model_command_contains_memory_and_cache_contract(self) -> None:
        role = default_state()["roles"]["scout"]
        command, environment = model_command("scout", role, Path("/mtplx"))
        self.assertIn("100000", command)
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
        quant_index = command.index("--paged-kv-quantization")
        self.assertEqual(command[quant_index + 1], "q8")
        preserve_index = command.index("--preserve-thinking")
        self.assertEqual(command[preserve_index + 1], "auto")


if __name__ == "__main__":
    unittest.main()
