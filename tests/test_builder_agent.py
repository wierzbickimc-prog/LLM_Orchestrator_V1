from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import builder_agent  # noqa: E402


def _canned_responses(*responses):
    calls = iter(responses)

    def fake_stream_chat(model_alias, messages, on_chunk=None, router_url="", timeout=0, max_tokens=None):
        return next(calls)

    return fake_stream_chat


class RunAgentTests(unittest.TestCase):
    def test_reads_writes_then_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text("x = 1")

            responses = _canned_responses(
                'Let me look.\n```tool\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```',
                'Now updating it.\n```tool\n{"name": "write_file", "arguments": {"path": "a.py", "content": "x = 2"}}\n```',
                "Done. Changed a.py from x = 1 to x = 2 as the plan asked.",
            )
            with patch.object(builder_agent, "stream_chat", responses):
                report, steps, touched = builder_agent.run_agent(
                    root, plan="Change x to 2 in a.py.", task="", max_steps=10,
                    command_timeout=30.0, router_url="unused", timeout=30.0,
                    dry_run=False, on_chunk=lambda _p: None,
                )

            self.assertEqual(steps, 3)
            self.assertIn("Done.", report)
            self.assertEqual((root / "a.py").read_text(), "x = 2")
            self.assertEqual(touched, ["a.py"])

    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text("x = 1")

            responses = _canned_responses(
                '```tool\n{"name": "write_file", "arguments": {"path": "a.py", "content": "x = 2"}}\n```',
                "Done.",
            )
            with patch.object(builder_agent, "stream_chat", responses):
                _report, _steps, touched = builder_agent.run_agent(
                    root, plan="p", task="", max_steps=10, command_timeout=30.0,
                    router_url="unused", timeout=30.0, dry_run=True,
                    on_chunk=lambda _p: None,
                )
            self.assertEqual((root / "a.py").read_text(), "x = 1")
            self.assertEqual(touched, [])

    def test_invalid_tool_call_gets_fed_back_instead_of_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            responses = _canned_responses(
                "```tool\n{not valid json\n```",
                "Done, gave up on that approach.",
            )
            with patch.object(builder_agent, "stream_chat", responses):
                report, steps, _touched = builder_agent.run_agent(
                    root, plan="p", task="", max_steps=10, command_timeout=30.0,
                    router_url="unused", timeout=30.0, dry_run=False,
                    on_chunk=lambda _p: None,
                )
            self.assertEqual(steps, 2)
            self.assertIn("Done", report)

    def test_mtplx_injected_tool_failure_is_retried_not_mistaken_for_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            responses = _canned_responses(
                'I\'ll start by checking the repo.</tool_call>[MTPLX: this reply tried to '
                'call "list_files", but no tools are active on this request...]',
                "Done, implemented directly without exploring first.",
            )
            with patch.object(builder_agent, "stream_chat", responses):
                report, steps, _touched = builder_agent.run_agent(
                    root, plan="p", task="", max_steps=10, command_timeout=30.0,
                    router_url="unused", timeout=30.0, dry_run=False,
                    on_chunk=lambda _p: None,
                )
            # Without the fix this would return step 1's garbled MTPLX notice
            # as the "final report" (no ```tool block means "done" to
            # extract_tool_call). It should instead retry and reach step 2.
            self.assertEqual(steps, 2)
            self.assertIn("Done", report)
            self.assertNotIn("MTPLX", report)

    def test_ask_question_blocks_on_input_and_feeds_answer_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            responses = _canned_responses(
                '```tool\n{"name": "ask_question", '
                '"arguments": {"question": "Token or IP allowlist?", "options": ["token", "allowlist"]}}\n```',
                "Done. Used a token per the answer.",
            )
            chunks: list[str] = []
            with patch.object(builder_agent, "stream_chat", responses), \
                 patch("builtins.input", return_value="token") as fake_input:
                report, steps, touched = builder_agent.run_agent(
                    root, plan="p", task="", max_steps=10, command_timeout=30.0,
                    router_url="unused", timeout=30.0, dry_run=False,
                    on_chunk=chunks.append,
                )
            fake_input.assert_called_once()
            self.assertEqual(steps, 2)
            self.assertIn("Done.", report)
            self.assertEqual(touched, [])
            transcript = "".join(chunks)
            self.assertIn("ASK_QUESTION", transcript)
            self.assertIn("Token or IP allowlist?", transcript)
            self.assertIn("answered: token", transcript)

    def test_max_steps_is_a_hard_cap_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            always_calling = _canned_responses(*(
                '```tool\n{"name": "run_command", "arguments": {"command": "echo hi"}}\n```'
                for _ in range(5)
            ))
            with patch.object(builder_agent, "stream_chat", always_calling):
                report, steps, _touched = builder_agent.run_agent(
                    root, plan="p", task="", max_steps=5, command_timeout=30.0,
                    router_url="unused", timeout=30.0, dry_run=False,
                    on_chunk=lambda _p: None,
                )
            self.assertEqual(steps, 5)
            self.assertIn("Stopped after 5 steps", report)

    def test_every_turn_is_capped_at_max_completion_tokens(self) -> None:
        # This is the fix for a live incident: an agentic loop stuck on
        # repeated JSON-escaping failures spiraled into one 798-second,
        # 17,329-token turn with nothing capping how long it could run.
        seen_max_tokens = []

        def fake_stream_chat(model_alias, messages, on_chunk=None, router_url="", timeout=0, max_tokens=None):
            seen_max_tokens.append(max_tokens)
            return "Done, nothing to do."

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(builder_agent, "stream_chat", fake_stream_chat):
                builder_agent.run_agent(
                    root, plan="p", task="", max_steps=10, command_timeout=30.0,
                    router_url="unused", timeout=30.0, dry_run=False,
                    on_chunk=lambda _p: None,
                )
        self.assertEqual(seen_max_tokens, [builder_agent.MAX_COMPLETION_TOKENS])

    def test_repeated_parse_failures_escalate_to_a_specific_fix(self) -> None:
        # The generic "try again" message did not work live: three straight
        # identical JSON-escaping failures on one write_file, each met with
        # the same generic nudge, before the model gave up retrying it
        # sanely and produced the runaway turn above instead. After
        # CONSECUTIVE_FAILURES_BEFORE_ESCALATION failures in a row, the
        # feedback has to stop being generic and start naming the fix.
        fed_back_messages = []
        threshold = builder_agent.CONSECUTIVE_FAILURES_BEFORE_ESCALATION
        responses = _canned_responses(
            *(["```tool\n{not valid json\n```"] * threshold),
            "Done, gave up on that approach.",
        )

        def fake_stream_chat(model_alias, messages, on_chunk=None, router_url="", timeout=0, max_tokens=None):
            fed_back_messages.append(messages[-1]["content"] if messages[-1]["role"] == "user" else None)
            return responses(model_alias, messages, on_chunk, router_url, timeout, max_tokens)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(builder_agent, "stream_chat", fake_stream_chat):
                report, steps, _touched = builder_agent.run_agent(
                    root, plan="p", task="", max_steps=10, command_timeout=30.0,
                    router_url="unused", timeout=30.0, dry_run=False,
                    on_chunk=lambda _p: None,
                )
            self.assertEqual(steps, threshold + 1)
            self.assertIn("Done", report)
            # The feedback fed back to the model after the threshold-th
            # failure must name the actual fix (splitting the write), not
            # repeat the same generic "try again" it already ignored once.
            escalated = fed_back_messages[threshold]
            self.assertIn("split", escalated.lower())
            self.assertIn(f"{threshold} failed attempts", escalated)

    def test_a_successful_call_between_failures_resets_the_escalation_counter(self) -> None:
        # One-off parse failures are normal and shouldn't trip the
        # escalated message just because they're not adjacent to each
        # other in the run -- only a genuine consecutive streak should.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text("x = 1")
            fed_back_messages = []
            responses = _canned_responses(
                "```tool\n{not valid json\n```",
                '```tool\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```',
                "```tool\n{not valid json\n```",
                "Done.",
            )

            def fake_stream_chat(model_alias, messages, on_chunk=None, router_url="", timeout=0, max_tokens=None):
                fed_back_messages.append(messages[-1]["content"] if messages[-1]["role"] == "user" else None)
                return responses(model_alias, messages, on_chunk, router_url, timeout, max_tokens)

            with patch.object(builder_agent, "stream_chat", fake_stream_chat):
                builder_agent.run_agent(
                    root, plan="p", task="", max_steps=10, command_timeout=30.0,
                    router_url="unused", timeout=30.0, dry_run=False,
                    on_chunk=lambda _p: None,
                )
            # Both parse failures were isolated (a successful read_file sat
            # between them), so neither should have triggered escalation.
            for message in fed_back_messages:
                if message and "invalid" in message.lower():
                    self.assertNotIn("split", message.lower())

    def test_an_empty_response_is_retried_not_mistaken_for_completion(self) -> None:
        # Live incident: a model with no way to disable its own reasoning
        # burned its entire completion-token cap thinking at a modest
        # prompt size and was truncated before producing anything at all.
        # The resulting response had no ```tool block AND no text -- which
        # extract_tool_call correctly reads as "no tool call," but that is
        # not the same thing as "the model is done": the system prompt
        # requires a real summary on completion, and an empty string is
        # never that. Treating it as completion wrote a blank report and
        # ended the run having touched nothing.
        responses = _canned_responses(
            "   ",  # whitespace-only: cut off before producing anything
            "Done, nothing to do.",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(builder_agent, "stream_chat", responses):
                report, steps, _touched = builder_agent.run_agent(
                    root, plan="p", task="", max_steps=10, command_timeout=30.0,
                    router_url="unused", timeout=30.0, dry_run=False,
                    on_chunk=lambda _p: None,
                )
            self.assertEqual(steps, 2)
            self.assertIn("Done", report)

    def test_a_genuinely_empty_final_reply_still_ends_the_run_eventually(self) -> None:
        # The retry-on-empty behavior must not become an infinite loop if
        # the model keeps producing nothing -- max_steps still applies.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            always_empty = _canned_responses(*(["" for _ in range(5)]))
            with patch.object(builder_agent, "stream_chat", always_empty):
                report, steps, _touched = builder_agent.run_agent(
                    root, plan="p", task="", max_steps=5, command_timeout=30.0,
                    router_url="unused", timeout=30.0, dry_run=False,
                    on_chunk=lambda _p: None,
                )
            self.assertEqual(steps, 5)
            self.assertIn("Stopped after 5 steps", report)


if __name__ == "__main__":
    unittest.main()
