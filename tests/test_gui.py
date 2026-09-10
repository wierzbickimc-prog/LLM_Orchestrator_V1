from __future__ import annotations

import os
import unittest

# Must be set before PySide6 is imported anywhere in the process -- there is
# no display in CI/this environment, and the offscreen platform plugin is
# what lets QApplication/widgets construct without one.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from modeldeck.gui import PipelinePanel, RoleEditor  # noqa: E402
from modeldeck.state import default_state, recommended_serving_settings  # noqa: E402

_app = QApplication.instance() or QApplication([])


class RoleEditorKvQuantizationTests(unittest.TestCase):
    """kv_quantization used to be settable only by hand-editing state.json
    (see mtplx.py's model_command, which has always read it per-role) --
    MoE models are reportedly unusually sensitive to it, so it needed a
    real control in the Deck tab, not just a config-file knob."""

    def test_offers_exactly_mtplxs_valid_values(self) -> None:
        role = default_state()["roles"]["scout"]
        editor = RoleEditor("Scout", [], role)
        values = [editor.kv_quantization.itemText(i) for i in range(editor.kv_quantization.count())]
        self.assertEqual(values, ["off", "q8", "q4"])

    def test_loads_the_roles_existing_value(self) -> None:
        role = dict(default_state()["roles"]["scout"])
        role["kv_quantization"] = "q4"
        editor = RoleEditor("Scout", [], role)
        self.assertEqual(editor.kv_quantization.currentText(), "q4")

    def test_an_unrecognized_stored_value_falls_back_to_q8_not_a_crash(self) -> None:
        role = dict(default_state()["roles"]["scout"])
        role["kv_quantization"] = "not-a-real-mode"
        editor = RoleEditor("Scout", [], role)
        self.assertEqual(editor.kv_quantization.currentText(), "q8")

    def test_apply_writes_the_selected_value_back_to_the_role(self) -> None:
        role = dict(default_state()["roles"]["builder"])
        editor = RoleEditor("Builder", [], role)
        editor.kv_quantization.setCurrentText("off")
        out = dict(role)
        editor.apply(out)
        self.assertEqual(out["kv_quantization"], "off")


class RoleEditorProfileAndNativeToolCallingTests(unittest.TestCase):
    """profile and native_tool_calling followed kv_quantization's pattern:
    both were per-role config with no Deck tab control at all, only settable
    by hand-editing state.json."""

    def test_profile_offers_mtplxs_full_valid_set(self) -> None:
        role = default_state()["roles"]["builder"]
        editor = RoleEditor("Builder", [], role)
        values = [editor.profile.itemText(i) for i in range(editor.profile.count())]
        self.assertEqual(
            values, ["turbo", "sustained", "stable", "performance-cold", "exact", "max-diagnostic"],
        )

    def test_profile_loads_the_roles_existing_value(self) -> None:
        role = dict(default_state()["roles"]["renovator"])
        self.assertEqual(role["profile"], "turbo")
        editor = RoleEditor("Renovator", [], role)
        self.assertEqual(editor.profile.currentText(), "turbo")

    def test_apply_writes_the_selected_profile_back_to_the_role(self) -> None:
        role = dict(default_state()["roles"]["builder"])
        editor = RoleEditor("Builder", [], role)
        editor.profile.setCurrentText("stable")
        out = dict(role)
        editor.apply(out)
        self.assertEqual(out["profile"], "stable")

    def test_native_tool_calling_control_only_exists_for_agentic_roles(self) -> None:
        agentic = RoleEditor("Builder", [], dict(default_state()["roles"]["builder"]))
        self.assertTrue(agentic.is_agentic)
        self.assertTrue(hasattr(agentic, "native_tool_calling"))

        one_shot = RoleEditor("Planner", [], dict(default_state()["roles"]["planner"]))
        self.assertFalse(one_shot.is_agentic)
        # The attribute still exists (built unconditionally, same as
        # max_steps), but apply() must never read it for a one-shot role --
        # see the next test.
        self.assertTrue(hasattr(one_shot, "native_tool_calling"))

    def test_apply_never_writes_native_tool_calling_for_a_one_shot_role(self) -> None:
        role = dict(default_state()["roles"]["planner"])
        role.pop("native_tool_calling", None)
        editor = RoleEditor("Planner", [], role)
        editor.native_tool_calling.setChecked(True)
        out = dict(role)
        editor.apply(out)
        self.assertNotIn("native_tool_calling", out)

    def test_apply_preset_pulls_profile_kv_and_depth_from_the_benchmark_table(self) -> None:
        role = dict(default_state()["roles"]["builder"])
        editor = RoleEditor("Builder", [], role)
        quality = "Youssofal/Qwen3.8-27B-MTPLX-Optimized-Quality-FP16"
        idx = editor.models.findData(quality)
        if idx < 0:
            editor.models.addItem("Quality", quality)
            idx = editor.models.count() - 1
        editor.models.setCurrentIndex(idx)
        editor.sampling_mode.setCurrentIndex(editor.sampling_mode.findData("instruct"))
        editor.profile.setCurrentText("sustained")  # force a value the preset must overwrite
        editor.kv_quantization.setCurrentText("off")
        editor.depth.setValue(1)
        editor.native_tool_calling.setChecked(False)

        editor._apply_sampling_preset()

        self.assertEqual(editor.profile.currentText(), "turbo")
        self.assertEqual(editor.kv_quantization.currentText(), "q8")
        self.assertEqual(editor.depth.value(), 3)
        self.assertTrue(editor.native_tool_calling.isChecked())

    def test_serving_settings_lookup_returns_none_for_an_unrecognized_model(self) -> None:
        # _apply_sampling_preset's own "no published preset" early-return
        # (a blocking QMessageBox) already covers an unrecognized model at
        # the GUI layer -- exercising that path here would pop a real modal
        # dialog under the offscreen platform and hang the test run. The
        # behavior this test actually cares about (recommended_serving_
        # settings must not fabricate a recommendation for a model it
        # doesn't know) is fully covered at the layer that owns it.
        self.assertIsNone(recommended_serving_settings("some-org/totally-unrecognized-model"))


class PipelinePanelFlowTests(unittest.TestCase):
    """The Reports tab grew a Feature/Troubleshoot flow selector; picking a
    flow reshapes the "Start from" dropdown to that flow's phases."""

    def test_feature_flow_offers_the_scout_led_phases(self) -> None:
        panel = PipelinePanel()
        phases = [panel.start_phase.itemData(i) for i in range(panel.start_phase.count())]
        self.assertEqual(phases, ["scout", "planner", "builder", "auditor"])

    def test_switching_to_troubleshoot_swaps_scout_for_diagnose(self) -> None:
        panel = PipelinePanel()
        panel.flow.setCurrentIndex(panel.flow.findData("troubleshoot"))
        phases = [panel.start_phase.itemData(i) for i in range(panel.start_phase.count())]
        self.assertEqual(phases, ["diagnose", "planner", "builder", "auditor"])

    def test_a_shared_start_phase_survives_the_flow_switch(self) -> None:
        panel = PipelinePanel()
        panel.start_phase.setCurrentIndex(panel.start_phase.findData("builder"))
        panel.flow.setCurrentIndex(panel.flow.findData("troubleshoot"))
        self.assertEqual(panel.start_phase.currentData(), "builder")


if __name__ == "__main__":
    unittest.main()
