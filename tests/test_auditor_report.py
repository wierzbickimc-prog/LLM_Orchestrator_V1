from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from auditor_report import detect_test_command, run_test_suite  # noqa: E402


class DetectTestCommandTests(unittest.TestCase):
    def test_detects_swift_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Package.swift").write_text("// swift-tools-version:5.9")
            self.assertEqual(detect_test_command(root), "swift test")

    def test_detects_pytest_style_tests_in_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_widget.py").write_text("def test_x(): pass")
            command = detect_test_command(root)
            self.assertIsNotNone(command)
            self.assertIn("-m pytest", command)

    def test_detects_pytest_style_tests_nested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sub").mkdir()
            (root / "sub" / "widget_test.py").write_text("def test_x(): pass")
            command = detect_test_command(root)
            self.assertIsNotNone(command)

    def test_prefers_projects_own_venv_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_widget.py").write_text("def test_x(): pass")
            venv_python = root / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("#!/bin/sh\n")
            command = detect_test_command(root)
            self.assertIn(str(venv_python), command)

    def test_none_when_nothing_recognizable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("# nothing here")
            self.assertIsNone(detect_test_command(root))


class RunTestSuiteTests(unittest.TestCase):
    def test_reports_exit_code_and_output_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_test_suite(Path(directory), "echo all good", timeout=10.0)
            self.assertIn("Exit code: 0", result)
            self.assertIn("all good", result)

    def test_reports_nonzero_exit_code_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_test_suite(Path(directory), "echo boom && exit 1", timeout=10.0)
            self.assertIn("Exit code: 1", result)
            self.assertIn("boom", result)

    def test_timeout_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_test_suite(Path(directory), "sleep 5", timeout=0.2)
            self.assertIn("TIMED OUT", result)

    def test_long_output_is_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_test_suite(
                Path(directory), "python3 -c \"print('x' * 20000)\"", timeout=10.0,
            )
            self.assertIn("truncated", result)


if __name__ == "__main__":
    unittest.main()
