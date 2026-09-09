from __future__ import annotations

import json
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
    QCheckBox,
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
from .pipeline import PIPELINE_ORDER, STATUS_PHASES, Pipeline
from .prompts import PROMPTS
from .secrets import get_openai_api_key, set_openai_api_key
from .state import (
    activate_local,
    activate_planner,
    load_state,
    recommended_serving_settings,
    sampling_preset,
    save_state,
)

sys.path.insert(0, str(PROJECT_DIR / "scripts"))
from report_common import (  # noqa: E402
    DEFAULT_CHAR_BUDGET,
    DEFAULT_EXTENSIONS,
    build_context,
    collect_files,
    describe_skipped,
    parse_builder_accuracy,
    parse_verdict,
    stream_chat,
)


def _thousands(value: int) -> str:
    """Token counts get big enough that raw digits stop being readable at a
    glance, which is the only reason these labels exist."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


CHAT_SYSTEM_PROMPT = (
    "You are helping the user turn a rough idea into a precise, buildable "
    "prompt for an automated coding pipeline (scout, planner, builder, "
    "auditor). That pipeline gets no chance to ask follow-up questions once "
    "it starts, so your job is to surface the ambiguities now: ask about "
    "scope, target files, constraints, and what \"done\" looks like. When the "
    "user asks for the prompt, output it as a single self-contained block of "
    "prose with no preamble, stating the goal, the constraints, and the "
    "acceptance criteria explicitly."
)

NORMAL_CHAT_SYSTEM_PROMPT = (
    "You are a helpful coding assistant with access to the user's project files. "
    "Answer questions about how code works, explain architecture, suggest changes, "
    "and perform simple actions when asked. Be concise and practical. When the user "
    "asks you to do something (e.g., 'launch this application'), explain what you "
    "would do or provide the exact command -- you cannot execute commands yourself "
    "unless explicitly given a tool interface."
)


class ChatBridge(QObject):
    chunk = Signal(str)
    finished = Signal(bool, str)  # success, full text or error


class Bridge(QObject):
    succeeded = Signal(str, str)
    failed = Signal(str, str)


class ReportBridge(QObject):
    finished = Signal(str, bool, str)  # phase, success, report content or error text
    chunk = Signal(str, str)  # phase, text piece as it streams in
    ask_question = Signal(str, str)  # phase, JSON {"question": ..., "options": [...] | None}


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

        status_columns = 3
        status_grid = QGridLayout()
        self.phase_status: dict[str, QLabel] = {}
        # Each phase label reads "Phase: <state> - <stats>". The state half is
        # rewritten constantly (a per-second running timer); the stats half
        # accumulates as the run produces it (turns as they're taken, tokens
        # and accuracy only at the end). Keeping the two halves separate is
        # what stops the timer tick from wiping out stats that arrived
        # earlier, and stops a late stat from erasing the elapsed time.
        self.phase_state: dict[str, str] = {}
        self.phase_stats: dict[str, dict[str, str]] = {}
        for index, phase in enumerate(STATUS_PHASES):
            label = QLabel(f"{phase.title()}: pending")
            label.setObjectName("reportStatus")
            status_grid.addWidget(label, index // status_columns, index % status_columns)
            self.phase_status[phase] = label
            self.phase_state[phase] = "pending"
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

    def set_phase_state(self, phase: str, state: str) -> None:
        self.phase_state[phase] = state
        self._render_phase(phase)

    def set_phase_stat(self, phase: str, key: str, text: str) -> None:
        """Attach one stat (turns / tokens / accuracy) to a phase's label."""
        self.phase_stats.setdefault(phase, {})[key] = text
        self._render_phase(phase)

    def _render_phase(self, phase: str) -> None:
        label = self.phase_status.get(phase)
        if label is None:
            return
        stats = self.phase_stats.get(phase) or {}
        parts = [self.phase_state.get(phase, "pending")]
        parts += [stats[key] for key in ("turns", "tokens", "accuracy") if stats.get(key)]
        label.setText(f"{phase.title()}: " + "  ·  ".join(parts))

    def reset_status(self) -> None:
        self.phase_stats.clear()
        for phase in self.phase_status:
            self.set_phase_state(phase, "pending")


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
        # MoE models (Ornith included, per the operator's own testing) have
        # been reported unusually sensitive to paged-KV cache quantization --
        # this used to be a "q8" hardcode buried in _role()'s default, only
        # changeable by hand-editing state.json. mtplx's own valid set is
        # exactly {"off", "q8", "q4"} (see `mtplx serve --help`).
        self.kv_quantization = QComboBox()
        self.kv_quantization.addItems(["off", "q8", "q4"])
        kv_index = self.kv_quantization.findText(str(role.get("kv_quantization", "q8")))
        self.kv_quantization.setCurrentIndex(kv_index if kv_index >= 0 else 1)
        # Full valid set per `mtplx serve --help` -- turbo/sustained are the
        # only two this app's own Apply-preset recommendations ever choose
        # (see recommended_serving_settings), the rest stay available for
        # manual use (e.g. performance-cold+max for what mtplx's own app
        # calls "Burst"; this GUI doesn't expose that combination directly).
        self.profile = QComboBox()
        self.profile.addItems(["turbo", "sustained", "stable", "performance-cold", "exact", "max-diagnostic"])
        profile_index = self.profile.findText(str(role.get("profile", "turbo")))
        self.profile.setCurrentIndex(profile_index if profile_index >= 0 else 0)
        self.context = QSpinBox()
        self.context.setRange(4096, 262144)
        self.context.setSingleStep(1024)
        self.context.setValue(int(role["context_window"]))
        self.depth = QSpinBox()
        self.depth.setRange(1, 8)
        self.depth.setValue(int(role["depth"]))

        # Turn budget for the agentic roles. 0 means "not an agentic role"
        # (one-shot phases have no loop), so the control is only shown where
        # the number actually means something.
        self.max_steps = QSpinBox()
        self.max_steps.setRange(0, 500)
        self.max_steps.setValue(int(role.get("max_steps") or 0))
        self.is_agentic = int(role.get("max_steps") or 0) > 0

        # Same is_agentic gate as max_steps -- native tool-calling only
        # means anything for Builder/Renovator's agentic loop, never for a
        # one-shot call_model() request (see native_tool_calling's comment
        # in state.py's _role()).
        self.native_tool_calling = QCheckBox("Native tool-calling")
        self.native_tool_calling.setChecked(bool(role.get("native_tool_calling", False)))
        self.native_tool_calling.setToolTip(
            "Use OpenAI-style tool_calls instead of the ```tool text convention. "
            "Requires the model to actually support mtplx's native tool-call parser -- "
            "confirmed safe for every model this app currently ships (Qwen3.6, Qwen3.8, "
            "Ornith), but untested for anything else."
        )

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
        compact.addWidget(QLabel("KV quant"))
        compact.addWidget(self.kv_quantization)
        compact.addWidget(QLabel("Profile"))
        compact.addWidget(self.profile)
        if self.is_agentic:
            compact.addWidget(QLabel("Max turns"))
            compact.addWidget(self.max_steps)
            compact.addWidget(self.native_tool_calling)
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

        # profile/kv_quantization/depth: not sampling parameters at all,
        # but the same "apply the benchmark-backed recommendation for this
        # model" idea -- see recommended_serving_settings in state.py for
        # what backs each of the three. Silently leaves them unchanged for
        # an unrecognized model family rather than guessing.
        serving = recommended_serving_settings(model)
        if serving is not None:
            profile_index = self.profile.findText(serving["profile"])
            if profile_index >= 0:
                self.profile.setCurrentIndex(profile_index)
            kv_index = self.kv_quantization.findText(serving["kv_quantization"])
            if kv_index >= 0:
                self.kv_quantization.setCurrentIndex(kv_index)
            self.depth.setValue(serving["depth"])
            # Not part of recommended_serving_settings on purpose -- see
            # that function's docstring. Every model actually benchmarked
            # in this app (Qwen3.6, Qwen3.8, Ornith) came back safe under
            # native tool-calling, and it's the fix for a real failure
            # (Ornith failed 0/3 without it on a harder task) -- reasonable
            # to default on for a recognized, agentic-role model. Not
            # touched for one-shot roles (self.is_agentic gates the
            # control's existence, same as max_steps).
            if self.is_agentic:
                self.native_tool_calling.setChecked(True)

    def apply(self, role: dict[str, Any]) -> None:
        role["model"] = self.models.currentData()
        role["reasoning"] = self.reasoning.currentText()
        role["kv_quantization"] = self.kv_quantization.currentText()
        role["profile"] = self.profile.currentText()
        role["context_window"] = self.context.value()
        role["depth"] = self.depth.value()
        role["sampling_mode"] = self.sampling_mode.currentData()
        if self.is_agentic:
            role["max_steps"] = self.max_steps.value()
            role["native_tool_calling"] = self.native_tool_calling.isChecked()
        role["temperature"] = self.temperature.value()
        role["top_p"] = self.top_p.value()
        role["top_k"] = self.top_k.value()
        role["min_p"] = self.min_p.value()
        role["presence_penalty"] = self.presence_penalty.value()
        role["repetition_penalty"] = self.repetition_penalty.value()


class PromptCard(QGroupBox):
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
        self.chat_bridge = ChatBridge()
        self.chat_bridge.chunk.connect(self._chat_chunk)
        self.chat_bridge.finished.connect(self._chat_finished)
        self.chat_busy = False
        self.chat_history: list[dict[str, str]] = []
        self.normal_chat_bridge = ChatBridge()
        self.normal_chat_bridge.chunk.connect(self._normal_chat_chunk)
        self.normal_chat_bridge.finished.connect(self._normal_chat_finished)
        self.normal_chat_busy = False
        self.normal_chat_history: list[dict[str, str]] = []

        # The shared Pipeline orchestrator -- all pipeline logic lives here.
        # The GUI is a thin adapter that translates Pipeline events into Qt signals.
        self.pipeline = Pipeline(
            self.manager,
            lambda: load_state(),
            self._pipeline_event_sink,
        )

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
        tabs.addTab(self._build_chat(models), "Prompt development")
        tabs.addTab(self._build_normal_chat(models), "Chat")
        tabs.addTab(self._build_reports(), "Reports")
        tabs.addTab(self._build_admin(), "Admin")
        self.setCentralWidget(tabs)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_metrics)
        self.timer.start(1200)
        self.refresh_metrics()

    # ------------------------------------------------------------------
    # Pipeline event sink -- translates Pipeline events into Qt signals
    # ------------------------------------------------------------------

    def _pipeline_event_sink(self, event: dict[str, Any]) -> None:
        """Receives events from the Pipeline orchestrator and dispatches them
        to the existing Qt signal handlers (ReportBridge / Bridge)."""
        etype = event.get("type")
        phase = event.get("phase", "")

        if etype == "finished":
            success = event.get("success", False)
            content = event.get("content", "")
            tokens = event.get("tokens") or {}
            if tokens:
                prompt = int(tokens.get("prompt_tokens") or 0)
                completion = int(tokens.get("completion_tokens") or 0)
                self.pipeline_panel.set_phase_stat(
                    phase, "tokens",
                    f"{_thousands(completion)} out / {_thousands(prompt)} in",
                )
            self.report_bridge.finished.emit(phase, success, content)
            # After processing finished, let the Pipeline decide what's next
            result = self.pipeline.on_phase_finished(phase, success, content)
            # Update button states based on the result
            panel = self.pipeline_panel
            status = result.get("status")
            if status == "complete":
                panel.full_suite_button.setEnabled(True)
                panel.step_wise_button.setEnabled(True)
                panel.continue_button.setEnabled(False)
                self.statusBar().showMessage("Pipeline complete", 5000)
            elif status == "step_paused":
                panel.continue_button.setEnabled(True)
            elif status == "failed":
                panel.full_suite_button.setEnabled(True)
                panel.step_wise_button.setEnabled(True)
                panel.continue_button.setEnabled(False)
            # If run_next was called internally (full mode), buttons stay disabled

        elif etype == "chunk":
            piece = event.get("content", "")
            self.report_bridge.chunk.emit(phase, piece)

        elif etype == "ask_question":
            payload = event.get("payload", "")
            self.report_bridge.ask_question.emit(phase, payload)

        elif etype == "phase_status":
            self.pipeline_panel.set_phase_state(phase, event.get("status", ""))

        elif etype == "turns":
            limit = int(event.get("limit") or 0)
            used = int(event.get("used") or 0)
            self.pipeline_panel.set_phase_stat(
                phase, "turns", f"turn {used}/{limit}" if limit else f"turn {used}",
            )

        elif etype == "notification":
            message = event.get("message", "")
            self.pipeline_panel.output.appendPlainText(f"\n{message}\n")
            self.statusBar().showMessage(message, 6000)

    # ------------------------------------------------------------------
    # Deck tab
    # ------------------------------------------------------------------

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
            QLabel(
                "Local uses the Planner role below (its own model/port), not\n"
                "another phase's -- cloud fields here apply only to Cloud (OpenAI)."
            )
        )
        planner_form.addRow("Cloud model", self.planner_model)
        planner_form.addRow("Cloud reasoning", self.planner_effort)
        planner_form.addRow(key_row)
        layout.addWidget(planner_box)
        self.planner_kind.currentIndexChanged.connect(self._update_planner_backend_visibility)
        self._update_planner_backend_visibility()

        self.editors: dict[str, RoleEditor] = {}
        for phase in ("scout", "planner", "builder", "renovator", "auditor"):
            editor = RoleEditor(phase.title(), models, self.state["roles"][phase])
            layout.addWidget(editor)
            self.editors[phase] = editor

        button_row = QHBoxLayout()
        shutdown_button = QPushButton("Shut models down")
        shutdown_button.setObjectName("stopButton")
        shutdown_button.setToolTip(
            "Stop every resident local model and free its RAM. Does not touch "
            "the router or a running pipeline phase."
        )
        shutdown_button.clicked.connect(self.shutdown_models)
        save_button = QPushButton("Save configuration")
        save_button.clicked.connect(self.save_configuration)
        button_row.addWidget(shutdown_button)
        button_row.addStretch()
        button_row.addWidget(save_button)
        layout.addLayout(button_row)
        layout.addStretch()

        scroll.setWidget(body)
        return scroll

    # ------------------------------------------------------------------
    # Telemetry (bottom of Reports tab)
    # ------------------------------------------------------------------

    def _build_telemetry(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)

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

    # ------------------------------------------------------------------
    # Admin tab
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Reports tab
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Chat tab -- draft the prompt before the pipeline ever sees it
    # ------------------------------------------------------------------

    def _build_chat(self, models: list[dict[str, Any]]) -> QWidget:
        """A plain conversation with a model of its own, for working a vague
        idea into a prompt worth spending a full pipeline run on. Separate
        model selector on purpose: the qualities that make a good drafting
        partner here have nothing to do with the tool-loop tuning the
        pipeline roles carry, and switching one must not perturb the other.
        The payoff button is "Send to pipeline", which drops the drafted
        text straight into the Deck tab's task field."""
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(22, 18, 22, 22)

        heading = QLabel("Prompt development")
        heading.setObjectName("title")
        layout.addWidget(heading)
        blurb = QLabel(
            "Talk the task through here first. When the wording is right, "
            "send it to the Deck tab's task field and run the suite."
        )
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Path"))
        self.chat_path_field = QLineEdit()
        self.chat_path_field.setPlaceholderText("Optional: target project path for context")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_chat_path)
        path_row.addWidget(self.chat_path_field, 1)
        path_row.addWidget(browse)
        layout.addLayout(path_row)

        role = self.state["roles"]["prompt_dev"]
        picker = QHBoxLayout()
        picker.addWidget(QLabel("Model"))
        self.chat_model = QComboBox()
        names = [str(item.get("name") or item.get("id") or "") for item in models]
        for name in names:
            if name:
                self.chat_model.addItem(name, name)
        current = str(role["model"])
        if self.chat_model.findData(current) < 0:
            self.chat_model.addItem(current, current)
        self.chat_model.setCurrentIndex(self.chat_model.findData(current))
        picker.addWidget(self.chat_model, 1)
        self.chat_load_button = QPushButton("Load model")
        self.chat_load_button.setObjectName("launchButton")
        self.chat_load_button.setToolTip(
            "Make this the resident local model (stops the others first, "
            "same single-model discipline as the pipeline phases)."
        )
        self.chat_load_button.clicked.connect(self._load_chat_model)
        picker.addWidget(self.chat_load_button)
        layout.addLayout(picker)

        self.chat_transcript = QPlainTextEdit()
        self.chat_transcript.setReadOnly(True)
        self.chat_transcript.setPlaceholderText(
            "Load the chat model, then start describing what you want built."
        )
        mono_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono_font.setPointSize(11)
        self.chat_transcript.setFont(mono_font)
        self.chat_transcript.setMinimumHeight(360)
        layout.addWidget(self.chat_transcript, 1)

        self.chat_input = QPlainTextEdit()
        self.chat_input.setPlaceholderText("Your message…")
        self.chat_input.setMaximumHeight(120)
        layout.addWidget(self.chat_input)

        row = QHBoxLayout()
        self.chat_send_button = QPushButton("Send")
        self.chat_send_button.setObjectName("launchButton")
        self.chat_send_button.clicked.connect(self._chat_send)
        row.addWidget(self.chat_send_button)
        clear_button = QPushButton("Clear conversation")
        clear_button.clicked.connect(self._chat_clear)
        row.addWidget(clear_button)
        row.addStretch(1)
        to_pipeline = QPushButton("Send to pipeline →")
        to_pipeline.setToolTip(
            "Copy the drafted prompt into the Deck tab's task field. Uses "
            "your selection if you've highlighted part of the transcript, "
            "otherwise the model's most recent reply."
        )
        to_pipeline.clicked.connect(self._chat_to_pipeline)
        row.addWidget(to_pipeline)
        layout.addLayout(row)
        return body

    def _load_chat_model(self) -> None:
        self.state["roles"]["prompt_dev"]["model"] = str(self.chat_model.currentData())
        save_state(self.state)
        self.activate("prompt_dev")

    def _chat_send(self) -> None:
        if self.chat_busy:
            return
        message = self.chat_input.toPlainText().strip()
        if not message:
            return
        self.chat_input.clear()
        self.chat_history.append({"role": "user", "content": message})
        self.chat_transcript.appendPlainText(f"\n\n### you\n{message}\n\n### model\n")
        self.chat_busy = True
        self.chat_send_button.setEnabled(False)
        system_content = CHAT_SYSTEM_PROMPT
        path_str = self.chat_path_field.text().strip()
        if path_str:
            root = Path(path_str)
            if root.exists():
                context, included, skipped = self._build_path_context(root)
                if context:
                    note = describe_skipped(skipped)
                    system_content += (
                        "\n\nThe target project is at: " + path_str +
                        ". Reference its structure when discussing scope.\n\nProject files:\n" +
                        context + note
                    )
            else:
                self.statusBar().showMessage(f"Warning: path does not exist: {path_str}", 5000)
        messages = [{"role": "system", "content": system_content}] + list(self.chat_history)

        def work() -> None:
            try:
                text = stream_chat(
                    "prompt_dev", messages,
                    on_chunk=lambda piece: self.chat_bridge.chunk.emit(piece),
                    router_url=self._router_base() + "/v1",
                    timeout=900.0,
                )
            except Exception as exc:
                self.chat_bridge.finished.emit(False, str(exc))
                return
            self.chat_bridge.finished.emit(True, text)

        threading.Thread(target=work, daemon=True).start()

    def _chat_chunk(self, piece: str) -> None:
        cursor = self.chat_transcript.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.chat_transcript.setTextCursor(cursor)
        self.chat_transcript.insertPlainText(piece)
        self.chat_transcript.ensureCursorVisible()

    def _chat_finished(self, success: bool, text: str) -> None:
        self.chat_busy = False
        self.chat_send_button.setEnabled(True)
        if not success:
            self.chat_transcript.appendPlainText(f"\n[failed: {text}]\n")
            self.statusBar().showMessage("Chat request failed", 5000)
            return
        self.chat_history.append({"role": "assistant", "content": text})

    def _chat_clear(self) -> None:
        self.chat_history.clear()
        self.chat_transcript.clear()

    def _chat_to_pipeline(self) -> None:
        selected = self.chat_transcript.textCursor().selectedText().replace("\u2029", "\n")
        draft = selected.strip()
        if not draft:
            for entry in reversed(self.chat_history):
                if entry["role"] == "assistant":
                    draft = entry["content"].strip()
                    break
        if not draft:
            QMessageBox.information(
                self, "Nothing to send",
                "Draft a prompt here first, or select the part of the "
                "transcript you want to use.",
            )
            return
        self.pipeline_panel.task_field.setPlainText(draft)
        self.statusBar().showMessage(
            "Prompt copied into the Deck tab's task field", 5000
        )

    def _browse_chat_path(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose a project path")
        if directory:
            self.chat_path_field.setText(directory)

    def _browse_normal_chat_path(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose a project path")
        if directory:
            self.normal_chat_path_field.setText(directory)

    def _build_path_context(self, root: Path) -> tuple[str, list[Path], list[Path]]:
        """Collect and budget file contents for the given path."""
        files = collect_files(root, DEFAULT_EXTENSIONS)
        if not files:
            return "", [], []
        context, included, skipped = build_context(files, DEFAULT_CHAR_BUDGET, cache_target=root)
        return context, included, skipped

    # ------------------------------------------------------------------
    # Normal Chat tab
    # ------------------------------------------------------------------

    def _build_normal_chat(self, models: list[dict[str, Any]]) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(22, 18, 22, 22)

        heading = QLabel("Chat")
        heading.setObjectName("title")
        layout.addWidget(heading)
        blurb = QLabel(
            "Ask questions about your project, request actions, or just talk "
            "through ideas. Specify a path to ground responses in actual files."
        )
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Path"))
        self.normal_chat_path_field = QLineEdit()
        self.normal_chat_path_field.setPlaceholderText("Project path to ground answers in")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_normal_chat_path)
        path_row.addWidget(self.normal_chat_path_field, 1)
        path_row.addWidget(browse)
        layout.addLayout(path_row)

        role = self.state["roles"]["chat"]
        picker = QHBoxLayout()
        picker.addWidget(QLabel("Model"))
        self.normal_chat_model = QComboBox()
        names = [str(item.get("name") or item.get("id") or "") for item in models]
        for name in names:
            if name:
                self.normal_chat_model.addItem(name, name)
        current = str(role["model"])
        if self.normal_chat_model.findData(current) < 0:
            self.normal_chat_model.addItem(current, current)
        self.normal_chat_model.setCurrentIndex(self.normal_chat_model.findData(current))
        picker.addWidget(self.normal_chat_model, 1)
        self.normal_chat_load_button = QPushButton("Load model")
        self.normal_chat_load_button.setObjectName("launchButton")
        self.normal_chat_load_button.setToolTip(
            "Make this the resident local model (stops the others first, "
            "same single-model discipline as the pipeline phases)."
        )
        self.normal_chat_load_button.clicked.connect(self._load_normal_chat_model)
        picker.addWidget(self.normal_chat_load_button)
        layout.addLayout(picker)

        self.normal_chat_transcript = QPlainTextEdit()
        self.normal_chat_transcript.setReadOnly(True)
        self.normal_chat_transcript.setPlaceholderText(
            "Load the chat model, then ask about your project."
        )
        mono_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono_font.setPointSize(11)
        self.normal_chat_transcript.setFont(mono_font)
        self.normal_chat_transcript.setMinimumHeight(360)
        layout.addWidget(self.normal_chat_transcript, 1)

        self.normal_chat_input = QPlainTextEdit()
        self.normal_chat_input.setPlaceholderText("Your message…")
        self.normal_chat_input.setMaximumHeight(120)
        layout.addWidget(self.normal_chat_input)

        row = QHBoxLayout()
        self.normal_chat_send_button = QPushButton("Send")
        self.normal_chat_send_button.setObjectName("launchButton")
        self.normal_chat_send_button.clicked.connect(self._normal_chat_send)
        row.addWidget(self.normal_chat_send_button)
        clear_button = QPushButton("Clear conversation")
        clear_button.clicked.connect(self._normal_chat_clear)
        row.addWidget(clear_button)
        row.addStretch(1)
        layout.addLayout(row)
        return body

    def _load_normal_chat_model(self) -> None:
        self.state["roles"]["chat"]["model"] = str(self.normal_chat_model.currentData())
        save_state(self.state)
        self.activate("chat")

    def _normal_chat_send(self) -> None:
        if self.normal_chat_busy:
            return
        message = self.normal_chat_input.toPlainText().strip()
        if not message:
            return
        self.normal_chat_input.clear()
        self.normal_chat_history.append({"role": "user", "content": message})
        self.normal_chat_transcript.appendPlainText(f"\n\n### you\n{message}\n\n### model\n")
        self.normal_chat_busy = True
        self.normal_chat_send_button.setEnabled(False)

        system_content = NORMAL_CHAT_SYSTEM_PROMPT
        path_str = self.normal_chat_path_field.text().strip()
        if path_str:
            root = Path(path_str)
            if root.exists():
                context, included, skipped = self._build_path_context(root)
                if context:
                    note = describe_skipped(skipped)
                    system_content += (
                        "\n\nThe user's project is at: " + path_str +
                        ".\n\nProject files:\n" + context + note
                    )
            else:
                self.statusBar().showMessage(f"Warning: path does not exist: {path_str}", 5000)
        messages = [{"role": "system", "content": system_content}] + list(self.normal_chat_history)

        def work() -> None:
            try:
                text = stream_chat(
                    "chat", messages,
                    on_chunk=lambda piece: self.normal_chat_bridge.chunk.emit(piece),
                    router_url=self._router_base() + "/v1",
                    timeout=900.0,
                )
            except Exception as exc:
                self.normal_chat_bridge.finished.emit(False, str(exc))
                return
            self.normal_chat_bridge.finished.emit(True, text)

        threading.Thread(target=work, daemon=True).start()

    def _normal_chat_chunk(self, piece: str) -> None:
        cursor = self.normal_chat_transcript.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.normal_chat_transcript.setTextCursor(cursor)
        self.normal_chat_transcript.insertPlainText(piece)
        self.normal_chat_transcript.ensureCursorVisible()

    def _normal_chat_finished(self, success: bool, text: str) -> None:
        self.normal_chat_busy = False
        self.normal_chat_send_button.setEnabled(True)
        if not success:
            self.normal_chat_transcript.appendPlainText(f"\n[failed: {text}]\n")
            self.statusBar().showMessage("Chat request failed", 5000)
            return
        self.normal_chat_history.append({"role": "assistant", "content": text})

    def _normal_chat_clear(self) -> None:
        self.normal_chat_history.clear()
        self.normal_chat_transcript.clear()

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

    # ------------------------------------------------------------------
    # Pipeline control (thin adapters over self.pipeline)
    # ------------------------------------------------------------------

    def _start_pipeline(self, mode: str) -> None:
        path = self.pipeline_panel.path_field.text().strip()
        task = self.pipeline_panel.task_field.toPlainText().strip()
        start_phase = str(self.pipeline_panel.start_phase.currentData())

        result = self.pipeline.start(mode, path, task, start_phase)
        if "error" in result:
            QMessageBox.information(self, "Pipeline", result["error"])
            return

        panel = self.pipeline_panel
        panel.reset_status()
        panel.output.clear()
        panel.full_suite_button.setEnabled(False)
        panel.step_wise_button.setEnabled(False)
        panel.continue_button.setEnabled(False)
        panel.stop_button.setEnabled(True)
        panel.output.appendPlainText(f"\n=== {start_phase.upper()} ===\n")

    def _on_pipeline_continue_clicked(self) -> None:
        self.pipeline_panel.continue_button.setEnabled(False)
        self.pipeline_panel.stop_button.setEnabled(True)
        result = self.pipeline.run_next()
        if result.get("status") == "complete":
            self.pipeline_panel.full_suite_button.setEnabled(True)
            self.pipeline_panel.step_wise_button.setEnabled(True)
            self.statusBar().showMessage("Pipeline complete", 5000)

    def _on_pipeline_stop_clicked(self) -> None:
        self.pipeline.stop()
        self.pipeline_panel.stop_button.setEnabled(False)

    def _tick_report_status(self) -> None:
        now = time.monotonic()
        panel = getattr(self, "pipeline_panel", None)
        if panel is None:
            return
        for phase, start in list(self.pipeline.report_start_times.items()):
            if phase in panel.phase_status:
                panel.set_phase_state(phase, f"running… {now - start:.0f}s")

    # ------------------------------------------------------------------
    # Report event handlers (Qt thread)
    # ------------------------------------------------------------------

    def _report_chunk(self, phase: str, piece: str) -> None:
        panel = self.pipeline_panel
        cursor = panel.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        panel.output.setTextCursor(cursor)
        panel.output.insertPlainText(piece)
        panel.output.ensureCursorVisible()

    def _on_ask_question(self, phase: str, payload: str) -> None:
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

        # Unblock the pipeline worker thread via the Pipeline's answer method
        self.pipeline.answer(phase, answer)

    def _report_finished(self, phase: str, success: bool, text: str) -> None:
        panel = self.pipeline_panel
        start = self.pipeline.report_start_times.pop(phase, None)
        elapsed = f" ({time.monotonic() - start:.0f}s)" if start is not None else ""
        panel.set_phase_state(phase, f"{'done' if success else 'failed'}{elapsed}")
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
            QMessageBox.warning(self, f"{phase.title()} failed", text[:2000])
            return

        if phase == "auditor":
            # The auditor scores how much of the plan the builder actually
            # landed. It hangs off the *builder's* label, not the auditor's,
            # because it is a measurement of the builder's configuration --
            # the whole point of asking for it (see auditor_report.py).
            accuracy = parse_builder_accuracy(text)
            if accuracy is not None:
                panel.set_phase_stat("builder", "accuracy", f"{accuracy}% accurate")
            verdict = parse_verdict(text)
            if verdict != "PASS":
                # The Pipeline's on_phase_finished already handled the queue logic.
                # Here we just show the user-facing dialog.
                if self.pipeline.pipeline_renovator_retries > 0:
                    pass  # renovator was queued by on_phase_finished
                else:
                    QMessageBox.warning(
                        self, "Auditor rejected again",
                        f"Auditor's verdict is still {verdict} after a repair pass. "
                        "Stopping here for manual review rather than looping "
                        "further -- see the output above and .ai/audit-report.md.",
                    )
                    return

        if self.pipeline.pipeline_mode == "full":
            return  # on_phase_finished already called run_next

        if self.pipeline.pipeline_queue:
            QMessageBox.information(
                self, f"{phase.title()} complete",
                f"{phase.title()} finished successfully{elapsed}. Review its output, "
                f"then click Continue to run {self.pipeline.pipeline_queue[0].title()} next.",
            )

    # ------------------------------------------------------------------
    # Configuration / activation
    # ------------------------------------------------------------------

    def save_configuration(self) -> None:
        for phase, editor in self.editors.items():
            editor.apply(self.state["roles"][phase])
        self.state["planner"]["kind"] = str(self.planner_kind.currentData())
        self.state["planner"]["model"] = self.planner_model.text().strip()
        self.state["planner"]["reasoning_effort"] = self.planner_effort.currentText()
        save_state(self.state)
        self.statusBar().showMessage("Configuration saved", 3000)

    def _update_planner_backend_visibility(self) -> None:
        is_local = self.planner_kind.currentData() == "local"
        self.planner_model.setEnabled(not is_local)
        self.planner_effort.setEnabled(not is_local)

    def shutdown_models(self) -> None:
        """Stop every resident local model. Runs off the UI thread because
        stop_local_models waits for each port to actually free."""
        state = load_state()
        ports = [int(r["port"]) for r in state["roles"].values()]

        def work() -> None:
            try:
                self.manager.stop_local_models(ports)
            except Exception as exc:
                self.bridge.failed.emit("shutdown", str(exc))
                return
            self.bridge.succeeded.emit("shutdown", "All local models stopped")

        self.statusBar().showMessage("Stopping local models…")
        threading.Thread(target=work, daemon=True).start()

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
                    # Planner has its own role now; a local planner launches
                    # roles["planner"] rather than borrowing another phase's.
                    local_phase = phase
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
        if phase == "shutdown":
            self.statusBar().showMessage(detail, 5000)
            self.refresh_metrics()
            return
        self.statusBar().showMessage(f"{phase.title()} active · {detail}")
        self.refresh_metrics()

    def _phase_failed(self, phase: str, message: str) -> None:
        self.busy = False
        self._set_buttons_enabled(True)
        if phase == "shutdown":
            self.statusBar().showMessage("Shutdown failed", 5000)
            QMessageBox.critical(self, "Shutdown failed", message)
            return
        self.statusBar().showMessage(f"Could not activate {phase}")
        QMessageBox.critical(self, "Phase switch failed", message)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self.launch_button.setEnabled(enabled)
        for button in self.phase_buttons.values():
            button.setEnabled(enabled)
        chat_load = getattr(self, "chat_load_button", None)
        if chat_load is not None:
            chat_load.setEnabled(enabled)
        normal_chat_load = getattr(self, "normal_chat_load_button", None)
        if normal_chat_load is not None:
            normal_chat_load.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Metrics / telemetry
    # ------------------------------------------------------------------

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
            self.pipeline._last_prefill_rate[phase] = float(latest["prefill_tok_s"])
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
        known_rate = self.pipeline._last_prefill_rate.get(role_phase)
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
