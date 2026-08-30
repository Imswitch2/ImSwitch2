"""No-hardware coverage for the Positioner legacy-feature port."""
import pytest

from imswitch.imcontrol.view.widgets import PositionerWidget

pytestmark = pytest.mark.nohardware


def test_coarse_mode_preserves_fine_step_and_units(qtbot):
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    widget.addPositioner('Stage', ['X'], speed=False, joystick=False, unit='mm')

    widget.setStepSize('Stage', 'X', 2.5)
    widget.setCoarseStepMultiplier(4)
    widget.setStepMode(True)

    assert widget.getStepSize('Stage', 'X') == 2.5
    assert widget.pars['CoarseStepPreviewStage--X'].text() == 'Coarse: 10'
    assert 'mm' in widget.pars['PositionStage--X'].text()
    assert widget.pars['StepUnitStage--X'].text().strip() == 'mm'


def test_step_mode_signal_and_global_step_entry_point_remain_compatible(qtbot):
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    widget.addPositioner('Stage', ['X'], speed=False, joystick=False)

    modes = []
    moves = []
    widget.sigStepModeChanged.connect(modes.append)
    widget.sigStepUpClicked.connect(lambda positioner, axis: moves.append((positioner, axis)))

    widget.pars['CoarseModeButton'].click()
    widget.stepAxis('Stage', 'X', 'plus')

    assert modes == [True]
    assert moves == [('Stage', 'X')]

    layout = widget.pars['StepModeWidget'].layout()
    assert layout.indexOf(widget.pars['FineModeButton']) < layout.indexOf(
        widget.pars['CoarseModeButton']
    )


def test_settings_button_is_present_without_private_shortcut_editor(qtbot):
    widget = PositionerWidget({})
    qtbot.addWidget(widget)

    assert 'SettingsButton' in widget.pars
    # Legacy QKeySequenceEdit-based shortcut configuration must not be reintroduced.
    assert not any('Shortcut' in key for key in widget.pars)
