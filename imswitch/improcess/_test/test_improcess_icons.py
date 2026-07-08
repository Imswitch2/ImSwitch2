import os
import builtins
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtGui, QtWidgets

from imswitch.improcess.model.runtime_tools import runtime_analysis_panel_shortcuts
import imswitch.improcess.view.icons as icons_module
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

FILE_TOOL_ACTION_IDS = (
    "quick-load-data",
    "quick-load-virtual-data",
    "save-reconstruction",
)


def test_improcess_icon_mapping_covers_toolbar_actions():
    expected = set(FILE_TOOL_ACTION_IDS)
    expected.update(IMAGE_TOOL_ACTION_IDS)
    expected.update(shortcut.id for shortcut in runtime_analysis_panel_shortcuts())
    expected.add("results-table")

    assert expected.issubset(IMPROCESS_ICON_NAMES)


def test_improcess_icon_mapping_uses_supported_qtawesome_families():
    supported = ("mdi.", "mdi6.", "fa5s.", "fa6s.", "ph.")

    assert all(
        icon_name.startswith(supported)
        for icon_name in IMPROCESS_ICON_NAMES.values()
    )


def test_improcess_icon_mapping_uses_requested_panel_icons():
    assert IMPROCESS_ICON_NAMES["graph"] == "fa6s.chart-line"
    assert IMPROCESS_ICON_NAMES["profile"] == "mdi6.vector-line"
    assert IMPROCESS_ICON_NAMES["roi-stats"] == "mdi.format-list-numbered"


def test_improcess_icon_returns_qicon(qtbot):
    widget = QtWidgets.QWidget()
    qtbot.addWidget(widget)

    action_ids = [*FILE_TOOL_ACTION_IDS, *IMAGE_TOOL_ACTION_IDS, "results-table"]
    action_ids.extend(shortcut.id for shortcut in runtime_analysis_panel_shortcuts())
    for action_id in action_ids:
        assert isinstance(improcessIcon(action_id, widget), QtGui.QIcon)


def test_improcess_unknown_icon_uses_fallback(qtbot):
    widget = QtWidgets.QWidget()
    qtbot.addWidget(widget)

    icon = improcessIcon("unknown-action", widget)

    assert isinstance(icon, QtGui.QIcon)


def test_improcess_icon_warns_once_when_qtawesome_missing(monkeypatch, qtbot):
    widget = QtWidgets.QWidget()
    qtbot.addWidget(widget)
    warnings = []
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "qtawesome":
            raise ImportError("missing in test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(icons_module, "_QTAWESOME_MISSING_WARNED", False)
    monkeypatch.setattr(
        icons_module,
        "_iconsLogger",
        lambda: SimpleNamespace(warning=lambda message: warnings.append(message)),
    )

    assert isinstance(improcessIcon("auto-contrast", widget), QtGui.QIcon)
    assert isinstance(improcessIcon("auto-contrast", widget), QtGui.QIcon)

    assert len(warnings) == 1
    assert "qtawesome" in warnings[0]
