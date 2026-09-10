from __future__ import annotations

import io
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modeldeck.pipeline import (
    MAX_RENOVATOR_RETRIES,
    PIPELINE_ORDER,
    Pipeline,
    REQUIRED_ARTIFACT_FOR_START,
    STATUS_PHASES,
    TROUBLESHOOT_ORDER,
)


class FakeManager:
    """Stand-in for ProcessManager -- no real processes."""
    def stop_local_models(self, ports):
        pass
    def launch(self, phase, role):
        pass
    def ensure_router(self):
        return 0


def make_state():
    return {
        "active": {"phase": "scout", "kind": "local", "base_url": "http://127.0.0.1:8000/v1", "model_id": "scout"},
        "planner": {"kind": "openai", "model": "gpt-5.6-sol", "base_url": "https://api.openai.com/v1", "reasoning_effort": "high"},
        "roles": {
            "scout": {"port": 8000, "model": "test-model"},
            "builder": {"port": 8002, "model": "test-model", "max_steps": 80},
            "renovator": {"port": 8006, "model": "test-model", "max_steps": 40},
            "auditor": {"port": 8004, "model": "test-model"},
            "planner": {"port": 8008, "model": "test-model"},
            "chat": {"port": 8010, "model": "test-model"},
            "prompt_dev": {"port": 8012, "model": "test-model"},
        },
        "router": {"host": "127.0.0.1", "port": 8100},
        "prompt_overrides": {},
    }


collector = None

def make_sink():
    global collector
    if collector is None:
        collector = []
    def sink(event):
        collector.append(event)
    return sink


def reset_collector():
    global collector
    collector = []


def _mock_path(exists_val=False):
    """Create a MagicMock that behaves like a Path with .exists() returning exists_val."""
    p = MagicMock(spec=Path)
    p.exists.return_value = exists_val
    p.__str__ = lambda self: "/tmp/project/.ai/artifact.md"
    return p


# ---------------------------------------------------------------------------
# start() validation
# ---------------------------------------------------------------------------

class TestStartValidation:
    def setup_method(self):
        reset_collector()
        self.manager = FakeManager()
        self.pipeline = Pipeline(self.manager, make_state, make_sink())

    def test_missing_path_rejected(self):
        result = self.pipeline.start("full", "", "task", "scout")
        assert "error" in result
        assert "Path required" in result["error"]

    def test_scout_without_task_rejected(self):
        result = self.pipeline.start("full", "/tmp/project", "", "scout")
        assert "error" in result
        assert "Task required" in result["error"]

    @patch("modeldeck.pipeline.resolve_ai_path")
    def test_planner_without_scout_report_rejected(self, mock_resolve):
        mock_resolve.return_value = _mock_path(exists_val=False)
        result = self.pipeline.start("full", "/tmp/project", "task", "planner")
        assert "error" in result
        assert "Missing prerequisite" in result["error"]

    @patch("modeldeck.pipeline.resolve_ai_path")
    def test_builder_without_plan_rejected(self, mock_resolve):
        mock_resolve.return_value = _mock_path(exists_val=False)
        result = self.pipeline.start("full", "/tmp/project", "task", "builder")
        assert "error" in result
        assert "Missing prerequisite" in result["error"]


# ---------------------------------------------------------------------------
# Queue construction
# ---------------------------------------------------------------------------

class TestQueueConstruction:
    def setup_method(self):
        reset_collector()
        self.manager = FakeManager()
        self.pipeline = Pipeline(self.manager, make_state, make_sink())

    @patch("modeldeck.pipeline.resolve_ai_path")
    def test_start_at_scout_full_queue(self, mock_resolve):
        mock_resolve.return_value = _mock_path(exists_val=True)
        with patch.object(self.pipeline, 'run_next') as mock_run:
            mock_run.return_value = {"status": "running", "phase": "scout"}
            self.pipeline.start("full", "/tmp/project", "task", "scout")
        # run_next is stubbed here, so nothing has been popped yet: the
        # starting phase is still at the head of the queue.
        assert self.pipeline.pipeline_queue == ["scout", "planner", "builder", "auditor"]

    @patch("modeldeck.pipeline.resolve_ai_path")
    def test_start_at_builder_partial_queue(self, mock_resolve):
        mock_resolve.return_value = _mock_path(exists_val=True)
        with patch.object(self.pipeline, 'run_next') as mock_run:
            mock_run.return_value = {"status": "running", "phase": "builder"}
            self.pipeline.start("full", "/tmp/project", "task", "builder")
        assert self.pipeline.pipeline_queue == ["builder", "auditor"]
        skipped_events = [e for e in collector if e.get("type") == "phase_status" and "skipped" in e.get("status", "")]
        assert len(skipped_events) == 2  # scout and planner skipped


# ---------------------------------------------------------------------------
# Troubleshoot flow
# ---------------------------------------------------------------------------

class TestTroubleshootFlow:
    def setup_method(self):
        reset_collector()
        self.manager = FakeManager()
        self.pipeline = Pipeline(self.manager, make_state, make_sink())

    @patch("modeldeck.pipeline.resolve_ai_path")
    def test_start_at_diagnose_builds_the_troubleshoot_queue(self, mock_resolve):
        mock_resolve.return_value = _mock_path(exists_val=True)
        with patch.object(self.pipeline, "run_next") as mock_run:
            mock_run.return_value = {"status": "running", "phase": "diagnose"}
            self.pipeline.start("full", "/tmp/project", "it broke", "diagnose", "troubleshoot")
        assert self.pipeline.pipeline_queue == ["diagnose", "planner", "builder", "auditor"]
        assert self.pipeline.pipeline_flow == "troubleshoot"

    def test_scout_is_not_a_phase_in_the_troubleshoot_flow(self):
        result = self.pipeline.start("full", "/tmp/project", "task", "scout", "troubleshoot")
        assert "error" in result

    @patch("modeldeck.pipeline.resolve_ai_path")
    def test_planner_in_troubleshoot_flow_needs_the_diagnosis_report(self, mock_resolve):
        mock_resolve.return_value = _mock_path(exists_val=False)
        result = self.pipeline.start("full", "/tmp/project", "task", "planner", "troubleshoot")
        assert "Missing prerequisite" in result["error"]

    def test_diagnose_phase_runs_on_the_auditor_role(self):
        assert self.pipeline._phase_local_role("diagnose") == "auditor"

    def test_planner_args_point_at_the_diagnosis_report_in_troubleshoot_flow(self):
        self.pipeline.pipeline_flow = "troubleshoot"
        mock_proc = MagicMock()
        mock_proc.stdout = io.StringIO("Wrote /tmp/out/report.md\n")
        mock_proc.wait.return_value = 0
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            with patch("modeldeck.pipeline.PROJECT_DIR", Path("/tmp/fake")):
                with patch.object(Path, "read_text", return_value="plan"):
                    self.pipeline._pipeline_worker("planner", "task", "/tmp/project")
        argv = mock_popen.call_args[0][0]
        assert "--scout-report" in argv
        assert argv[argv.index("--scout-report") + 1].endswith("diagnosis-report.md")

    def test_planner_gets_the_troubleshoot_flag_in_troubleshoot_flow(self):
        self.pipeline.pipeline_flow = "troubleshoot"
        mock_proc = MagicMock()
        mock_proc.stdout = io.StringIO("Wrote /tmp/out/report.md\n")
        mock_proc.wait.return_value = 0
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            with patch("modeldeck.pipeline.PROJECT_DIR", Path("/tmp/fake")):
                with patch.object(Path, "read_text", return_value="plan"):
                    self.pipeline._pipeline_worker("planner", "task", "/tmp/project")
        assert "--troubleshoot" in mock_popen.call_args[0][0]

    def test_feature_flow_planner_args_have_no_scout_report_override(self):
        self.pipeline.pipeline_flow = "feature"
        mock_proc = MagicMock()
        mock_proc.stdout = io.StringIO("Wrote /tmp/out/report.md\n")
        mock_proc.wait.return_value = 0
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            with patch("modeldeck.pipeline.PROJECT_DIR", Path("/tmp/fake")):
                with patch.object(Path, "read_text", return_value="plan"):
                    self.pipeline._pipeline_worker("planner", "task", "/tmp/project")
        argv = mock_popen.call_args[0][0]
        assert "--scout-report" not in argv
        assert "--troubleshoot" not in argv


# ---------------------------------------------------------------------------
# Worker happy path (mocked Popen)
# ---------------------------------------------------------------------------

class TestWorkerHappyPath:
    def setup_method(self):
        reset_collector()
        self.manager = FakeManager()
        self.pipeline = Pipeline(self.manager, make_state, make_sink())

    @patch("subprocess.Popen")
    def test_worker_success(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.stdout = io.StringIO("Hello world\nWrote /tmp/out/report.md\n")
        mock_proc.wait.return_value = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        fake_out = MagicMock(spec=Path)
        fake_out.read_text.return_value = "Report content"
        with patch("modeldeck.pipeline.PROJECT_DIR", Path("/tmp/fake")):
            with patch.object(Path, 'read_text', return_value="Report content"):
                self.pipeline._pipeline_worker("scout", "task", "/tmp/project")

        finished = [e for e in collector if e.get("type") == "finished"]
        assert len(finished) == 1
        assert finished[0]["success"] is True
        assert finished[0]["content"] == "Report content"

    @patch("subprocess.Popen")
    def test_worker_nonzero_exit(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.stdout = io.StringIO("some output\n")
        mock_proc.wait.return_value = 1
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("modeldeck.pipeline.PROJECT_DIR", Path("/tmp/fake")):
            self.pipeline._pipeline_worker("scout", "task", "/tmp/project")

        finished = [e for e in collector if e.get("type") == "finished"]
        assert len(finished) == 1
        assert finished[0]["success"] is False

    @patch("subprocess.Popen")
    def test_worker_no_wrote_line(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.stdout = io.StringIO("no artifact here\n")
        mock_proc.wait.return_value = 0
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        with patch("modeldeck.pipeline.PROJECT_DIR", Path("/tmp/fake")):
            self.pipeline._pipeline_worker("scout", "task", "/tmp/project")

        finished = [e for e in collector if e.get("type") == "finished"]
        assert len(finished) == 1
        assert finished[0]["success"] is False
        assert "output path wasn't found" in finished[0]["content"]


# ---------------------------------------------------------------------------
# ask_question blocking/unblocking
# ---------------------------------------------------------------------------

class TestAskQuestion:
    def setup_method(self):
        reset_collector()
        self.manager = FakeManager()
        self.pipeline = Pipeline(self.manager, make_state, make_sink())

    def test_answer_unblocks(self):
        event = threading.Event()
        box = []
        self.pipeline._pending_answers["builder"] = (event, box)

        answered = threading.Event()
        def answer_thread():
            time.sleep(0.05)
            self.pipeline.answer("builder", "option B")
            answered.set()

        t = threading.Thread(target=answer_thread)
        t.start()
        event.wait(timeout=2)
        t.join(timeout=2)
        assert event.is_set()
        assert box == ["option B"]

    def test_stop_unblocks_with_empty(self):
        event = threading.Event()
        box = []
        self.pipeline._pending_answers["builder"] = (event, box)

        ev, bx = self.pipeline._pending_answers.pop("builder", (None, None))
        if bx is not None:
            bx.append("")
        if ev is not None:
            ev.set()
        assert event.is_set()
        assert box == [""]


# ---------------------------------------------------------------------------
# Renovator retry logic
# ---------------------------------------------------------------------------

class TestRenovatorLogic:
    def setup_method(self):
        reset_collector()
        self.manager = FakeManager()
        self.pipeline = Pipeline(self.manager, make_state, make_sink())
        self.pipeline.pipeline_mode = "full"

    def test_auditor_reject_queues_renovator(self):
        self.pipeline.pipeline_renovator_retries = 0
        self.pipeline.report_start_times["auditor"] = time.monotonic()
        self.pipeline.report_processes["auditor"] = MagicMock(poll=lambda: None)
        # run_next is stubbed so the queue can be inspected as it was set,
        # before the repair pass is popped off and actually launched -- in
        # "full" mode on_phase_finished runs the next phase immediately.
        with patch.object(self.pipeline, 'run_next') as mock_run:
            mock_run.return_value = {"status": "running", "phase": "renovator"}
            self.pipeline.on_phase_finished("auditor", True, "VERDICT: REJECT")
        assert self.pipeline.pipeline_renovator_retries == 1
        assert self.pipeline.pipeline_queue == ["renovator", "auditor"]

    def test_second_reject_stops(self):
        self.pipeline.pipeline_renovator_retries = MAX_RENOVATOR_RETRIES
        self.pipeline.report_start_times["auditor"] = time.monotonic()
        self.pipeline.report_processes["auditor"] = MagicMock(poll=lambda: None)
        result = self.pipeline.on_phase_finished("auditor", True, "VERDICT: REJECT")
        assert self.pipeline.pipeline_queue == []
        assert result["status"] == "failed"

    def test_auditor_pass_continues(self):
        self.pipeline.pipeline_renovator_retries = 0
        self.pipeline.pipeline_queue = []
        self.pipeline.report_start_times["auditor"] = time.monotonic()
        self.pipeline.report_processes["auditor"] = MagicMock(poll=lambda: None)
        with patch.object(self.pipeline, 'run_next') as mock_run:
            mock_run.return_value = {"status": "complete"}
            result = self.pipeline.on_phase_finished("auditor", True, "VERDICT: PASS")
        assert "renovator" not in self.pipeline.pipeline_queue


# ---------------------------------------------------------------------------
# Event-sink ordering contract
# ---------------------------------------------------------------------------

class TestEventSinkOrdering:
    def setup_method(self):
        reset_collector()
        self.manager = FakeManager()
        self.pipeline = Pipeline(self.manager, make_state, make_sink())

    @patch("modeldeck.pipeline.resolve_ai_path")
    def test_full_suite_event_sequence(self, mock_resolve):
        mock_resolve.return_value = _mock_path(exists_val=True)

        def fake_run_next():
            if not self.pipeline.pipeline_queue:
                return {"status": "complete"}
            phase = self.pipeline.pipeline_queue.pop(0)
            self.pipeline.pipeline_current_phase = phase
            self.pipeline.event_sink({"type": "phase_status", "phase": phase, "status": "preparing model"})
            self.pipeline.report_start_times[phase] = time.monotonic()
            return {"status": "running", "phase": phase}

        with patch.object(self.pipeline, 'run_next', side_effect=fake_run_next):
            self.pipeline.start("full", "/tmp/project", "task", "scout")

        status_events = [e for e in collector if e.get("type") == "phase_status"]
        assert len(status_events) >= 1
        first = status_events[0]
        assert first["phase"] == "scout"


# ---------------------------------------------------------------------------
# Turn budget and token accounting
# ---------------------------------------------------------------------------

class TestTurnsAndTokens:
    def setup_method(self):
        reset_collector()
        self.manager = FakeManager()
        self.pipeline = Pipeline(self.manager, make_state, make_sink())

    def _run(self, phase, stdout_text):
        mock_proc = MagicMock()
        mock_proc.stdout = io.StringIO(stdout_text)
        mock_proc.wait.return_value = 0
        mock_proc.stdin = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            with patch("modeldeck.pipeline.PROJECT_DIR", Path("/tmp/fake")):
                with patch.object(Path, "read_text", return_value="Report content"):
                    self.pipeline._pipeline_worker(phase, "task", "/tmp/project")
        return mock_popen.call_args[0][0]

    def test_agentic_phase_passes_its_turn_budget_to_the_script(self):
        # The number lives in role config so it is visible and editable in
        # the Deck tab, rather than being a script default nobody can see.
        argv = self._run("builder", "Wrote /tmp/out/report.md\n")
        assert "--max-steps" in argv
        assert argv[argv.index("--max-steps") + 1] == "80"

    def test_one_shot_phase_gets_no_turn_budget(self):
        argv = self._run("scout", "Wrote /tmp/out/report.md\n")
        assert "--max-steps" not in argv

    def test_step_lines_are_reported_as_turn_events(self):
        # This is the visible answer to "did it run out of turns, or did it
        # actually finish?" -- a run that stops at its cap looks like a
        # quality failure unless the turn count is on screen.
        self._run("builder", "--- step 1 ---\n--- step 2 ---\nWrote /tmp/out/report.md\n")
        turns = [e for e in collector if e.get("type") == "turns"]
        assert [e["used"] for e in turns] == [0, 1, 2]
        assert {e["limit"] for e in turns} == {80}

    def test_token_delta_subtracts_the_starting_totals(self):
        before = {"prompt_tokens": 1_000, "completion_tokens": 200, "requests": 3}
        after = {"prompt_tokens": 4_500, "completion_tokens": 900, "requests": 5}
        assert Pipeline._token_delta(before, after) == {
            "prompt_tokens": 3_500, "completion_tokens": 700, "requests": 2,
        }

    def test_token_delta_survives_a_relaunch_resetting_the_counters(self):
        # Counters restart at zero when a model is relaunched mid-phase; the
        # naive subtraction would go negative and read as "used -30k tokens".
        before = {"prompt_tokens": 50_000, "completion_tokens": 9_000}
        after = {"prompt_tokens": 1_200, "completion_tokens": 300}
        assert Pipeline._token_delta(before, after) == {
            "prompt_tokens": 1_200, "completion_tokens": 300,
        }

    def test_no_resident_model_yields_no_token_numbers(self):
        # Better to show nothing than to show a fabricated zero.
        assert Pipeline._token_delta({"prompt_tokens": 5}, {}) == {}
