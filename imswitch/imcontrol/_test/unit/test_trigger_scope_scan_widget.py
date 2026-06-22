from types import SimpleNamespace

import pytest

from imswitch.imcontrol.view.widgets.TriggerScopeScanWidget import TriggerScopeScanWidget


pytestmark = pytest.mark.nohardware


def test_trigger_scope_scan_widget_set_current_mode_alias(qtbot):
    """State restore code can select the unified TriggerScope scan mode."""
    widget = TriggerScopeScanWidget(SimpleNamespace())
    qtbot.addWidget(widget)

    widget.setCurrentMode(widget.MODE_PLSR)

    assert widget.currentMode() == widget.MODE_PLSR
    assert widget.stack.currentWidget() is widget.plsrPage
