from __future__ import annotations

import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import subprocess

from report_common import (  # noqa: E402
    DEFAULT_CHAR_BUDGET,
    DEFAULT_EXTENSIONS,
    collect_files,
    describe_skipped,
    git_changed_files,
    git_repro_context,
    looks_like_failed_tool_call,
    char_budget_for_role,
    parse_builder_accuracy,
    parse_verdict,
    referenced_files,
    resolve_ai_path,
)


def _git_repo(root: Path) -> None:
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t.t"],
        ["config", "user.name", "t"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


class GitContextTests(unittest.TestCase):
    def test_returns_empty_string_outside_a_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(git_repro_context(Path(directory)), "")
            self.assertEqual(git_changed_files(Path(directory)), set())

    def test_surfaces_recent_commits_and_the_uncommitted_diff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _git_repo(root)
            (root / "app.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "app.py"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-qm", "add app"], cwd=root, check=True, capture_output=True,
            )
            (root / "app.py").write_text("x = 2\n")

            context = git_repro_context(root)
            self.assertIn("add app", context)
            self.assertIn("-x = 1", context)
            self.assertIn("+x = 2", context)
            self.assertEqual(
                git_changed_files(root), {str((root / "app.py").resolve())},
            )

    def test_counts_an_untracked_file_as_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _git_repo(root)
            (root / "new.py").write_text("y = 1\n")
            self.assertIn(str((root / "new.py").resolve()), git_changed_files(root))


class EnsureNonemptyReportTests(unittest.TestCase):
    def test_raises_on_a_too_short_response(self) -> None:
        from report_common import ReportError, ensure_nonempty_report
        with self.assertRaises(ReportError):
            ensure_nonempty_report("   \n  ", phase="diagnose")
        with self.assertRaises(ReportError):
            ensure_nonempty_report("too short", phase="diagnose")

    def test_passes_a_real_report_through_unchanged(self) -> None:
        from report_common import ensure_nonempty_report
        body = "# Diagnosis\n\n" + ("root cause analysis. " * 40)
        self.assertEqual(ensure_nonempty_report(body, phase="diagnose"), body)


class ReadRequiredArtifactTests(unittest.TestCase):
    def test_an_empty_artifact_is_rejected_by_default(self) -> None:
        from report_common import ReportError, read_required_artifact
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "diagnosis-report.md"
            p.write_text("   \n")
            with self.assertRaises(ReportError):
                read_required_artifact(p, produced_by="scripts/diagnose_report.py")

    def test_allow_empty_opts_out(self) -> None:
        from report_common import read_required_artifact
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "x.md"
            p.write_text("")
            self.assertEqual(read_required_artifact(p, produced_by="x", allow_empty=True), "")


class ImportersOfTests(unittest.TestCase):
    def test_finds_a_module_that_imports_a_changed_file(self) -> None:
        from report_common import importers_of
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "core.js").write_text("export const x = 1;\n")
            (root / "app.js").write_text("import { x } from './core.js';\n")
            (root / "unrelated.js").write_text("const y = 2;\n")
            hits = importers_of([root / "core.js"], root, {".js"})
            names = {p.name for p in hits}
            self.assertIn("app.js", names)
            self.assertNotIn("unrelated.js", names)
            self.assertNotIn("core.js", names)


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


class BuilderAccuracyTests(unittest.TestCase):
    def test_reads_the_score_from_the_line_after_the_verdict(self) -> None:
        report = "VERDICT: REJECT\nBUILDER_ACCURACY: 62\n\n## Findings\n..."
        self.assertEqual(parse_builder_accuracy(report), 62)

    def test_absent_line_is_none_rather_than_a_made_up_zero(self) -> None:
        # Older reports predate the line entirely; a zero here would read as
        # "the builder produced nothing", which is a very different claim.
        self.assertIsNone(parse_builder_accuracy("VERDICT: PASS\n\n## Findings\n"))

    def test_out_of_range_and_unparseable_values_are_rejected(self) -> None:
        self.assertIsNone(parse_builder_accuracy("VERDICT: PASS\nBUILDER_ACCURACY: 140\n"))
        self.assertIsNone(parse_builder_accuracy("VERDICT: PASS\nBUILDER_ACCURACY: high\n"))

    def test_stops_looking_once_the_report_body_starts(self) -> None:
        # A "BUILDER_ACCURACY:" mentioned in prose partway down the report is
        # the model discussing the field, not reporting a score.
        report = "VERDICT: PASS\n\n## Findings\nThe BUILDER_ACCURACY: 99 line was missing.\n"
        self.assertIsNone(parse_builder_accuracy(report))

    def test_percent_sign_and_extra_spacing_are_tolerated(self) -> None:
        self.assertEqual(parse_builder_accuracy("VERDICT: PASS\nBUILDER_ACCURACY:  85%\n"), 85)


class IgnoreScopeTests(unittest.TestCase):
    def test_ignored_dir_names_are_matched_relative_to_the_scan_root(self) -> None:
        # Pointing a phase directly at a project that happens to live inside
        # an ignored directory has to work: sandbox/SomeProject is scanned
        # regularly, and "sandbox" is an ignored name. The target's own
        # location must never disqualify the target.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sandbox" / "MyProject"
            (root / "src").mkdir(parents=True)
            (root / "src" / "main.py").write_text("x = 1\n")
            found = collect_files(root, {".py"})
        self.assertEqual([p.name for p in found], ["main.py"])

    def test_ignored_dir_inside_the_tree_is_still_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "keep.py").write_text("x = 1\n")
            (root / "sandbox").mkdir()
            (root / "sandbox" / "other.py").write_text("y = 2\n")
            found = collect_files(root, {".py"})
        self.assertEqual([p.name for p in found], ["keep.py"])


class InterleaveByAreaTests(unittest.TestCase):
    def _tree(self, root: Path) -> None:
        for area, names in {
            "aaa": ["one.py", "two.py", "three.py"],
            "zzz": ["alpha.py", "beta.py"],
        }.items():
            (root / area).mkdir()
            for name in names:
                (root / area / name).write_text("x\n")

    def test_every_area_appears_before_any_area_repeats(self) -> None:
        # The point of the ordering: build_context fills its budget in list
        # order, so whatever sorts last is what gets dropped. Alphabetical
        # order made that an amputation of whole directories -- this repo
        # lost all of scripts/ and tests/ from every Scout context, and
        # nothing downstream could tell that had happened.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            areas = [p.parent.name for p in collect_files(root, {".py"})]
        self.assertEqual(areas[:2], ["aaa", "zzz"])
        self.assertEqual(areas, ["aaa", "zzz", "aaa", "zzz", "aaa"])

    def test_an_exhausted_area_drops_out_without_stalling_the_others(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            found = collect_files(root, {".py"})
        self.assertEqual(len(found), 5)
        self.assertEqual(found[-1].parent.name, "aaa")

    def test_files_keep_alphabetical_order_within_their_own_area(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            found = collect_files(root, {".py"})
        aaa = [p.name for p in found if p.parent.name == "aaa"]
        self.assertEqual(aaa, sorted(aaa))


class CharBudgetForRoleTests(unittest.TestCase):
    """char_budget_for_role must never touch or care about the operator's
    real config file -- these all point MODEL_DECK_CONFIG_DIR at a throwaway
    directory. (See test_web.py's isolated_config fixture for what happens
    when a test in this codebase forgets to.)"""

    def _state_dir(self, tmp_path: Path, state: dict) -> None:
        import json as _json
        (tmp_path / "state.json").write_text(_json.dumps(state))

    def test_derives_from_the_roles_context_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._state_dir(Path(directory), {
                "planner": {"kind": "local"},
                "roles": {"scout": {"context_window": 131_072}},
            })
            with patch.dict("os.environ", {"MODEL_DECK_CONFIG_DIR": directory}):
                budget = char_budget_for_role("scout")
        expected = int((131_072 - 34_000) * 3.7)
        self.assertEqual(budget, expected)

    def test_cloud_planner_falls_back_rather_than_using_the_unused_local_role(self) -> None:
        # roles["planner"] still has a context_window even when the cloud
        # backend is active -- it's just not the model actually serving the
        # request, so deriving from it would be a guess, not a measurement.
        with tempfile.TemporaryDirectory() as directory:
            self._state_dir(Path(directory), {
                "planner": {"kind": "openai"},
                "roles": {"planner": {"context_window": 131_072}},
            })
            with patch.dict("os.environ", {"MODEL_DECK_CONFIG_DIR": directory}):
                budget = char_budget_for_role("planner")
        self.assertEqual(budget, DEFAULT_CHAR_BUDGET)

    def test_unknown_phase_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._state_dir(Path(directory), {"planner": {}, "roles": {}})
            with patch.dict("os.environ", {"MODEL_DECK_CONFIG_DIR": directory}):
                budget = char_budget_for_role("does-not-exist")
        self.assertEqual(budget, DEFAULT_CHAR_BUDGET)

    def test_zero_context_window_falls_back(self) -> None:
        # load_state deep-merges onto default_state(), so a truly missing
        # key would inherit the real default -- this has to set the field
        # explicitly to exercise the "can't derive anything useful" path.
        with tempfile.TemporaryDirectory() as directory:
            self._state_dir(Path(directory), {
                "planner": {"kind": "local"},
                "roles": {"scout": {"context_window": 0}},
            })
            with patch.dict("os.environ", {"MODEL_DECK_CONFIG_DIR": directory}):
                budget = char_budget_for_role("scout")
        self.assertEqual(budget, DEFAULT_CHAR_BUDGET)

    def test_a_missing_state_file_still_derives_correctly(self) -> None:
        # load_state() returns default_state() wholesale when state.json is
        # absent, and that default is itself a real, valid context_window --
        # so this is NOT a fallback case. A report script running before the
        # GUI has ever saved a config should still get a correctly derived
        # budget, not silently degrade to the stale constant.
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"MODEL_DECK_CONFIG_DIR": directory}):
                budget = char_budget_for_role("scout")
        self.assertNotEqual(budget, DEFAULT_CHAR_BUDGET)
        self.assertGreater(budget, 0)

    def test_an_exception_resolving_state_falls_back_instead_of_raising(self) -> None:
        # A report script must be able to run even if state.json is
        # corrupt or modeldeck.state misbehaves -- getting the budget right
        # is an optimization, not a precondition for the phase to work.
        with patch("modeldeck.state.load_state", side_effect=RuntimeError("boom")):
            budget = char_budget_for_role("scout")
        self.assertEqual(budget, DEFAULT_CHAR_BUDGET)
