from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFontDatabase, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .mtplx import PROJECT_DIR, ProcessManager, fetch_json, fetch_sse_snapshot, installed_models, post_json
from .prompts import PROMPTS
from .secrets import get_openai_api_key, set_openai_api_key
from .state import activate_local, activate_planner, load_state, sampling_preset, save_state

sys.path.insert(0, str(PROJECT_DIR / "scripts"))
from report_common import parse_verdict, resolve_ai_path  # noqa: E402


# Scripts run against the router for each phase -- scout/planner/auditor are
# single tool-free calls (see scripts/report_common.py); builder is a small
# purpose-built agent loop (see scripts/builder_agent.py) since it actually
# has to edit files and run commands, which the others never do.
REPORT_SCRIPTS: dict[str, dict[str, Any]] = {
    "scout": {
        "script": "scout_report.py",
        "task_required": True,
        "out": ".ai/scout-report.md",
        "build_args": lambda task, path: [task, path],
    },
    "planner": {
        "script": "planner_report.py",
        "task_required": False,
        "out": ".ai/implementation-plan.md",
        "build_args": lambda task, path: [path, *(["--task", task] if task else [])],
    },
    "builder": {
        "script": "builder_agent.py",
        "task_required": False,
        "out": ".ai/builder-report.md",
        "build_args": lambda task, path: [path, *(["--task", task] if task else [])],
    },
    "auditor": {
        "script": "auditor_report.py",
        "task_required": False,
        "out": ".ai/audit-report.md",
        "build_args": lambda task, path: [path, *(["--task", task] if task else [])],
    },
    "renovator": {
        "script": "renovator_agent.py",
        "task_required": False,
        "out": ".ai/renovator-report.md",
        # Scope comes entirely from audit-report.md's Fix List, not a free-form
        # task -- see renovator_agent.py's docstring.
        "build_args": lambda task, path: [path],
    },
}


class Bridge(QObject):
    succeeded = Signal(str, str)
    failed = Signal(str, str)


class ReportBridge(QObject):
    finished = Signal(str, bool, str)  # phase, success, report content or error text
    chunk = Signal(str, str)  # phase, text piece as it streams in
    ask_question = Signal(str, str)  # phase, JSON {"question": ..., "options": [...] | None}


PIPELINE_ORDER: tuple[str, ...] = ("scout", "planner", "builder", "auditor")
# Renovator only ever runs after an Auditor REJECT, appended to the queue
# dynamically (see MainWindow._report_finished) -- but its status label is
# created up front alongside the other four so the row doesn't jump around
# when the loop actually fires.
STATUS_PHASES: tuple[str, ...] = PIPELINE_ORDER + ("renovator",)
MAX_RENOVATOR_RETRIES = 1
# What must already exist under <path>/.ai/ to start a run at this phase
# instead of from Scout -- e.g. resuming at Builder after a Stop or a
# rejected-twice Auditor verdict, without redoing Scout/Planner. Scout has
# no prerequisite: it scans the target fresh.
REQUIRED_ARTIFACT_FOR_START: dict[str, str] = {
    "scout": "",
    "planner": "scout-report.md",
    "builder": "implementation-plan.md",
    "auditor": "implementation-plan.md",
}


class PipelinePanel(QGroupBox):
    """One task + one path, run against all four phases either automatically
    (Full Suite) or one phase at a time with a pause for review between each
    (Step-by-Step) -- see MainWindow._start_pipeline and friends."""

    def __init__(self):
        super().__init__("Pipeline")
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.path_field = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path_field, 1)
        path_row.addWidget(browse)
        form.addRow("Path", path_row)
        layout.addLayout(form)

        task_label = QLabel("Task")
        layout.addWidget(task_label)
        self.task_field = QPlainTextEdit()
        self.task_field.setPlaceholderText("what the change/investigation is for")
        self.task_field.setMinimumHeight(140)
        layout.addWidget(self.task_field)

        # Lets a run resume after a stop -- a rejected Auditor verdict that
        # used up its retry, a manual Stop click, or a crash -- without
        # redoing already-completed (and possibly expensive) earlier phases.
        # Starting anywhere but Scout requires that phase's input artifact
        # to already exist on disk (checked in MainWindow._start_pipeline).
        start_row = QHBoxLayout()
        start_row.addWidget(QLabel("Start from"))
        self.start_phase = QComboBox()
        for phase in PIPELINE_ORDER:
            self.start_phase.addItem(phase.title(), phase)
        start_row.addWidget(self.start_phase, 1)
        layout.addLayout(start_row)

        run_row = QHBoxLayout()
        self.full_suite_button = QPushButton("Run Full Suite")
        self.full_suite_button.setObjectName("launchButton")
        self.step_wise_button = QPushButton("Run Step-by-Step")
        self.continue_button = QPushButton("Continue →")
        self.continue_button.setObjectName("launchButton")
        self.continue_button.setEnabled(False)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setEnabled(False)
        run_row.addWidget(self.full_suite_button)
        run_row.addWidget(self.step_wise_button)
        run_row.addWidget(self.continue_button)
        run_row.addWidget(self.stop_button)
        layout.addLayout(run_row)

        # Grid, not a single row -- five labels (four phases plus the
        # conditional Renovator) in one QHBoxLayout get squeezed/clipped on
        # a narrower window instead of wrapping, which is exactly the kind
        # of "I can't tell what's happening" gap this row exists to avoid.
        status_columns = 3
        status_grid = QGridLayout()
        self.phase_status: dict[str, QLabel] = {}
        for index, phase in enumerate(STATUS_PHASES):
            label = QLabel(f"{phase.title()}: pending")
            label.setObjectName("reportStatus")
            status_grid.addWidget(label, index // status_columns, index % status_columns)
            self.phase_status[phase] = label
        layout.addLayout(status_grid)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText(
            "Output streams here as each phase runs -- .ai/scout-report.md, "
            "implementation-plan.md, builder-report.md, audit-report.md, in order."
        )
        mono_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono_font.setPointSize(11)
        self.output.setFont(mono_font)
        self.output.setMinimumHeight(320)
        layout.addWidget(self.output)

    def _browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose a file or directory to scan")
        if directory:
            self.path_field.setText(directory)

    def reset_status(self) -> None:
        for phase, label in self.phase_status.items():
            label.setText(f"{phase.title()}: pending")


class SecretDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("OpenAI API Key")
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "The key is stored in macOS Keychain and is never written to this project."
            )
        )
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("sk-…")
        layout.addWidget(self.key)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class RoleEditor(QGroupBox):
    def __init__(self, title: str, models: list[dict[str, Any]], role: dict[str, Any]):
        super().__init__(title)
        self.models = QComboBox()
        for model in models:
            label = f"{model['repo_id']}  ·  {model['size_gb']:.1f} GB"
            self.models.addItem(label, model["repo_id"])
        current = self.models.findData(role["model"])
        if current >= 0:
            self.models.setCurrentIndex(current)
        else:
            self.models.addItem(str(role["model"]), role["model"])
            self.models.setCurrentIndex(self.models.count() - 1)

        self.reasoning = QComboBox()
        self.reasoning.addItems(["off", "auto", "on"])
        self.reasoning.setCurrentText(str(role["reasoning"]))
        self.context = QSpinBox()
        self.context.setRange(4096, 262144)
        self.context.setSingleStep(1024)
        self.context.setValue(int(role["context_window"]))
        self.depth = QSpinBox()
        self.depth.setRange(1, 8)
        self.depth.setValue(int(role["depth"]))

        # Per-request sampling -- sent by the router on every call to this
        # alias (see router/main.py's local-role Backend resolution), not a
        # launch flag, so changing these takes effect on the next request
        # with no model restart needed.
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setSingleStep(0.05)
        self.temperature.setDecimals(2)
        self.temperature.setValue(float(role.get("temperature", 0.7)))
        self.top_p = QDoubleSpinBox()
        self.top_p.setRange(0.0, 1.0)
        self.top_p.setSingleStep(0.05)
        self.top_p.setDecimals(2)
        self.top_p.setValue(float(role.get("top_p", 0.9)))
        self.top_k = QSpinBox()
        self.top_k.setRange(0, 500)
        self.top_k.setValue(int(role.get("top_k", 40)))
        self.min_p = QDoubleSpinBox()
        self.min_p.setRange(0.0, 1.0)
        self.min_p.setSingleStep(0.05)
        self.min_p.setDecimals(2)
        self.min_p.setValue(float(role.get("min_p", 0.0)))
        self.presence_penalty = QDoubleSpinBox()
        self.presence_penalty.setRange(-2.0, 2.0)
        self.presence_penalty.setSingleStep(0.1)
        self.presence_penalty.setDecimals(2)
        self.presence_penalty.setValue(float(role.get("presence_penalty", 0.0)))
        self.repetition_penalty = QDoubleSpinBox()
        self.repetition_penalty.setRange(0.5, 2.0)
        self.repetition_penalty.setSingleStep(0.05)
        self.repetition_penalty.setDecimals(2)
        self.repetition_penalty.setValue(float(role.get("repetition_penalty", 1.0)))

        # Officially published preset per model+mode (see modeldeck.state.
        # SAMPLING_PRESETS) -- "Apply preset" snaps the six fields above to
        # it; it's a starting point, not a lock, so values can still be
        # hand-tuned afterward. sampling_mode itself is also persisted (see
        # apply() below) so the choice survives a restart.
        self.sampling_mode = QComboBox()
        self.sampling_mode.addItem("Thinking", "thinking")
        self.sampling_mode.addItem("Thinking (precise coding)", "thinking_precise")
        self.sampling_mode.addItem("Instruct (non-thinking)", "instruct")
        mode_index = self.sampling_mode.findData(str(role.get("sampling_mode", "thinking")))
        if mode_index >= 0:
            self.sampling_mode.setCurrentIndex(mode_index)
        apply_preset = QPushButton("Apply preset →")
        apply_preset.clicked.connect(self._apply_sampling_preset)

        form = QFormLayout(self)
        form.addRow("Model", self.models)
        compact = QHBoxLayout()
        compact.addWidget(QLabel("Context"))
        compact.addWidget(self.context)
        compact.addWidget(QLabel("Depth"))
        compact.addWidget(self.depth)
        compact.addWidget(QLabel("Reasoning"))
        compact.addWidget(self.reasoning)
        form.addRow(compact)
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Sampling preset"))
        preset_row.addWidget(self.sampling_mode, 1)
        preset_row.addWidget(apply_preset)
        form.addRow(preset_row)
        sampling = QHBoxLayout()
        sampling.addWidget(QLabel("Temperature"))
        sampling.addWidget(self.temperature)
        sampling.addWidget(QLabel("Top-p"))
        sampling.addWidget(self.top_p)
        sampling.addWidget(QLabel("Top-k"))
        sampling.addWidget(self.top_k)
        form.addRow(sampling)
        sampling2 = QHBoxLayout()
        sampling2.addWidget(QLabel("Min-p"))
        sampling2.addWidget(self.min_p)
        sampling2.addWidget(QLabel("Presence pen."))
        sampling2.addWidget(self.presence_penalty)
        sampling2.addWidget(QLabel("Repetition pen."))
        sampling2.addWidget(self.repetition_penalty)
        form.addRow(sampling2)

    def _apply_sampling_preset(self) -> None:
        model = str(self.models.currentData())
        mode = str(self.sampling_mode.currentData())
        preset = sampling_preset(model, mode)
        if preset is None:
            QMessageBox.information(
                self, "No published preset",
                f"No official sampling preset is published for this model in "
                f"\"{self.sampling_mode.currentText()}\" mode. Values are unchanged -- "
                "adjust them by hand if needed.",
            )
            return
        self.temperature.setValue(preset["temperature"])
        self.top_p.setValue(preset["top_p"])
        self.top_k.setValue(int(preset["top_k"]))
        self.min_p.setValue(preset["min_p"])
        self.presence_penalty.setValue(preset["presence_penalty"])
        self.repetition_penalty.setValue(preset["repetition_penalty"])

    def apply(self, role: dict[str, Any]) -> None:
        role["model"] = self.models.currentData()
        role["reasoning"] = self.reasoning.currentText()
        role["context_window"] = self.context.value()
        role["depth"] = self.depth.value()
        role["sampling_mode"] = self.sampling_mode.currentData()
        role["temperature"] = self.temperature.value()
        role["top_p"] = self.top_p.value()
        role["top_k"] = self.top_k.value()
        role["min_p"] = self.min_p.value()
        role["presence_penalty"] = self.presence_penalty.value()
        role["repetition_penalty"] = self.repetition_penalty.value()


class PromptCard(QGroupBox):
    """Editable, per-phase chat-client injection text (see modeldeck.prompts.
    effective_prompt). save_callback(phase, text) is called on Save and is
    responsible for persisting to state.json's "prompt_overrides"; reset_
    callback(phase) restores the built-in default from modeldeck/prompts.py.
    Editing here has no effect on scripts/*_report.py or renovator_agent.py,
    which carry their own complete system prompts -- see the banner in
    MainWindow._build_admin."""

    def __init__(
        self,
        phase: str,
        document: str,
        prompt: str,
        is_override: bool,
        save_callback,
        reset_callback,
    ):
        super().__init__(phase.title())
        self.phase = phase
        self.save_callback = save_callback
        self.reset_callback = reset_callback
        layout = QVBoxLayout(self)
        expected = QLabel(f"Produces: {document}")
        expected.setObjectName("documentLabel")
        layout.addWidget(expected)
        self.status_label = QLabel()
        self.status_label.setObjectName("documentLabel")
        layout.addWidget(self.status_label)
        self.prompt = QPlainTextEdit(prompt)
        prompt_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        prompt_font.setPointSize(11)
        self.prompt.setFont(prompt_font)
        self.prompt.setMinimumHeight(165)
        layout.addWidget(self.prompt)
        button_row = QHBoxLayout()
        copy = QPushButton("Copy prompt")
        copy.clicked.connect(self.copy_prompt)
        button_row.addWidget(copy)
        button_row.addStretch()
        reset = QPushButton("Reset to default")
        reset.clicked.connect(self._on_reset)
        button_row.addWidget(reset)
        save = QPushButton("Save")
        save.setObjectName("launchButton")
        save.clicked.connect(self._on_save)
        button_row.addWidget(save)
        layout.addLayout(button_row)
        self.set_override_state(is_override)

    def set_override_state(self, is_override: bool) -> None:
        self.status_label.setText(
            "Edited -- overriding the built-in default below" if is_override
            else "Using the built-in default (never edited)"
        )

    def copy_prompt(self) -> None:
        QApplication.clipboard().setText(self.prompt.toPlainText())

    def _on_save(self) -> None:
        self.save_callback(self.phase, self.prompt.toPlainText())
        self.set_override_state(True)

    def _on_reset(self) -> None:
        default_text = self.reset_callback(self.phase)
        self.prompt.setPlainText(default_text)
        self.set_override_state(False)


class MetricCard(QFrame):
    """One dashboard tile: a small heading over a big value, used for the
    live process-parameter grid in the Reports tab (see MainWindow.
    _build_telemetry / _refresh_flight)."""

    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        heading = QLabel(title.upper())
        heading.setObjectName("metricHeading")
        self.value = QLabel("—")
        self.value.setObjectName("metricValue")
        self.value.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(self.value)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Model Deck")
        self.resize(1420, 900)
        self.state = load_state()
        self.manager = ProcessManager()
        self.bridge = Bridge()
        self.bridge.succeeded.connect(self._phase_ready)
        self.bridge.failed.connect(self._phase_failed)
        self.report_bridge = ReportBridge()
        self.report_bridge.finished.connect(self._report_finished)
        self.report_bridge.chunk.connect(self._report_chunk)
        self.report_bridge.ask_question.connect(self._on_ask_question)
        self.report_start_times: dict[str, float] = {}
        self.report_processes: dict[str, subprocess.Popen] = {}
        # phase -> (Event the worker thread blocks on, single-item list the
        # main-thread dialog handler drops the answer into before setting it)
        self._pending_answers: dict[str, tuple[threading.Event, list[str]]] = {}
        self.pipeline_mode: str | None = None
        self.pipeline_task: str = ""
        self.pipeline_path: str = ""
        self.pipeline_queue: list[str] = []
        self.pipeline_current_phase: str | None = None
        self.pipeline_renovator_retries: int = 0
        self._pipeline_resident_role: str | None = None
        # phase -> last-measured prefill_tok_s from /metrics (populated only
        # after a request completes) -- carried forward to estimate an ETA
        # for the *next* request's prefill phase, since mtplx's live /v1/
        # mtplx/flight endpoint reports prompt_tokens but no live prefill
        # progress or rate (confirmed empirically: "prefill" stays null
        # throughout an active prefill, not just before/after it).
        self._last_prefill_rate: dict[str, float] = {}
        self.report_timer = QTimer(self)
        self.report_timer.timeout.connect(self._tick_report_status)
        self.report_timer.start(1000)
        self.busy = False
        self.loop_dialog_open = False

        try:
            models = installed_models()
        except Exception as exc:
            models = []
            QTimer.singleShot(
                0,
                lambda: QMessageBox.warning(
                    self, "Model inventory", f"Could not read MTPLX models: {exc}"
                ),
            )

        tabs = QTabWidget()
        tabs.addTab(self._build_controls(models), "Deck")
        tabs.addTab(self._build_reports(), "Reports")
        tabs.addTab(self._build_admin(), "Admin")
        self.setCentralWidget(tabs)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_metrics)
        self.timer.start(1200)
        self.refresh_metrics()

    def _build_controls(self, models: list[dict[str, Any]]) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(22, 18, 22, 22)

        top = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Model Deck")
        title.setObjectName("title")
        subtitle = QLabel("One endpoint. Four deliberate phases.")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        top.addLayout(title_box)
        top.addStretch()
        self.active_badge = QLabel("ACTIVE · —")
        self.active_badge.setObjectName("activeBadge")
        top.addWidget(self.active_badge)
        layout.addLayout(top)

        endpoint = QLabel("Chat client  ·  http://127.0.0.1:8100/v1  ·  model: local")
        endpoint.setObjectName("endpoint")
        endpoint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(endpoint)

        phase_row = QHBoxLayout()
        self.phase_buttons: dict[str, QPushButton] = {}
        for phase, label in (
            ("scout", "Scout"),
            ("planner", "Plan with GPT"),
            ("builder", "Build"),
            ("auditor", "Audit"),
        ):
            button = QPushButton(label)
            button.setObjectName("phaseButton")
            button.clicked.connect(lambda _checked=False, p=phase: self.activate(p))
            phase_row.addWidget(button)
            self.phase_buttons[phase] = button
        layout.addLayout(phase_row)

        launch_row = QHBoxLayout()
        self.start_phase = QComboBox()
        self.start_phase.addItem("Scout", "scout")
        self.start_phase.addItem("Builder", "builder")
        self.start_phase.addItem("Auditor", "auditor")
        self.launch_button = QPushButton("Launch Models")
        self.launch_button.setObjectName("launchButton")
        self.launch_button.clicked.connect(
            lambda: self.activate(str(self.start_phase.currentData()))
        )
        launch_row.addWidget(self.start_phase)
        launch_row.addWidget(self.launch_button, 1)
        layout.addLayout(launch_row)

        planner_box = QGroupBox("GPT Planner")
        planner_form = QFormLayout(planner_box)
        self.planner_kind = QComboBox()
        self.planner_kind.addItem("Cloud (OpenAI)", "openai")
        self.planner_kind.addItem("Local model", "local")
        kind_index = self.planner_kind.findData(str(self.state["planner"].get("kind", "openai")))
        if kind_index >= 0:
            self.planner_kind.setCurrentIndex(kind_index)
        self.planner_local_phase = QComboBox()
        for phase in ("scout", "builder", "renovator", "auditor"):
            self.planner_local_phase.addItem(phase.title(), phase)
        phase_index = self.planner_local_phase.findData(
            str(self.state["planner"].get("local_phase", "builder"))
        )
        if phase_index >= 0:
            self.planner_local_phase.setCurrentIndex(phase_index)
        self.planner_model = QLineEdit(str(self.state["planner"]["model"]))
        self.planner_effort = QComboBox()
        self.planner_effort.addItems(
            ["none", "low", "medium", "high", "xhigh", "max"]
        )
        self.planner_effort.setCurrentText(
            str(self.state["planner"]["reasoning_effort"])
        )
        key_row = QHBoxLayout()
        self.key_status = QLabel()
        key_button = QPushButton("Set API key…")
        key_button.clicked.connect(self.set_api_key)
        key_row.addWidget(self.key_status)
        key_row.addStretch()
        key_row.addWidget(key_button)
        planner_form.addRow("Backend", self.planner_kind)
        planner_form.addRow(
            "Local role (fallback -- no API credits needed)", self.planner_local_phase
        )
        planner_form.addRow("Model", self.planner_model)
        planner_form.addRow("Reasoning", self.planner_effort)
        planner_form.addRow(key_row)
        layout.addWidget(planner_box)
        self.planner_kind.currentIndexChanged.connect(self._update_planner_backend_visibility)
        self._update_planner_backend_visibility()

        self.editors: dict[str, RoleEditor] = {}
        for phase in ("scout", "builder", "renovator", "auditor"):
            editor = RoleEditor(phase.title(), models, self.state["roles"][phase])
            layout.addWidget(editor)
            self.editors[phase] = editor

        save_button = QPushButton("Save configuration")
        save_button.clicked.connect(self.save_configuration)
        layout.addWidget(save_button, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addStretch()

        scroll.setWidget(body)
        return scroll

    def _build_telemetry(self) -> QWidget:
        """Live telemetry -- lives at the bottom of the Reports tab (not the
        Deck tab) since that's where it's actually watched: while a pipeline
        phase is running, not while adjusting role config."""
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)

        # Live process-parameter dashboard -- from /v1/mtplx/flight, which
        # (unlike /metrics) updates *during* a request, not only after it
        # finishes. This is the actual answer to "is it stuck or just
        # thinking": which model, current phase (prefill vs decode), live
        # tok/s, token counts, elapsed time, and MTP depth acceptance, all
        # in the same card layout the old (post-hoc, /metrics-fed) grid
        # used -- just wired to a source that's actually live during a run
        # instead of frozen until the request finishes.
        live_box = QGroupBox("Current request")
        live_box_layout = QVBoxLayout(live_box)
        self.live_status = QLabel("Idle -- no request in flight")
        self.live_status.setObjectName("liveStatus")
        live_box_layout.addWidget(self.live_status)

        live_grid = QGridLayout()
        self.live_cards: dict[str, MetricCard] = {}
        for index, (key, label) in enumerate(
            (
                ("model", "Model"),
                ("phase", "Phase"),
                ("prefill_rate", "Prefill rate (last measured)"),
                ("decode_now", "Decode now"),
                ("decode_avg", "Decode avg"),
                ("prompt_tokens", "Prompt tokens"),
                ("gen_tokens", "Generated"),
                ("elapsed", "Elapsed"),
                ("depth", "MTP depth (acc/draft)"),
            )
        ):
            card = MetricCard(label)
            live_grid.addWidget(card, index // 4, index % 4)
            self.live_cards[key] = card
        live_box_layout.addLayout(live_grid)

        self.live_tail = QPlainTextEdit()
        self.live_tail.setReadOnly(True)
        self.live_tail.setMaximumHeight(60)
        self.live_tail.setPlaceholderText("A live tail of what the model is currently writing appears here.")
        live_box_layout.addWidget(self.live_tail)
        layout.addWidget(live_box)

        context_box = QGroupBox("Last request context")
        context_layout = QVBoxLayout(context_box)
        self.context_label = QLabel("No request telemetry yet")
        self.context_bar = QProgressBar()
        self.context_bar.setRange(0, 1000)
        context_layout.addWidget(self.context_label)
        context_layout.addWidget(self.context_bar)
        layout.addWidget(context_box)

        advanced = QGroupBox("MTP acceptance · cache · thermal")
        advanced_layout = QVBoxLayout(advanced)
        self.acceptance = QLabel("P1 —   P2 —   P3 —")
        self.cache_detail = QLabel("Cache —")
        self.thermal = QLabel("Thermal —")
        for widget in (self.acceptance, self.cache_detail, self.thermal):
            widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            advanced_layout.addWidget(widget)
        layout.addWidget(advanced)

        return body

    def _build_admin(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(20, 18, 20, 22)
        heading = QLabel("Admin · chat-client phase-injection prompts")
        heading.setObjectName("title")
        layout.addWidget(heading)
        banner = QLabel(
            "These prompts apply ONLY to a chat client (e.g. an IDE chat extension) that "
            "hits this router's phase-injected `local` alias -- they are spliced onto "
            "whatever system message that client already sent (see router/main.py's "
            "inject_phase_instructions). "
            "They have NO effect on the Reports tab pipeline: scripts/scout_report.py, "
            "planner_report.py, builder_agent.py, auditor_report.py, and renovator_agent.py "
            "each carry their own complete, hardcoded system prompt and explicitly skip this "
            "injection (X-Model-Deck-Skip-Injection header) -- editing here changes nothing "
            "about what those scripts send. Edits are saved to state.json's "
            "\"prompt_overrides\" and take effect on the next request, no restart needed."
        )
        banner.setWordWrap(True)
        banner.setObjectName("workflowNote")
        layout.addWidget(banner)
        diagram = QLabel(
            "35B SCOUT\n"
            "   ↓  .ai/scout-report.md\n"
            "GPT PLANNER\n"
            "   ↓  .ai/implementation-plan.md\n"
            "LOCAL BUILDER\n"
            "   ↓  code + .ai/builder-report.md\n"
            "FRESH AUDITOR\n"
            "   ↓  .ai/audit-report.md  →  REJECT? one RENOVATOR repair pass, then re-audit"
        )
        diagram.setObjectName("diagram")
        diagram_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        diagram_font.setPointSize(13)
        diagram.setFont(diagram_font)
        layout.addWidget(diagram)
        note = QLabel(
            "For a chat client itself (not the Reports pipeline): switch the phase here, "
            "the client remains on model `local`. Start a fresh chat task before Build and "
            "Audit so the file handoff -- not old conversation -- is authoritative. On a "
            "large repo, "
            "restart Scout into a fresh task too if its context climbs past ~50k tokens "
            "without a written report yet -- the session's cache stops sticking as it nears "
            "the context window, generation slows sharply, and it tends to stall repeating "
            "itself instead of finishing."
        )
        note.setWordWrap(True)
        note.setObjectName("workflowNote")
        layout.addWidget(note)
        self.prompt_cards: dict[str, PromptCard] = {}
        for phase, (document, default_prompt) in PROMPTS.items():
            override = (self.state.get("prompt_overrides") or {}).get(phase)
            is_override = isinstance(override, str) and bool(override.strip())
            card = PromptCard(
                phase, document, override if is_override else default_prompt,
                is_override, self._save_prompt_override, self._reset_prompt_override,
            )
            layout.addWidget(card)
            self.prompt_cards[phase] = card
        layout.addStretch()
        scroll.setWidget(body)
        return scroll

    def _save_prompt_override(self, phase: str, text: str) -> None:
        state = load_state()
        overrides = dict(state.get("prompt_overrides") or {})
        overrides[phase] = text
        state["prompt_overrides"] = overrides
        save_state(state)
        self.state = state
        self.statusBar().showMessage(f"Saved {phase} prompt override", 3000)

    def _reset_prompt_override(self, phase: str) -> str:
        state = load_state()
        overrides = dict(state.get("prompt_overrides") or {})
        overrides.pop(phase, None)
        state["prompt_overrides"] = overrides
        save_state(state)
        self.state = state
        self.statusBar().showMessage(f"Reset {phase} prompt to default", 3000)
        entry = PROMPTS.get(phase)
        return entry[1] if entry else ""

    def _build_reports(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(20, 18, 20, 22)
        heading = QLabel("Reports")
        heading.setObjectName("title")
        layout.addWidget(heading)
        note = QLabel(
            "One task, one path, run against all four phases -- no chat client, no VS "
            "Code involved. Scout/Planner/Auditor are one-shot, tool-free calls; "
            "Builder is a small purpose-built agent loop that actually edits "
            "files and runs commands in the target path (not sandboxed beyond "
            "that -- review .ai/implementation-plan.md once Planner finishes, "
            "before Builder runs, especially in Full Suite mode where nothing "
            "pauses for you to look). \"Run Full Suite\" runs all four back to "
            "back automatically, swapping the resident model between phases as "
            "needed. \"Run Step-by-Step\" runs one phase, then pauses and lets "
            "you review before you click Continue for the next one.\n\n"
            "On a long silent stretch with the elapsed counter still climbing: "
            "that's normal, not stuck. A large context means a long prefill (the "
            "model reading everything before it writes a single token), and "
            "there's no progress signal for that phase in this API -- not from "
            "mtplx, not from any chat client, not from anything. Nothing streams "
            "until prefill finishes; the counter moving below, and the live "
            "telemetry at the bottom of this tab, are the only confirmation "
            "it's alive."
        )
        note.setWordWrap(True)
        note.setObjectName("workflowNote")
        layout.addWidget(note)

        self.pipeline_panel = PipelinePanel()
        self.pipeline_panel.full_suite_button.clicked.connect(lambda: self._start_pipeline("full"))
        self.pipeline_panel.step_wise_button.clicked.connect(lambda: self._start_pipeline("step"))
        self.pipeline_panel.continue_button.clicked.connect(self._on_pipeline_continue_clicked)
        self.pipeline_panel.stop_button.clicked.connect(self._on_pipeline_stop_clicked)
        layout.addWidget(self.pipeline_panel)
        layout.addWidget(self._build_telemetry())
        layout.addStretch()
        scroll.setWidget(body)
        return scroll

    def _activate_for_report(self, phase: str) -> None:
        """Point Deck's telemetry panel at whichever backend this phase is
        about to use. The pipeline calls model aliases (scout/planner/
        builder/auditor) directly and never goes through activate_local/
        activate_planner otherwise, so without this the telemetry panel
        silently keeps polling whatever was last clicked in the Deck tab --
        not what's actually running. This only moves the state pointer, not
        process lifecycle: no launch/stop of any model here."""
        state = load_state()
        if phase == "planner":
            activate_planner(state)
        else:
            activate_local(state, phase)
        save_state(state)
        self.state = state
        self.refresh_metrics()

    def _phase_local_role(self, phase: str) -> str | None:
        """Which local role needs to be resident for this phase, or None
        when it's the cloud planner (nothing local to launch)."""
        if phase == "planner":
            if self.state["planner"].get("kind") == "local":
                return str(self.state["planner"].get("local_phase") or "builder")
            return None
        return phase

    def _ensure_role_resident(self, role_name: str | None) -> None:
        """Swaps the resident local model only if the phase about to run
        needs a different one than what's already loaded -- consecutive
        phases that share a role (Planner-via-Builder followed by the real
        Builder phase) don't pay a pointless stop/relaunch cycle. Runs on
        the background worker thread: launch() blocks waiting for model
        warmup, so this must never be called from the UI thread."""
        if role_name is None:
            return
        if self._pipeline_resident_role == role_name:
            return
        state = load_state()
        ports = [int(r["port"]) for r in state["roles"].values()]
        self.manager.stop_local_models(ports)
        self.manager.launch(role_name, state["roles"][role_name])
        self._pipeline_resident_role = role_name

    def _start_pipeline(self, mode: str) -> None:
        path = self.pipeline_panel.path_field.text().strip()
        task = self.pipeline_panel.task_field.toPlainText().strip()
        start_phase = str(self.pipeline_panel.start_phase.currentData())
        if not path:
            QMessageBox.information(self, "Path required", "Enter a file or directory to scan.")
            return
        if start_phase == "scout" and not task:
            QMessageBox.information(self, "Task required", "Describe what this change/investigation is for.")
            return

        required = REQUIRED_ARTIFACT_FOR_START.get(start_phase, "")
        if required:
            required_path = resolve_ai_path(Path(path), required)
            if not required_path.exists():
                QMessageBox.warning(
                    self, "Missing prerequisite",
                    f"Starting at {start_phase.title()} needs {required_path} to already exist "
                    f"(normally written by an earlier phase). Run from Scout instead, or point "
                    f"Path at a location where that file is already present.",
                )
                return

        self.pipeline_mode = mode
        self.pipeline_task = task
        self.pipeline_path = path
        start_index = PIPELINE_ORDER.index(start_phase)
        self.pipeline_queue = list(PIPELINE_ORDER[start_index:])
        self.pipeline_renovator_retries = 0
        self._pipeline_resident_role = None
        self.pipeline_panel.reset_status()
        for skipped_phase in PIPELINE_ORDER[:start_index]:
            self.pipeline_panel.phase_status[skipped_phase].setText(f"{skipped_phase.title()}: skipped (resumed)")
        self.pipeline_panel.output.clear()
        self.pipeline_panel.full_suite_button.setEnabled(False)
        self.pipeline_panel.step_wise_button.setEnabled(False)
        self.pipeline_panel.continue_button.setEnabled(False)
        self._run_next_pipeline_phase()

    def _run_next_pipeline_phase(self) -> None:
        if not self.pipeline_queue:
            self.pipeline_panel.full_suite_button.setEnabled(True)
            self.pipeline_panel.step_wise_button.setEnabled(True)
            self.statusBar().showMessage("Pipeline complete", 5000)
            return

        phase = self.pipeline_queue.pop(0)
        self.pipeline_current_phase = phase
        self._activate_for_report(phase)

        panel = self.pipeline_panel
        panel.phase_status[phase].setText(f"{phase.title()}: preparing model…")
        panel.stop_button.setEnabled(True)
        panel.output.appendPlainText(f"\n=== {phase.upper()} ===\n")
        self.report_start_times[phase] = time.monotonic()
        threading.Thread(
            target=self._pipeline_worker,
            args=(phase, self.pipeline_task, self.pipeline_path),
            daemon=True,
        ).start()

    def _on_pipeline_continue_clicked(self) -> None:
        self.pipeline_panel.continue_button.setEnabled(False)
        self._run_next_pipeline_phase()

    def _on_pipeline_stop_clicked(self) -> None:
        phase = getattr(self, "pipeline_current_phase", None)
        process = self.report_processes.get(phase) if phase else None
        if process is None or process.poll() is not None:
            return
        self.pipeline_queue.clear()  # a manual stop should not auto-continue
        self.pipeline_panel.phase_status[phase].setText(f"{phase.title()}: stopping…")
        self.pipeline_panel.stop_button.setEnabled(False)
        process.terminate()
        # If the worker thread is blocked in _handle_ask_question waiting on
        # an answer (not on reading stdout), terminate() alone would never
        # unblock it -- release it here with an empty answer so it can
        # notice the dead process and finish instead of hanging forever.
        event, box = self._pending_answers.pop(phase, (None, None))
        if box is not None:
            box.append("")
        if event is not None:
            event.set()
        # No further bookkeeping needed here: the worker thread's read loop
        # sees stdout close, process.wait() returns a nonzero code, and
        # _pipeline_worker's existing failure path (report_bridge.finished
        # with success=False) already handles that -- same UI update as any
        # other failed run.

    def _tick_report_status(self) -> None:
        """Ticks every second regardless of whether any text has streamed
        yet. Prefill (the model processing the input context before it
        produces a single output token) has no progress signal in the
        streaming API -- not from mtplx, not from any chat client, not from
        anything -- so for a large-context single-shot call there can be a long,
        genuinely silent stretch before the output pane shows anything.
        This at least confirms the run hasn't died, rather than showing
        nothing at all."""
        now = time.monotonic()
        panel = getattr(self, "pipeline_panel", None)
        if panel is None:
            return
        for phase, start in self.report_start_times.items():
            label = panel.phase_status.get(phase)
            if label is not None:
                label.setText(f"{phase.title()}: running… {now - start:.0f}s")

    def _pipeline_worker(self, phase: str, task: str, path: str) -> None:
        try:
            self._ensure_role_resident(self._phase_local_role(phase))
        except Exception as exc:
            self.report_bridge.finished.emit(phase, False, f"Could not prepare the model for {phase}: {exc}")
            return

        config = REPORT_SCRIPTS[phase]
        python = PROJECT_DIR / ".venv" / "bin" / "python"
        script_path = PROJECT_DIR / "scripts" / config["script"]
        args = [str(a) for a in config["build_args"](task, path)]
        try:
            process = subprocess.Popen(
                [str(python), str(script_path), *args],
                cwd=PROJECT_DIR,
                stdin=subprocess.PIPE,  # builder_agent.py's ask_question blocks reading a line here
                stdout=subprocess.PIPE,
                # Combined into stdout so we only have one pipe to drain --
                # the script's progress lines and the model's streamed text
                # arrive interleaved in the order they were printed, which is
                # exactly the transcript we want to show live.
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            self.report_bridge.finished.emit(phase, False, str(exc))
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
            self.report_bridge.chunk.emit(phase, piece)
            line_buffer += piece
            while "\n" in line_buffer:
                line, line_buffer = line_buffer.split("\n", 1)
                if line.startswith("[ASK_QUESTION] "):
                    self._handle_ask_question(phase, process, line[len("[ASK_QUESTION] "):])
        returncode = process.wait()
        output_text = "".join(collected)

        if returncode != 0:
            prefix = "Stopped by user.\n\n" if returncode < 0 else ""
            self.report_bridge.finished.emit(
                phase, False, prefix + (output_text.strip() or f"exit code {returncode}")
            )
            return
        out_path = self._parse_wrote_path(output_text)
        if out_path is None:
            self.report_bridge.finished.emit(
                phase, False, f"Script exited 0 but its output path wasn't found in:\n{output_text}"
            )
            return
        try:
            content = out_path.read_text()
        except OSError as exc:
            self.report_bridge.finished.emit(
                phase, False, f"Script succeeded but {out_path} could not be read: {exc}"
            )
            return
        self.report_bridge.finished.emit(phase, True, content)

    def _handle_ask_question(self, phase: str, process: subprocess.Popen, payload: str) -> None:
        """Runs on the pipeline worker thread. Hands the question to the UI
        thread via a signal (PySide6 queues cross-thread signals onto the
        receiving QObject's own thread automatically) and blocks this
        thread -- not the UI -- until _on_ask_question sets the event after
        the person answers the dialog."""
        answer_event = threading.Event()
        answer_box: list[str] = []
        self._pending_answers[phase] = (answer_event, answer_box)
        self.report_bridge.ask_question.emit(phase, payload)
        answer_event.wait()
        answer = answer_box[0] if answer_box else ""
        assert process.stdin is not None
        try:
            process.stdin.write(answer + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # process already gone (e.g. stopped while waiting) -- nothing to feed the answer to

    def _on_ask_question(self, phase: str, payload: str) -> None:
        """Runs on the UI thread. Shows a blocking dialog, then unblocks
        _handle_ask_question's wait on the pipeline worker thread."""
        try:
            data = json.loads(payload)
        except ValueError:
            data = {"question": payload, "options": None}
        question = str(data.get("question") or "(no question text)")
        options = data.get("options")
        panel = self.pipeline_panel
        panel.output.appendPlainText(f"\n--- {phase.upper()} IS ASKING ---\n{question}\n")

        answer = ""
        title = f"{phase.title()} needs a decision"
        if isinstance(options, list) and options:
            choice, ok = QInputDialog.getItem(
                self, title, question, [str(option) for option in options], 0, False,
            )
            answer = choice if ok else str(options[0])
            if not ok:
                panel.output.appendPlainText(
                    f"(dialog dismissed -- defaulting to {answer!r} so the run can continue)\n"
                )
        else:
            text, ok = QInputDialog.getText(self, title, question)
            answer = text if ok else ""
        panel.output.appendPlainText(f"--- answered: {answer} ---\n")

        event, box = self._pending_answers.pop(phase, (None, None))
        if box is not None:
            box.append(answer)
        if event is not None:
            event.set()

    def _report_chunk(self, phase: str, piece: str) -> None:
        panel = self.pipeline_panel
        cursor = panel.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        panel.output.setTextCursor(cursor)
        panel.output.insertPlainText(piece)
        panel.output.ensureCursorVisible()

    @staticmethod
    def _parse_wrote_path(stdout: str) -> Path | None:
        """Each script prints "Wrote <path>" on success. Parsing that instead
        of reconstructing the path ourselves keeps the GUI in sync with
        wherever the script actually anchored the artifact (the target
        project, not this tool's own directory -- see resolve_ai_path)."""
        for line in stdout.splitlines():
            if line.startswith("Wrote "):
                raw = Path(line[len("Wrote "):].strip())
                return raw if raw.is_absolute() else PROJECT_DIR / raw
        return None

    def _report_finished(self, phase: str, success: bool, text: str) -> None:
        panel = self.pipeline_panel
        self.report_processes.pop(phase, None)
        start = self.report_start_times.pop(phase, None)
        elapsed = f" ({time.monotonic() - start:.0f}s)" if start is not None else ""
        panel.phase_status[phase].setText(f"{phase.title()}: {'done' if success else 'failed'}{elapsed}")
        panel.output.appendPlainText(
            f"\n\n--- {phase.upper()} {'FINISHED' if success else 'FAILED'} ---\n"
            + (text if success else f"Failed:\n\n{text}")
            + "\n"
        )
        panel.stop_button.setEnabled(False)
        self.statusBar().showMessage(
            f"{phase.title()} {'ready' if success else 'failed'}", 4000
        )

        if not success:
            self.pipeline_queue.clear()
            panel.full_suite_button.setEnabled(True)
            panel.step_wise_button.setEnabled(True)
            panel.continue_button.setEnabled(False)
            QMessageBox.warning(self, f"{phase.title()} failed", text[:2000])
            return

        if phase == "auditor":
            verdict = parse_verdict(text)
            if verdict != "PASS":
                if self.pipeline_renovator_retries < MAX_RENOVATOR_RETRIES:
                    self.pipeline_renovator_retries += 1
                    self.pipeline_queue = ["renovator", "auditor"]
                    note = (
                        f"Auditor verdict: {verdict}. Queuing one repair pass "
                        f"(Renovator, retry {self.pipeline_renovator_retries}/"
                        f"{MAX_RENOVATOR_RETRIES}) against its Fix List, then "
                        "re-auditing."
                    )
                    panel.output.appendPlainText(f"\n{note}\n")
                    self.statusBar().showMessage(note, 6000)
                    # Falls through to the normal full/step advance logic below
                    # -- pipeline_queue now has entries either way, so it
                    # behaves exactly like any other multi-phase continuation.
                else:
                    self.pipeline_queue.clear()
                    panel.full_suite_button.setEnabled(True)
                    panel.step_wise_button.setEnabled(True)
                    panel.continue_button.setEnabled(False)
                    QMessageBox.warning(
                        self, "Auditor rejected again",
                        f"Auditor's verdict is still {verdict} after a repair pass. "
                        "Stopping here for manual review rather than looping "
                        "further -- see the output above and .ai/audit-report.md.",
                    )
                    return

        if self.pipeline_mode == "full":
            self._run_next_pipeline_phase()
            return

        if self.pipeline_queue:
            panel.continue_button.setEnabled(True)
            QMessageBox.information(
                self, f"{phase.title()} complete",
                f"{phase.title()} finished successfully{elapsed}. Review its output, "
                f"then click Continue to run {self.pipeline_queue[0].title()} next.",
            )
        else:
            panel.full_suite_button.setEnabled(True)
            panel.step_wise_button.setEnabled(True)
            self.statusBar().showMessage("Pipeline complete", 5000)

    def save_configuration(self) -> None:
        for phase, editor in self.editors.items():
            editor.apply(self.state["roles"][phase])
        self.state["planner"]["kind"] = str(self.planner_kind.currentData())
        self.state["planner"]["local_phase"] = str(self.planner_local_phase.currentData())
        self.state["planner"]["model"] = self.planner_model.text().strip()
        self.state["planner"]["reasoning_effort"] = self.planner_effort.currentText()
        save_state(self.state)
        self.statusBar().showMessage("Configuration saved", 3000)

    def _update_planner_backend_visibility(self) -> None:
        is_local = self.planner_kind.currentData() == "local"
        self.planner_local_phase.setEnabled(is_local)
        self.planner_model.setEnabled(not is_local)
        self.planner_effort.setEnabled(not is_local)

    def set_api_key(self) -> None:
        dialog = SecretDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            set_openai_api_key(dialog.key.text())
        except Exception as exc:
            QMessageBox.critical(self, "API key", str(exc))
        self._update_key_status()

    def activate(self, phase: str) -> None:
        if self.busy:
            return
        self.save_configuration()
        planner_is_local = phase == "planner" and self.state["planner"].get("kind") == "local"
        if phase == "planner" and not planner_is_local and not get_openai_api_key():
            QMessageBox.information(
                self,
                "Planner API key",
                "Set your OpenAI API key before activating the GPT Planner, "
                "or switch its Backend to \"Local model\" to skip the API entirely.",
            )
            self.set_api_key()
            if not get_openai_api_key():
                return
        self.busy = True
        self._set_buttons_enabled(False)
        self.statusBar().showMessage(f"Switching to {phase}…")

        snapshot = json.loads(json.dumps(self.state))

        def work() -> None:
            try:
                ports = [int(role["port"]) for role in snapshot["roles"].values()]
                self.manager.stop_local_models(ports)
                self.manager.ensure_router()
                if phase == "planner" and not planner_is_local:
                    activate_planner(snapshot)
                    detail = str(snapshot["planner"]["model"])
                else:
                    local_phase = snapshot["planner"]["local_phase"] if planner_is_local else phase
                    role = snapshot["roles"][local_phase]
                    self.manager.launch(local_phase, role)
                    if planner_is_local:
                        activate_planner(snapshot)
                    else:
                        activate_local(snapshot, phase)
                    detail = str(role["model"])
                save_state(snapshot)
                self.bridge.succeeded.emit(phase, detail)
            except Exception as exc:
                self.bridge.failed.emit(phase, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _phase_ready(self, phase: str, detail: str) -> None:
        self.state = load_state()
        self.busy = False
        self._set_buttons_enabled(True)
        self.statusBar().showMessage(f"{phase.title()} active · {detail}")
        self.refresh_metrics()

    def _phase_failed(self, phase: str, message: str) -> None:
        self.busy = False
        self._set_buttons_enabled(True)
        self.statusBar().showMessage(f"Could not activate {phase}")
        QMessageBox.critical(self, "Phase switch failed", message)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self.launch_button.setEnabled(enabled)
        for button in self.phase_buttons.values():
            button.setEnabled(enabled)

    def _router_base(self) -> str:
        router = self.state["router"]
        return f"http://{router['host']}:{router['port']}"

    def _check_loop_alert(self) -> None:
        if self.loop_dialog_open:
            return
        try:
            info = fetch_json(f"{self._router_base()}/loop-alert", timeout=0.3)
        except Exception:
            return
        if not info.get("should_alert"):
            return
        self.loop_dialog_open = True
        try:
            self._show_loop_alert(info)
        finally:
            self.loop_dialog_open = False

    def _show_loop_alert(self, info: dict[str, Any]) -> None:
        base = self._router_base()
        phase = str(info.get("phase") or "model").title()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Possible stuck loop")
        box.setText(
            f"{phase} looks like it's repeating itself.\n\n"
            f"Repeated block ({info.get('window')} chars × {info.get('repeats')}×):\n"
            f"“{info.get('snippet', '')}”"
        )
        stop_button = box.addButton(
            "Stop generation", QMessageBox.ButtonRole.DestructiveRole
        )
        box.addButton("Keep watching", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        guard_id, seq = info.get("id"), info.get("seq")
        if box.clickedButton() is stop_button:
            try:
                post_json(f"{base}/loop-alert/stop", {"id": guard_id})
            except Exception:
                pass
        try:
            post_json(f"{base}/loop-alert/ack", {"id": guard_id, "seq": seq})
        except Exception:
            pass

    def refresh_metrics(self) -> None:
        self.state = load_state()
        self._check_loop_alert()
        active = self.state["active"]
        phase = str(active["phase"])
        self.active_badge.setText(f"ACTIVE · {phase.upper()}")
        self._update_key_status()
        for name, button in self.phase_buttons.items():
            button.setProperty("active", name == phase)
            button.style().unpolish(button)
            button.style().polish(button)
        if active["kind"] != "local":
            self._clear_metrics(f"Cloud planner · {active['model_id']}")
            return
        base = str(active["base_url"]).removesuffix("/v1")
        try:
            health = fetch_json(f"{base}/health", timeout=0.3)
            envelope = fetch_json(f"{base}/metrics", timeout=0.3)
        except Exception:
            self._clear_metrics("Waiting for local model…")
            return
        latest = envelope.get("latest") or {}
        if latest.get("prefill_tok_s"):
            # Carried forward so a later request's live prefill phase (which
            # itself reports no rate -- see _refresh_flight) has something
            # to estimate an ETA from.
            self._last_prefill_rate[phase] = float(latest["prefill_tok_s"])
        self._refresh_flight(base, phase, health)

        used = int(latest.get("context_len") or 0)
        maximum = int(health.get("context_window") or 0)
        percent = (used / maximum * 100.0) if maximum else 0.0
        self.context_label.setText(
            f"{used:,} / {maximum:,} tokens  ·  {percent:.1f}% used"
        )
        self.context_bar.setValue(min(1000, round(percent * 10)))

        probs = latest.get("mean_accept_probability_by_depth") or []
        accepted = latest.get("accepted_by_depth") or []
        parts = []
        for index in range(max(len(probs), len(accepted), 3)):
            probability = float(probs[index]) * 100 if index < len(probs) else 0.0
            count = accepted[index] if index < len(accepted) else 0
            parts.append(f"P{index + 1} {probability:.0f}% · {count} accepted")
        self.acceptance.setText("    ".join(parts))
        bank = health.get("session_bank") or {}
        ssd = health.get("ssd_session_cache") or {}
        self.cache_detail.setText(
            f"RAM bank {self._gib(bank.get('total_nbytes'))} / "
            f"{self._gib(bank.get('effective_max_bytes'))}  ·  "
            f"{bank.get('entries', 0)} entries  ·  SSD "
            f"{'on' if ssd.get('enabled') else 'off'}"
        )
        self.thermal.setText(
            f"Fans {health.get('fan_mode', '—')}  ·  boost "
            f"{'active' if health.get('fan_boost_active') else 'idle'}  ·  "
            f"requests {health.get('active_requests', 0)}  ·  KV "
            f"{health.get('paged_kv_quantization', '—')}"
        )

    def _clear_metrics(self, detail: str) -> None:
        self.context_label.setText(detail)
        self.context_bar.setValue(0)
        self.acceptance.setText("P1 —   P2 —   P3 —")
        self.cache_detail.setText("Cache —")
        self.thermal.setText("Thermal —")
        self.live_status.setText("Idle -- no request in flight")
        self._clear_live_cards()
        self.live_tail.setPlainText("")

    @staticmethod
    def _model_label(health: dict[str, Any]) -> str:
        """A short, readable model name for the dashboard's Model card --
        health's own "model" field is just the router alias (e.g. "scout"),
        not which actual weights are loaded, so pull the repo name out of
        model_path instead."""
        alias = str(health.get("model") or "—")
        path = str(health.get("model_path") or "")
        repo = path.rsplit("/", 1)[-1] if path else ""
        repo = repo.split("--", 1)[-1] if "--" in repo else repo
        repo = repo.replace("-MTPLX-Optimized-Speed-FP16", "")
        return f"{alias} ({repo})" if repo else alias

    def _clear_live_cards(self) -> None:
        for card in self.live_cards.values():
            card.value.setText("—")

    @staticmethod
    def _live_prefill_rate(base: str) -> tuple[float, int] | None:
        """(tokens/sec, tokens_done) for the current prefill chunk, from
        /v1/mtplx/metrics/stream's in_flight[].prefill_state -- see
        _refresh_flight's docstring. Returns None if there's no in-flight
        request, or if it hasn't finished its first chunk yet (tokens_done
        still 0), in which case the caller falls back to the historical
        estimate."""
        try:
            snapshot = fetch_sse_snapshot(f"{base}/v1/mtplx/metrics/stream?snapshot_interval_ms=50", timeout=0.3)
        except Exception:
            return None
        in_flight = snapshot.get("in_flight") or []
        if not in_flight:
            return None
        prefill_state = in_flight[0].get("prefill_state") or {}
        tokens_done = int(prefill_state.get("tokens_done") or 0)
        elapsed = prefill_state.get("elapsed_s")
        if tokens_done <= 0 or not elapsed:
            return None
        return tokens_done / float(elapsed), tokens_done

    def _refresh_flight(self, base: str, role_phase: str, health: dict[str, Any]) -> None:
        """Live process-parameter dashboard from mtplx's own flight log --
        this is what actually updates during prefill/decode, unlike
        /metrics (whose "latest" stays null until a request finishes). Best
        effort: an older mtplx build without this endpoint just shows Idle,
        same as no request being in flight. Model card always reflects
        health (genuinely live server state) even when idle -- everything
        else needs an in-flight request to mean anything.

        During an active prefill, /v1/mtplx/flight's own numbers are no help
        for a rate or ETA: "prefill" is null and tps_now/tps_avg are both 0
        for the whole phase (confirmed empirically, not documented). The
        real live per-chunk progress (tokens_done/tokens_total/elapsed_s)
        only exists on /v1/mtplx/metrics/stream's in_flight[].prefill_state
        -- confirmed against mtplx's own app, which reads this same field
        for its live "prefill tps / ETA" gauge. That's an SSE endpoint, but
        each event is a full snapshot (not a delta), so one connect-read-
        close per refresh tick works fine -- no persistent connection
        needed. Only queried while phase == "prefill", to avoid the extra
        request on every tick. If tokens_done is still 0 (prefill hasn't
        finished its first chunk yet, e.g. a short prompt that completes
        within one 2048-token chunk before this ever gets called), falls
        back to the same last-completed-request estimate as before."""
        known_rate = self._last_prefill_rate.get(role_phase)
        prefill_rate_text = f"{known_rate:.0f} tok/s" if known_rate else "—"

        self.live_cards["model"].value.setText(self._model_label(health))
        self.live_cards["prefill_rate"].value.setText(prefill_rate_text)
        try:
            flight = fetch_json(f"{base}/v1/mtplx/flight", timeout=0.3)
        except Exception:
            self.live_status.setText("Idle -- no request in flight")
            self._clear_live_cards()
            self.live_cards["model"].value.setText(self._model_label(health))
            self.live_cards["prefill_rate"].value.setText(prefill_rate_text)
            self.live_tail.setPlainText("")
            return
        active = flight.get("active") or []
        if not active:
            self.live_status.setText("Idle -- no request in flight")
            self._clear_live_cards()
            self.live_cards["model"].value.setText(self._model_label(health))
            self.live_cards["prefill_rate"].value.setText(prefill_rate_text)
            self.live_tail.setPlainText("")
            return
        request = active[0]
        request_phase = str(request.get("phase") or "?")
        tps_now = request.get("tps_now")
        tps_avg = request.get("tps_avg")
        elapsed = request.get("elapsed_s")
        prompt_tokens = int(request.get("prompt_tokens") or 0)
        gen_tokens = int(request.get("gen_tokens") or 0)

        eta_text = ""
        if request_phase == "prefill":
            live_rate = self._live_prefill_rate(base)
            if live_rate:
                remaining_tokens = max(prompt_tokens - live_rate[1], 0)
                eta_text = f"  ·  live {live_rate[0]:.0f} tok/s  ·  {remaining_tokens / live_rate[0]:.0f}s remaining ({live_rate[1]:,}/{prompt_tokens:,} tok)"
                self.live_cards["prefill_rate"].value.setText(f"{live_rate[0]:.0f} tok/s (live)")
            elif known_rate:
                eta_text = f"  ·  est. {prompt_tokens / known_rate:.0f}s remaining (from last request's {known_rate:.0f} tok/s)"
            else:
                eta_text = "  ·  no prior request this session to estimate a prefill ETA from"
        self.live_status.setText(f"● live{eta_text}")

        self.live_cards["phase"].value.setText(request_phase)
        self.live_cards["decode_now"].value.setText(self._rate(tps_now))
        self.live_cards["decode_avg"].value.setText(self._rate(tps_avg))
        self.live_cards["prompt_tokens"].value.setText(f"{prompt_tokens:,}")
        self.live_cards["gen_tokens"].value.setText(f"{gen_tokens:,}")
        self.live_cards["elapsed"].value.setText(self._seconds(elapsed))

        accepted = request.get("accepted_by_depth") or []
        drafted = request.get("drafted_by_depth") or []
        if accepted or drafted:
            parts = []
            for index in range(max(len(accepted), len(drafted))):
                a = accepted[index] if index < len(accepted) else 0
                d = drafted[index] if index < len(drafted) else 0
                percent = (a / d * 100.0) if d else 0.0
                parts.append(f"D{index + 1} {a}/{d} ({percent:.0f}%)")
            self.live_cards["depth"].value.setText("  ".join(parts))
        else:
            self.live_cards["depth"].value.setText("—")

        tail = request.get("tail")
        if isinstance(tail, str) and tail:
            self.live_tail.setPlainText(tail)

    def _update_key_status(self) -> None:
        self.key_status.setText(
            "● Key stored in macOS Keychain"
            if get_openai_api_key()
            else "○ No API key stored"
        )

    @staticmethod
    def _rate(value: Any) -> str:
        return f"{float(value):.1f} tok/s" if value is not None else "—"

    @staticmethod
    def _seconds(value: Any) -> str:
        return f"{float(value):.2f} s" if value is not None else "—"

    @staticmethod
    def _gib(value: Any) -> str:
        return f"{float(value) / 1073741824:.1f} GiB" if value is not None else "—"


STYLE = """
QWidget { background: #11151c; color: #e7edf6; font-size: 13px; }
QScrollArea { border: none; }
QLabel#title { font-size: 27px; font-weight: 700; color: #f6f8fb; }
QLabel#subtitle { color: #8d9aac; font-size: 14px; }
QLabel#endpoint { background: #171e28; color: #9eb4d0; padding: 10px 12px;
                  border: 1px solid #273244; border-radius: 8px; }
QLabel#activeBadge { background: #123b33; color: #6ee7b7; padding: 8px 12px;
                     border-radius: 12px; font-weight: 700; }
QLabel#liveStatus { color: #6ee7b7; font-weight: 650; padding: 2px 0; }
QGroupBox { border: 1px solid #273244; border-radius: 10px; margin-top: 12px;
            padding: 14px 10px 10px 10px; font-weight: 650; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #b9c7da; }
QPushButton { background: #202a38; border: 1px solid #334258; border-radius: 7px;
              padding: 8px 12px; }
QPushButton:hover { background: #29374a; }
QPushButton#launchButton { background: #2563eb; border-color: #3b82f6;
                           font-size: 15px; font-weight: 700; padding: 11px; }
QPushButton#phaseButton[active="true"] { background: #0f766e; border-color: #14b8a6; }
QPushButton#stopButton { background: #3f1d1d; border-color: #7f1d1d; color: #fca5a5; }
QPushButton#stopButton:hover:!disabled { background: #5b1f1f; }
QPushButton#stopButton:disabled { color: #6b7280; }
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox { background: #171e28; color: #e7edf6;
                                border: 1px solid #334258; border-radius: 6px; padding: 6px; }
QComboBox QAbstractItemView { background: #171e28; color: #e7edf6;
                               selection-background-color: #29374a; }
QFrame#metricCard { background: #171e28; border: 1px solid #273244; border-radius: 8px; }
QLabel#metricHeading { color: #77869a; font-size: 10px; font-weight: 700; }
QLabel#metricValue { color: #f8fafc; font-size: 19px; font-weight: 700; }
QLabel#diagram { background: #0b0f14; color: #7dd3fc; padding: 18px;
                 border: 1px solid #273244; border-radius: 10px; }
QLabel#workflowNote { color: #aebbd0; padding: 8px 2px; }
QLabel#documentLabel { color: #6ee7b7; }
QPlainTextEdit { background: #0b0f14; border: 1px solid #273244; border-radius: 7px;
                 color: #d5deeb; padding: 8px; }
QProgressBar { border: 1px solid #334258; border-radius: 5px; text-align: center;
               background: #171e28; }
QProgressBar::chunk { background: #2563eb; border-radius: 4px; }
QStatusBar { color: #9eb4d0; }
"""


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Model Deck")
    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
