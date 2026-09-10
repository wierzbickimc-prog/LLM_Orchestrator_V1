from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .mtplx import PROJECT_DIR, ProcessManager, fetch_sse_snapshot
from .state import activate_local, activate_planner, load_state, save_state

# build_args receives (task, path, flow) -- flow is "feature" or
# "troubleshoot" and only matters where the two flows feed a phase
# different inputs (Planner: a scout report vs. a diagnosis report).
REPORT_SCRIPTS: dict[str, dict[str, Any]] = {
    "scout": {
        "script": "scout_report.py",
        "task_required": True,
        "out": ".ai/scout-report.md",
        "build_args": lambda task, path, flow: [task, path],
    },
    "diagnose": {
        "script": "diagnose_report.py",
        "task_required": True,
        "out": ".ai/diagnosis-report.md",
        "build_args": lambda task, path, flow: [task, path],
    },
    "planner": {
        "script": "planner_report.py",
        "task_required": False,
        "out": ".ai/implementation-plan.md",
        "build_args": lambda task, path, flow: [
            path,
            *(["--task", task] if task else []),
            *(
                [
                    "--troubleshoot",
                    "--scout-report",
                    str(resolve_ai_path(Path(path), "diagnosis-report.md")),
                ]
                if flow == "troubleshoot" else []
            ),
        ],
    },
    "builder": {
        "script": "builder_agent.py",
        "task_required": False,
        "out": ".ai/builder-report.md",
        "build_args": lambda task, path, flow: [path, *(["--task", task] if task else [])],
    },
    "auditor": {
        "script": "auditor_report.py",
        "task_required": False,
        "out": ".ai/audit-report.md",
        "build_args": lambda task, path, flow: [path, *(["--task", task] if task else [])],
    },
    "renovator": {
        "script": "renovator_agent.py",
        "task_required": False,
        "out": ".ai/renovator-report.md",
        "build_args": lambda task, path, flow: [path],
    },
}

PIPELINE_ORDER: tuple[str, ...] = ("scout", "planner", "builder", "auditor")
# Troubleshoot swaps Scout's open-ended survey for a diff-anchored Diagnose
# phase; Planner/Builder/Auditor downstream are unchanged (Planner just
# reads .ai/diagnosis-report.md instead of .ai/scout-report.md).
TROUBLESHOOT_ORDER: tuple[str, ...] = ("diagnose", "planner", "builder", "auditor")
FLOW_ORDERS: dict[str, tuple[str, ...]] = {
    "feature": PIPELINE_ORDER,
    "troubleshoot": TROUBLESHOOT_ORDER,
}
STATUS_PHASES: tuple[str, ...] = ("scout", "diagnose", "planner", "builder", "auditor", "renovator")
MAX_RENOVATOR_RETRIES = 1

# Diagnose has no role of its own -- it runs on the Auditor's model/config
# (same defect-finding task). This maps the phase name to the role the
# process manager should have resident and the alias the script calls.
PHASE_ROLE_OVERRIDE: dict[str, str] = {"diagnose": "auditor"}

REQUIRED_ARTIFACT_FOR_START: dict[str, str] = {
    "scout": "",
    "planner": "scout-report.md",
    "builder": "implementation-plan.md",
    "auditor": "implementation-plan.md",
}
TROUBLESHOOT_REQUIRED_ARTIFACT_FOR_START: dict[str, str] = {
    **REQUIRED_ARTIFACT_FOR_START,
    "diagnose": "",
    "planner": "diagnosis-report.md",
}

# Add scripts directory to path for report_common imports
sys.path.insert(0, str(PROJECT_DIR / "scripts"))
from report_common import parse_verdict, resolve_ai_path  # noqa: E402


class Pipeline:
    def __init__(
        self,
        manager: ProcessManager,
        state_provider: Callable[[], dict[str, Any]],
        event_sink: Callable[[dict[str, Any]], None],
    ):
        self.manager = manager
        self.state_provider = state_provider
        self.event_sink = event_sink
        self.pipeline_queue: list[str] = []
        self.pipeline_mode: str | None = None
        self.pipeline_flow: str = "feature"
        self.pipeline_task: str = ""
        self.pipeline_path: str = ""
        self.pipeline_current_phase: str | None = None
        self.pipeline_renovator_retries: int = 0
        self._pipeline_resident_role: str | None = None
        self.report_start_times: dict[str, float] = {}
        self.report_processes: dict[str, subprocess.Popen] = {}
        self._pending_answers: dict[str, tuple[threading.Event, list[str]]] = {}
        self._last_prefill_rate: dict[str, float] = {}

    def start(
        self, mode: str, path: str, task: str, start_phase: str, flow: str = "feature"
    ) -> dict[str, Any]:
        if not path:
            return {"error": "Path required"}
        order = FLOW_ORDERS.get(flow, PIPELINE_ORDER)
        if start_phase not in order:
            return {"error": f"{start_phase!r} is not a phase in the {flow} flow"}
        if start_phase == order[0] and not task:
            return {"error": "Task required"}

        required_map = (
            TROUBLESHOOT_REQUIRED_ARTIFACT_FOR_START if flow == "troubleshoot"
            else REQUIRED_ARTIFACT_FOR_START
        )
        required = required_map.get(start_phase, "")
        if required:
            required_path = resolve_ai_path(Path(path), required)
            if not required_path.exists():
                return {"error": f"Missing prerequisite: {required_path}"}

        self.pipeline_mode = mode
        self.pipeline_flow = flow
        self.pipeline_task = task
        self.pipeline_path = path
        start_index = order.index(start_phase)
        self.pipeline_queue = list(order[start_index:])
        self.pipeline_renovator_retries = 0
        self._pipeline_resident_role = None
        self.report_start_times = {}
        self.report_processes = {}
        self._pending_answers = {}
        self._last_prefill_rate = {}

        # Emit skipped phases
        for skipped_phase in order[:start_index]:
            self.event_sink({
                "type": "phase_status",
                "phase": skipped_phase,
                "status": f"skipped (resumed)",
            })

        return self.run_next()

    def run_next(self) -> dict[str, Any]:
        if not self.pipeline_queue:
            return {"status": "complete"}

        phase = self.pipeline_queue.pop(0)
        self.pipeline_current_phase = phase
        self._activate_for_report(phase)

        self.event_sink({
            "type": "phase_status",
            "phase": phase,
            "status": "preparing model",
        })

        self.report_start_times[phase] = time.monotonic()
        threading.Thread(
            target=self._pipeline_worker,
            args=(phase, self.pipeline_task, self.pipeline_path),
            daemon=True,
        ).start()

        return {"status": "running", "phase": phase}

    def _activate_for_report(self, phase: str) -> None:
        state = load_state()
        if phase == "planner":
            activate_planner(state)
        else:
            activate_local(state, PHASE_ROLE_OVERRIDE.get(phase, phase))
        save_state(state)

    def _phase_local_role(self, phase: str) -> str | None:
        state = self.state_provider()
        if phase == "planner":
            # Planner has its own role now, rather than borrowing another
            # phase's. None still means "cloud planner, nothing local to
            # launch".
            if state["planner"].get("kind") == "local":
                return "planner"
            return None
        return PHASE_ROLE_OVERRIDE.get(phase, phase)

    def _ensure_role_resident(self, role_name: str | None) -> None:
        if role_name is None:
            return
        if self._pipeline_resident_role == role_name:
            return
        state = load_state()
        ports = [int(r["port"]) for r in state["roles"].values()]
        self.manager.stop_local_models(ports)
        self.manager.launch(role_name, state["roles"][role_name])
        self._pipeline_resident_role = role_name



    @staticmethod
    def _token_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
        """Tokens attributable to one phase. A negative delta means the model
        was relaunched mid-phase (counters restarted at zero), in which case
        the post-relaunch total is the best available number."""
        if not after:
            return {}
        delta = {}
        for key, value in after.items():
            diff = value - int(before.get(key, 0))
            delta[key] = diff if diff >= 0 else value
        return delta

    _STEP_RE = re.compile(r"^--- step (\d+) ---")

    @staticmethod
    def _lifetime_totals(role_name: str | None, state: dict[str, Any]) -> dict[str, int]:
        """Cumulative token counters for the model serving this role, or an
        empty dict if nothing is resident/reachable. Diffing a snapshot from
        before and after a phase gives that phase's own token usage, which
        works whether or not the model was relaunched in between (a
        relaunch resets the counters to zero, which the diff handles the
        same as any other start point)."""
        if role_name is None:
            return {}
        try:
            port = int(state["roles"][role_name]["port"])
        except (KeyError, TypeError, ValueError):
            return {}
        try:
            snapshot = fetch_sse_snapshot(
                f"http://127.0.0.1:{port}/v1/mtplx/metrics/stream?snapshot_interval_ms=50",
                timeout=1.0,
            )
        except Exception:
            return {}
        lifetime = snapshot.get("lifetime") or {}
        return {
            "prompt_tokens": int(lifetime.get("prompt_tokens_total") or 0),
            "completion_tokens": int(lifetime.get("completion_tokens_total") or 0),
            "requests": int(lifetime.get("requests_total") or 0),
        }

    def _pipeline_worker(self, phase: str, task: str, path: str) -> None:
        try:
            self._ensure_role_resident(self._phase_local_role(phase))
        except Exception as exc:
            self.event_sink({
                "type": "finished",
                "phase": phase,
                "success": False,
                "content": f"Could not prepare the model for {phase}: {exc}",
            })
            return

        state = self.state_provider()
        role_name = self._phase_local_role(phase)
        tokens_before = self._lifetime_totals(role_name, state)

        config = REPORT_SCRIPTS[phase]
        python = PROJECT_DIR / ".venv" / "bin" / "python"
        script_path = PROJECT_DIR / "scripts" / config["script"]
        args = [str(a) for a in config["build_args"](task, path, self.pipeline_flow)]

        # Agentic phases get their turn budget from role config, so it is
        # visible and editable in the Deck tab rather than buried as a
        # script default -- running out of turns is a real failure mode
        # that reads as a quality problem if you cannot see the number.
        max_steps = int((state["roles"].get(role_name) or {}).get("max_steps") or 0) if role_name else 0
        if max_steps and phase in ("builder", "renovator"):
            args += ["--max-steps", str(max_steps)]
            self.event_sink({
                "type": "turns", "phase": phase, "used": 0, "limit": max_steps,
            })
        try:
            process = subprocess.Popen(
                [str(python), str(script_path), *args],
                cwd=PROJECT_DIR,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            self.event_sink({
                "type": "finished",
                "phase": phase,
                "success": False,
                "content": str(exc),
            })
            return

        self.report_processes[phase] = process
        collected: list[str] = []
        assert process.stdout is not None
        line_buffer = ""
        while True:
            piece = process.stdout.read(64)
            if not piece:
                break
            collected.append(piece)
            self.event_sink({
                "type": "chunk",
                "phase": phase,
                "content": piece,
            })
            line_buffer += piece
            while "\n" in line_buffer:
                line, line_buffer = line_buffer.split("\n", 1)
                if line.startswith("[ASK_QUESTION] "):
                    self._handle_ask_question(phase, process, line[len("[ASK_QUESTION] "):])
                    continue
                step_match = self._STEP_RE.match(line)
                if step_match and max_steps:
                    self.event_sink({
                        "type": "turns",
                        "phase": phase,
                        "used": int(step_match.group(1)),
                        "limit": max_steps,
                    })
        returncode = process.wait()
        output_text = "".join(collected)

        if returncode != 0:
            prefix = "Stopped by user.\n\n" if returncode < 0 else ""
            self.event_sink({
                "type": "finished",
                "phase": phase,
                "success": False,
                "content": prefix + (output_text.strip() or f"exit code {returncode}"),
            })
            return
        out_path = self._parse_wrote_path(output_text)
        if out_path is None:
            self.event_sink({
                "type": "finished",
                "phase": phase,
                "success": False,
                "content": f"Script exited 0 but its output path wasn't found in:\n{output_text}",
            })
            return
        try:
            content = out_path.read_text()
        except OSError as exc:
            self.event_sink({
                "type": "finished",
                "phase": phase,
                "success": False,
                "content": f"Script succeeded but {out_path} could not be read: {exc}",
            })
            return
        tokens_after = self._lifetime_totals(role_name, state)
        self.event_sink({
            "type": "finished",
            "phase": phase,
            "success": True,
            "content": content,
            "tokens": self._token_delta(tokens_before, tokens_after),
        })

    def _handle_ask_question(self, phase: str, process: subprocess.Popen, payload: str) -> None:
        answer_event = threading.Event()
        answer_box: list[str] = []
        self._pending_answers[phase] = (answer_event, answer_box)
        self.event_sink({
            "type": "ask_question",
            "phase": phase,
            "payload": payload,
        })
        answer_event.wait()
        answer = answer_box[0] if answer_box else ""
        assert process.stdin is not None
        try:
            process.stdin.write(answer + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def answer(self, phase: str, answer: str) -> None:
        event, box = self._pending_answers.pop(phase, (None, None))
        if box is not None:
            box.append(answer)
        if event is not None:
            event.set()

    def stop(self) -> None:
        phase = self.pipeline_current_phase
        if phase is None:
            return
        process = self.report_processes.get(phase)
        if process is None or process.poll() is not None:
            return
        self.pipeline_queue.clear()
        self.event_sink({
            "type": "phase_status",
            "phase": phase,
            "status": "stopping",
        })
        process.terminate()
        # Unblock any pending question
        event, box = self._pending_answers.pop(phase, (None, None))
        if box is not None:
            box.append("")
        if event is not None:
            event.set()

    def on_phase_finished(self, phase: str, success: bool, text: str) -> dict[str, Any]:
        """Called by the event handler (GUI or Web) after processing 'finished' event."""
        self.report_processes.pop(phase, None)
        start = self.report_start_times.pop(phase, None)
        elapsed = f" ({time.monotonic() - start:.0f}s)" if start is not None else ""

        self.event_sink({
            "type": "phase_status",
            "phase": phase,
            "status": f"{'done' if success else 'failed'}{elapsed}",
        })

        if not success:
            self.pipeline_queue.clear()
            return {"status": "failed", "phase": phase}

        if phase == "auditor":
            verdict = parse_verdict(text)
            if verdict != "PASS":
                if self.pipeline_renovator_retries < MAX_RENOVATOR_RETRIES:
                    self.pipeline_renovator_retries += 1
                    self.pipeline_queue = ["renovator", "auditor"]
                    self.event_sink({
                        "type": "notification",
                        "message": f"Auditor verdict: {verdict}. Queuing one repair pass (Renovator, retry {self.pipeline_renovator_retries}/{MAX_RENOVATOR_RETRIES})",
                    })
                else:
                    self.pipeline_queue.clear()
                    return {"status": "failed", "phase": phase, "reason": "Auditor rejected after retries"}

        if self.pipeline_mode == "full":
            return self.run_next()

        if self.pipeline_queue:
            return {"status": "step_paused", "next_phase": self.pipeline_queue[0]}
        else:
            return {"status": "complete"}

    def status(self) -> dict[str, Any]:
        return {
            "current_phase": self.pipeline_current_phase,
            "queue": list(self.pipeline_queue),
            "renovator_retries": self.pipeline_renovator_retries,
            "per_phase_status": {
                phase: f"running... {time.monotonic() - start:.0f}s"
                for phase, start in self.report_start_times.items()
                if phase in self.report_processes and self.report_processes[phase].poll() is None
            },
            "processes_alive": {
                phase: self.report_processes[phase].poll() is None
                for phase in self.report_processes
            },
        }

    @staticmethod
    def _parse_wrote_path(stdout: str) -> Path | None:
        for line in stdout.splitlines():
            if line.startswith("Wrote "):
                raw = Path(line[len("Wrote "):].strip())
                return raw if raw.is_absolute() else PROJECT_DIR / raw
        return None
