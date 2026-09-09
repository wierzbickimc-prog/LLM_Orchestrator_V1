from __future__ import annotations

import os
import unittest

# Must be set before PySide6 is imported anywhere in the process -- there is
# no display in CI/this environment, and the offscreen platform plugin is
# what lets QApplication/widgets construct without one.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from modeldeck.gui import RoleEditor  # noqa: E402
from modeldeck.state import default_state  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
