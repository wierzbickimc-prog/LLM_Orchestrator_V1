from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from builder_tools import ToolCallParseError, extract_tool_call, run_tool  # noqa: E402


class ExtractToolCallTests(unittest.TestCase):
    def test_plain_text_means_done(self) -> None:
        self.assertIsNone(extract_tool_call("All done, tests pass."))

    def test_parses_a_valid_call(self) -> None:
        text = 'Reading the file first.\n```tool\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```\n'
        call = extract_tool_call(text)
        self.assertEqual(call, {"name": "read_file", "arguments": {"path": "a.py"}})

    def test_invalid_json_is_reported_not_raised_as_crash(self) -> None:
        with self.assertRaises(ToolCallParseError):
            extract_tool_call("```tool\n{not json\n```")

    def test_unknown_tool_name_is_rejected(self) -> None:
        with self.assertRaises(ToolCallParseError):
            extract_tool_call('```tool\n{"name": "delete_everything", "arguments": {}}\n```')

    def test_truncated_tool_block_is_distinguished_from_done(self) -> None:
        # An opening fence with no closing fence -- a large write_file cut
        # off mid-generation -- must not be mistaken for "no tool call at
        # all" (which means the model is done).
        truncated = (
            'Let me write the test file now.\n\n```tool\n'
            '{"name": "write_file", "arguments": {"path": "test_x.py", '
            '"content": "import unittest\\n\\ndef test_a():\\n    pass'
        )
        with self.assertRaises(ToolCallParseError) as ctx:
            extract_tool_call(truncated)
        self.assertIn("cut off", str(ctx.exception))
        self.assertIn("append_file", str(ctx.exception))

    def test_short_complete_call_missing_only_closing_fence_is_recovered(self) -> None:
        # Observed in a live run: five consecutive short, well-formed tool
        # calls (e.g. a single read_file, ~60 chars of JSON) each missing
        # only their closing ``` despite being nowhere near large enough to
        # be genuinely truncated. The JSON itself is complete and valid, so
        # this must be recovered directly, not treated as a truncation to
        # retry (which wastes a step every time and never succeeds).
        text = '```tool\n{"name": "read_file", "arguments": {"path": "thermocycler_core.py"}}'
        call = extract_tool_call(text)
        self.assertEqual(call, {"name": "read_file", "arguments": {"path": "thermocycler_core.py"}})

    def test_genuinely_truncated_mid_string_still_raises(self) -> None:
        # An opening fence whose JSON is actually incomplete (unterminated
        # string, as a large write_file cut off mid-generation would look)
        # must still be reported as truncated, not silently swallowed.
        truncated = '```tool\n{"name": "write_file", "arguments": {"path": "a.py", "content": "no closing quote'
        with self.assertRaises(ToolCallParseError) as ctx:
            extract_tool_call(truncated)
        self.assertIn("cut off", str(ctx.exception))

    def test_bare_json_tool_call_with_no_fence_is_recovered(self) -> None:
        # Observed in a live run: the fence markers were stripped entirely
        # (not truncated -- there's no "```tool" substring anywhere), leaving
        # a bare tool-call-shaped JSON object. Without recovery this reads
        # as "no tool call at all" (done), ending the session after zero
        # real work.
        text = 'tool\n{"name": "read_file", "arguments": {"path": "devices.py"}}'
        call = extract_tool_call(text)
        self.assertEqual(call, {"name": "read_file", "arguments": {"path": "devices.py"}})

    def test_bare_json_tool_call_with_content_braces_still_parses(self) -> None:
        # A real JSON decoder (not a brace-matching regex) must not be
        # confused by braces inside a string value like write_file's content.
        text = 'tool\n{"name": "write_file", "arguments": {"path": "a.py", "content": "def f():\\n    return {}"}}'
        call = extract_tool_call(text)
        self.assertEqual(call["name"], "write_file")
        self.assertEqual(call["arguments"]["content"], "def f():\n    return {}")

    def test_bare_json_with_unknown_tool_name_is_not_recovered(self) -> None:
        # Guards against false-positive recovery of unrelated JSON that
        # happens to start with "name" -- only a real tool name counts.
        self.assertIsNone(extract_tool_call('Example config: {"name": "not_a_tool", "arguments": {}}'))

    def test_missing_arguments_key_is_rejected(self) -> None:
        with self.assertRaises(ToolCallParseError):
            extract_tool_call('```tool\n{"name": "read_file"}\n```')


class RunToolPathContainmentTests(unittest.TestCase):
    def test_read_file_within_root_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text("print(1)")
            result = run_tool(
                {"name": "read_file", "arguments": {"path": "a.py"}}, root, 30.0, dry_run=False
            )
            self.assertEqual(result, "print(1)")

    def test_read_file_escaping_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            (Path(directory) / "secret.txt").write_text("outside")
            result = run_tool(
                {"name": "read_file", "arguments": {"path": "../secret.txt"}}, root, 30.0, dry_run=False
            )
            self.assertIn("escapes the project root", result)

    def test_write_file_escaping_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            result = run_tool(
                {"name": "write_file", "arguments": {"path": "../escaped.txt", "content": "x"}},
                root, 30.0, dry_run=False,
            )
            self.assertIn("escapes the project root", result)
            self.assertFalse((Path(directory) / "escaped.txt").exists())

    def test_write_file_into_xcodeproj_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_tool(
                {"name": "write_file", "arguments": {
                    "path": "MyApp.xcodeproj/project.pbxproj", "content": "// corrupted",
                }},
                root, 30.0, dry_run=False,
            )
            self.assertIn("refusing to write inside MyApp.xcodeproj", result)
            self.assertFalse((root / "MyApp.xcodeproj").exists())

    def test_append_file_into_swiftpm_build_dir_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_tool(
                {"name": "append_file", "arguments": {"path": ".build/artifact.txt", "content": "x"}},
                root, 30.0, dry_run=False,
            )
            self.assertIn("refusing to write inside .build", result)
            self.assertFalse((root / ".build").exists())

    def test_write_file_to_normal_swift_source_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_tool(
                {"name": "write_file", "arguments": {
                    "path": "Sources/MyApp/Feature.swift", "content": "struct Feature {}",
                }},
                root, 30.0, dry_run=False,
            )
            self.assertIn("Wrote", result)
            self.assertEqual((root / "Sources/MyApp/Feature.swift").read_text(), "struct Feature {}")

    def test_write_file_dry_run_does_not_touch_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_tool(
                {"name": "write_file", "arguments": {"path": "new.py", "content": "x = 1"}},
                root, 30.0, dry_run=True,
            )
            self.assertIn("[dry-run]", result)
            self.assertFalse((root / "new.py").exists())

    def test_write_file_creates_parent_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_tool(
                {"name": "write_file", "arguments": {"path": "pkg/mod.py", "content": "x = 1"}},
                root, 30.0, dry_run=False,
            )
            self.assertEqual((root / "pkg" / "mod.py").read_text(), "x = 1")

    def test_append_file_adds_to_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "big.py").write_text("part one\n")
            run_tool(
                {"name": "append_file", "arguments": {"path": "big.py", "content": "part two\n"}},
                root, 30.0, dry_run=False,
            )
            self.assertEqual((root / "big.py").read_text(), "part one\npart two\n")

    def test_append_file_creates_file_if_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_tool(
                {"name": "append_file", "arguments": {"path": "new.py", "content": "x = 1"}},
                root, 30.0, dry_run=False,
            )
            self.assertEqual((root / "new.py").read_text(), "x = 1")

    def test_append_file_escaping_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            result = run_tool(
                {"name": "append_file", "arguments": {"path": "../escaped.txt", "content": "x"}},
                root, 30.0, dry_run=False,
            )
            self.assertIn("escapes the project root", result)
            self.assertFalse((Path(directory) / "escaped.txt").exists())

    def test_append_file_dry_run_does_not_touch_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "big.py").write_text("part one\n")
            run_tool(
                {"name": "append_file", "arguments": {"path": "big.py", "content": "part two\n"}},
                root, 30.0, dry_run=True,
            )
            self.assertEqual((root / "big.py").read_text(), "part one\n")

    def test_run_command_executes_and_reports_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_tool(
                {"name": "run_command", "arguments": {"command": "echo hello"}},
                Path(directory), 30.0, dry_run=False,
            )
            self.assertTrue(result.startswith("Exit code 0"))
            self.assertIn("hello", result)

    def test_run_command_dry_run_does_not_execute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            result = run_tool(
                {"name": "run_command", "arguments": {"command": f"touch {marker}"}},
                Path(directory), 30.0, dry_run=True,
            )
            self.assertIn("[dry-run]", result)
            self.assertFalse(marker.exists())

    def test_run_command_timeout_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_tool(
                {"name": "run_command", "arguments": {"command": "sleep 5"}},
                Path(directory), 0.2, dry_run=False,
            )
            self.assertIn("timed out", result)


if __name__ == "__main__":
    unittest.main()
