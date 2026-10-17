"""The retired PointScan scan panel: clear error, and its script API on Advanced."""

from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.controllers.ScanControllerAdvanced import (
    ScanControllerAdvanced,
)
from imswitch.imcontrol.model.SetupInfo import retiredScanWidgetTypeError
from imswitch.imcontrol.view.ImConMainView import ImConMainView


@pytest.mark.parametrize('current', ['Base', 'Advanced', 'TriggerScope'])
def test_current_scan_widget_types_are_not_retired(current):
    assert retiredScanWidgetTypeError(current) is None


def test_pointscan_error_names_the_advanced_panel():
    error = retiredScanWidgetTypeError('PointScan')
    assert isinstance(error, ValueError)
    assert '"Advanced"' in str(error)
    assert 'AdvancedScanTTLCycleDesigner' in str(error)


def test_main_view_refuses_a_pointscan_setup_before_building_widgets():
    view = ImConMainView.__new__(ImConMainView)
    view.viewSetupInfo = SimpleNamespace(scan=SimpleNamespace(scanWidgetType='PointScan'))
    view.widgets = {}
    with pytest.raises(ValueError, match='Advanced'):
        view._addDocks({'Scan': SimpleNamespace(name='Scan', yPosition=0)}, None, 'left')
    assert view.widgets == {}


def test_changed3StepDelayPar_sets_the_advanced_widget_field():
    calls = []
    controller = ScanControllerAdvanced.__new__(ScanControllerAdvanced)
    controller._widget = SimpleNamespace(setd3StepDelayPar=calls.append)

    ScanControllerAdvanced.changed3StepDelayPar(controller, 25)

    assert calls == [25]


def test_changed3StepDelayPar_is_a_script_api_export():
    # APIExport marks the function; scripts reach it as api.imcontrol.<name>.
    method = ScanControllerAdvanced.__dict__['changed3StepDelayPar']
    assert method._APIExport is True
    assert method._APIRunOnUIThread is True


def test_main_view_skips_a_layout_key_that_names_no_widget():
    """A widgetLayout naming a removed panel (the single-SLM "SLM") is skipped
    with a warning instead of stopping ImControl, as availableWidgets does."""
    warnings = []
    view = ImConMainView.__new__(ImConMainView)
    view.viewSetupInfo = SimpleNamespace(scan=SimpleNamespace(scanWidgetType='Base'))
    view.widgets = {}
    view.docks = {}
    view._ImConMainView__logger = SimpleNamespace(warning=warnings.append)

    docks = view._addDocks({'SLM': SimpleNamespace(name='SLM', yPosition=0)}, None, 'left')

    assert docks == []
    assert view.widgets == {}
    assert len(warnings) == 1 and '"SLM"' in warnings[0]
