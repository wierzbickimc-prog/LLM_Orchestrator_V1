from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from report_common import (  # noqa: E402
    DEFAULT_EXTENSIONS,
    collect_files,
    describe_skipped,
    looks_like_failed_tool_call,
    parse_verdict,
    referenced_files,
    resolve_ai_path,
)


class CollectFilesSwiftPackageTests(unittest.TestCase):
    def test_finds_swift_sources_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Sources" / "MyApp").mkdir(parents=True)
            (root / "Sources" / "MyApp" / "Feature.swift").write_text("struct Feature {}")
            (root / "Package.swift").write_text("// swift-tools-version:5.9")
            found = {p.name for p in collect_files(root, DEFAULT_EXTENSIONS)}
            self.assertIn("Feature.swift", found)
            self.assertIn("Package.swift", found)

    def test_ignores_build_and_package_manager_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".build" / "checkouts").mkdir(parents=True)
            (root / ".build" / "checkouts" / "Dependency.swift").write_text("// vendored")
            (root / ".swiftpm").mkdir()
            (root / ".swiftpm" / "state.json").write_text("{}")
            found = {p.name for p in collect_files(root, DEFAULT_EXTENSIONS)}
            self.assertNotIn("Dependency.swift", found)
            self.assertNotIn("state.json", found)

    def test_ignores_xcode_project_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "MyApp.xcodeproj" / "xcshareddata"
            bundle.mkdir(parents=True)
            (root / "MyApp.xcodeproj" / "project.pbxproj").write_text("// plist")
            # .plist IS a tracked extension now, so this specifically tests
            # that the ignored-dir entry (not just the extension filter)
            # keeps the pipeline out of Xcode's project bundle.
            (bundle / "WorkspaceSettings.plist").write_text("<plist/>")
            found = {p.name for p in collect_files(root, DEFAULT_EXTENSIONS)}
            self.assertNotIn("project.pbxproj", found)
            self.assertNotIn("WorkspaceSettings.plist", found)


class ResolveAiPathTests(unittest.TestCase):
    def test_anchors_to_a_directory_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.assertEqual(
                resolve_ai_path(target, "scout-report.md"),
                target / ".ai" / "scout-report.md",
            )

    def test_anchors_to_the_parent_of_a_file_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "serve.py"
            target.write_text("# stub")
            self.assertEqual(
                resolve_ai_path(target, "scout-report.md"),
                Path(directory) / ".ai" / "scout-report.md",
            )

    def test_does_not_anchor_to_this_tools_own_directory(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            result = resolve_ai_path(Path(directory), "scout-report.md")
        self.assertFalse(str(result).startswith(str(project_root)))


class LooksLikeFailedToolCallTests(unittest.TestCase):
    def test_detects_the_mtplx_injected_notice(self) -> None:
        text = (
            'I\'ll start by checking what\'s present.</tool_call>[MTPLX: this reply '
            'tried to call "list_files", but no tools are active on this request...]'
        )
        self.assertTrue(looks_like_failed_tool_call(text))

    def test_detects_bare_tool_call_tags(self) -> None:
        self.assertTrue(looks_like_failed_tool_call("<tool_call>{\"name\": \"x\"}</tool_call>"))

    def test_normal_report_text_is_not_flagged(self) -> None:
        text = "# Scout Report\n\nThe relevant file is `serve.py`, which defines the routes."
        self.assertFalse(looks_like_failed_tool_call(text))


class ReferencedFilesTests(unittest.TestCase):
    def test_finds_a_directly_referenced_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "web").mkdir()
            (root / "web" / "app.js").write_text("// stub")
            (root / "serve.py").write_text("# stub")
            report = "The routes live in `serve.py`, and the SPA is in `web/app.js`."
            result = referenced_files(report, root)
            self.assertEqual(
                {p.name for p in result}, {"serve.py", "app.js"},
            )

    def test_resolves_a_bare_filename_found_deeper_in_the_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "web").mkdir()
            (root / "web" / "app.js").write_text("// stub")
            report = "The frontend logic is in `app.js`."
            result = referenced_files(report, root)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].name, "app.js")

    def test_ambiguous_bare_filename_is_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a").mkdir()
            (root / "b").mkdir()
            (root / "a" / "index.html").write_text("<html></html>")
            (root / "b" / "index.html").write_text("<html></html>")
            report = "See `index.html` for the page shell."
            result = referenced_files(report, root)
            self.assertEqual(result, [])

    def test_nonexistent_paths_are_dropped_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = "This mentions gpt-5.6-sol and Qwen3.8-27B, neither of which is a file."
            self.assertEqual(referenced_files(report, root), [])

    def test_drops_a_trailing_line_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "serve.py").write_text("# stub")
            report = "See `serve.py:42` for the route table."
            result = referenced_files(report, root)
            self.assertEqual([p.name for p in result], ["serve.py"])


class DescribeSkippedTests(unittest.TestCase):
    def test_empty_list_produces_no_note(self) -> None:
        self.assertEqual(describe_skipped([]), "")

    def test_lists_skipped_paths_and_warns_against_reporting_them_missing(self) -> None:
        note = describe_skipped([Path("web/view.js"), Path("serve.py")])
        self.assertIn("web/view.js", note)
        self.assertIn("serve.py", note)
        self.assertIn("do not report them as missing", note)


class ParseVerdictTests(unittest.TestCase):
    def test_reads_pass(self) -> None:
        self.assertEqual(parse_verdict("VERDICT: PASS\n\n# Audit Report\nNo defects."), "PASS")

    def test_reads_reject(self) -> None:
        self.assertEqual(parse_verdict("VERDICT: REJECT\n\n## Fix List\n1. ..."), "REJECT")

    def test_is_case_insensitive(self) -> None:
        self.assertEqual(parse_verdict("verdict: pass\n"), "PASS")

    def test_ignores_leading_blank_lines(self) -> None:
        self.assertEqual(parse_verdict("\n\n  VERDICT: PASS\nmore text"), "PASS")

    def test_missing_verdict_line_is_unknown(self) -> None:
        self.assertEqual(parse_verdict("# Audit Report\nNo defects found."), "UNKNOWN")

    def test_verdict_must_be_the_first_line_not_anywhere_in_the_text(self) -> None:
        self.assertEqual(parse_verdict("# Report\nSummary text.\nVERDICT: PASS\n"), "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
