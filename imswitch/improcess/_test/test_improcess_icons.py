import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtGui, QtWidgets

from imswitch.improcess.model.runtime_tools import runtime_analysis_panel_shortcuts
from imswitch.improcess.view.icons import IMPROCESS_ICON_NAMES, improcessIcon


IMAGE_TOOL_ACTION_IDS = (
    "auto-contrast",
    "brightness-contrast",
    "reset-contrast",
    "channels",
    "duplicate",
    "crop-substack",
    "max-projection",
    "split-stack",
    "split-channels",
    "merge-channels",
    "make-composite",
    "make-rgb",
    "reset-view",
)


def test_improcess_icon_mapping_covers_toolbar_actions():
    expected = set(IMAGE_TOOL_ACTION_IDS)
    expected.update(shortcut.id for shortcut in runtime_analysis_panel_shortcuts())

    assert expected.issubset(IMPROCESS_ICON_NAMES)


def test_improcess_icon_mapping_uses_one_qtawesome_family():
    assert all(
        icon_name.startswith("mdi6.")
        for icon_name in IMPROCESS_ICON_NAMES.values()
    )


def test_improcess_icon_returns_qicon(qtbot):
    widget = QtWidgets.QWidget()
    qtbot.addWidget(widget)

    for action_id in IMAGE_TOOL_ACTION_IDS:
        assert isinstance(improcessIcon(action_id, widget), QtGui.QIcon)


def test_improcess_unknown_icon_uses_fallback(qtbot):
    widget = QtWidgets.QWidget()
    qtbot.addWidget(widget)

    icon = improcessIcon("unknown-action", widget)

    assert isinstance(icon, QtGui.QIcon)
