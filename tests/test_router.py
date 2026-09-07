from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from router.main import (
    backend_for_model,
    inject_phase_instructions,
    prepare_payload,
    wants_injection_skipped,
)
from router.settings import Backend
from modeldeck.state import activate_local, activate_planner, default_state, save_state


class RoutingTests(unittest.TestCase):
    def test_aliases_resolve_to_configured_backends(self) -> None:
        self.assertEqual(backend_for_model("scout").alias, "scout")
        self.assertEqual(backend_for_model("builder").alias, "builder")

    def test_unknown_alias_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as context:
            backend_for_model("unknown")
        self.assertEqual(context.exception.status_code, 404)

    def test_local_alias_reads_active_local_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = activate_local(default_state(), "builder")
            save_state(state, Path(directory) / "state.json")
            with patch.dict("os.environ", {"MODEL_DECK_CONFIG_DIR": directory}):
                backend = backend_for_model("local")
        self.assertEqual(backend.model_id, "builder")
        self.assertEqual(backend.base_url, "http://127.0.0.1:8002/v1")

    def test_local_alias_hides_planner_key_behind_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = activate_planner(default_state())
            save_state(state, Path(directory) / "state.json")
            with (
                patch.dict("os.environ", {"MODEL_DECK_CONFIG_DIR": directory}),
                patch("router.main.get_openai_api_key", return_value="secret"),
            ):
                backend = backend_for_model("local")
        self.assertEqual(backend.provider, "openai")
        self.assertEqual(backend.model_id, "gpt-5.6-sol")
        self.assertEqual(backend.api_key, "secret")

    def test_planner_alias_is_directly_addressable_regardless_of_active_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # Active phase is scout, not planner -- the direct alias must not
            # depend on Model Deck's GUI-managed "active" state.
            state = activate_local(default_state(), "scout")
            save_state(state, Path(directory) / "state.json")
            with (
                patch.dict("os.environ", {"MODEL_DECK_CONFIG_DIR": directory}),
                patch("router.main.get_openai_api_key", return_value="secret"),
            ):
                backend = backend_for_model("planner")
        self.assertEqual(backend.provider, "openai")
        self.assertEqual(backend.model_id, "gpt-5.6-sol")
        self.assertEqual(backend.api_key, "secret")

    def test_planner_alias_requires_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            save_state(default_state(), Path(directory) / "state.json")
            with (
                patch.dict("os.environ", {"MODEL_DECK_CONFIG_DIR": directory}),
                patch("router.main.get_openai_api_key", return_value=None),
            ):
                with self.assertRaises(HTTPException) as context:
                    backend_for_model("planner")
        self.assertEqual(context.exception.status_code, 503)

    def test_planner_alias_uses_local_role_when_configured_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = default_state()
            state["planner"]["kind"] = "local"
            save_state(state, Path(directory) / "state.json")
            with patch.dict("os.environ", {"MODEL_DECK_CONFIG_DIR": directory}):
                # No API key needed, and no api_key patch supplied -- if this
                # accidentally fell through to the cloud branch it would 503.
                backend = backend_for_model("planner")
        self.assertEqual(backend.provider, "local")
        self.assertIsNone(backend.api_key)
        self.assertEqual(backend.model_id, "planner")
        self.assertEqual(backend.base_url, "http://127.0.0.1:8008/v1")

    def test_auditor_alias_is_directly_addressable_regardless_of_active_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = activate_local(default_state(), "scout")
            save_state(state, Path(directory) / "state.json")
            with patch.dict("os.environ", {"MODEL_DECK_CONFIG_DIR": directory}):
                backend = backend_for_model("auditor")
        self.assertEqual(backend.model_id, "auditor")
        self.assertEqual(backend.base_url, "http://127.0.0.1:8004/v1")

    def test_scout_overrides_thinking_controls(self) -> None:
        backend = Backend(
            alias="scout",
            base_url="http://127.0.0.1:8000/v1",
            model_id="physical-scout",
            reasoning="off",
            enable_thinking=False,
        )
        payload = prepare_payload(
            {"model": "scout", "reasoning": "on", "enable_thinking": True},
            backend,
        )
        self.assertEqual(payload["model"], "physical-scout")
        self.assertEqual(payload["reasoning"], "off")
        self.assertIs(payload["enable_thinking"], False)

    def test_builder_preserves_client_reasoning_controls(self) -> None:
        backend = Backend(
            alias="builder",
            base_url="http://127.0.0.1:8002/v1",
            model_id="physical-builder",
        )
        payload = prepare_payload(
            {"model": "builder", "reasoning": "auto", "enable_thinking": True},
            backend,
        )
        self.assertEqual(payload["model"], "physical-builder")
        self.assertEqual(payload["reasoning"], "auto")
        self.assertIs(payload["enable_thinking"], True)

    def test_openai_payload_uses_gateway_reasoning_contract(self) -> None:
        backend = Backend(
            alias="local",
            base_url="https://api.openai.com/v1",
            model_id="gpt-5.6-sol",
            api_key="secret",
            reasoning_effort="high",
            provider="openai",
        )
        payload = prepare_payload(
            {
                "model": "local",
                "reasoning": "auto",
                "enable_thinking": True,
                "top_k": 20,
            },
            backend,
        )
        self.assertEqual(payload["model"], "gpt-5.6-sol")
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertNotIn("reasoning", payload)
        self.assertNotIn("enable_thinking", payload)
        self.assertNotIn("top_k", payload)


class PhaseInstructionInjectionTests(unittest.TestCase):
    def test_appends_to_existing_system_message(self) -> None:
        payload = {
            "messages": [
                {"role": "system", "content": "You are an AI coding assistant."},
                {"role": "user", "content": "hi"},
            ]
        }
        result = inject_phase_instructions(payload, "scout")
        self.assertEqual(len(result["messages"]), 2)
        content = result["messages"][0]["content"]
        self.assertTrue(content.startswith("You are an AI coding assistant."))
        self.assertIn("scout-report.md", content)

    def test_inserts_system_message_when_absent(self) -> None:
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        result = inject_phase_instructions(payload, "builder")
        self.assertEqual(len(result["messages"]), 2)
        self.assertEqual(result["messages"][0]["role"], "system")
        self.assertIn("builder-report.md", result["messages"][0]["content"])

    def test_unknown_phase_is_left_untouched(self) -> None:
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(inject_phase_instructions(payload, "unknown-phase"), payload)


class _FakeRequest:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class InjectionSkipHeaderTests(unittest.TestCase):
    def test_recognizes_truthy_values(self) -> None:
        for value in ("1", "true", "True", "TRUE"):
            self.assertTrue(wants_injection_skipped(_FakeRequest({"x-modeldeck-skip-injection": value})))

    def test_defaults_to_false(self) -> None:
        self.assertFalse(wants_injection_skipped(_FakeRequest({})))
        self.assertFalse(wants_injection_skipped(_FakeRequest({"x-modeldeck-skip-injection": "0"})))


if __name__ == "__main__":
    unittest.main()


class ChatAliasTests(unittest.TestCase):
    def test_chat_alias_is_its_own_role_on_its_own_port(self) -> None:
        # The Chat tab must not borrow a pipeline role: loading a drafting
        # model has to be possible without perturbing the tuned phases.
        with tempfile.TemporaryDirectory() as directory:
            state = activate_local(default_state(), "scout")
            save_state(state, Path(directory) / "state.json")
            with patch.dict("os.environ", {"MODEL_DECK_CONFIG_DIR": directory}):
                backend = backend_for_model("chat")
        self.assertEqual(backend.model_id, "chat")
        self.assertEqual(backend.base_url, "http://127.0.0.1:8010/v1")
        self.assertEqual(backend.provider, "local")
