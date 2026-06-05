"""Tests for PositionerWidget keyboard shortcuts."""
import pytest
from imswitch.imcontrol.view.widgets import PositionerWidget
from imswitch.imcommon.model import generateShortcuts

pytestmark = pytest.mark.nohardware


def test_axis_shortcuts_basic(qtbot):
    """Test that each of the six shortcut methods emits the correct signal
    for a positioner with X, Y, Z axes."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    # Add a single positioner with X, Y, Z axes
    widget.addPositioner('Stage', ['X', 'Y', 'Z'], speed=False, joystick=False)
    
    # Connect spies to both signals
    up_signals = []
    down_signals = []
    widget.sigStepUpClicked.connect(lambda positioner, axis: up_signals.append((positioner, axis)))
    widget.sigStepDownClicked.connect(lambda positioner, axis: down_signals.append((positioner, axis)))
    
    # Test X axis
    widget.stepXPlus()
    assert up_signals == [('Stage', 'X')]
    assert down_signals == []
    
    up_signals.clear()
    down_signals.clear()
    
    widget.stepXMinus()
    assert up_signals == []
    assert down_signals == [('Stage', 'X')]
    
    up_signals.clear()
    down_signals.clear()
    
    # Test Y axis
    widget.stepYPlus()
    assert up_signals == [('Stage', 'Y')]
    assert down_signals == []
    
    up_signals.clear()
    down_signals.clear()
    
    widget.stepYMinus()
    assert up_signals == []
    assert down_signals == [('Stage', 'Y')]
    
    up_signals.clear()
    down_signals.clear()
    
    # Test Z axis
    widget.stepZPlus()
    assert up_signals == [('Stage', 'Z')]
    assert down_signals == []
    
    up_signals.clear()
    down_signals.clear()
    
    widget.stepZMinus()
    assert up_signals == []
    assert down_signals == [('Stage', 'Z')]


def test_axis_shortcuts_case_insensitive(qtbot):
    """Test that shortcuts work with lowercase axis names."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    # Add positioner with lowercase axes
    widget.addPositioner('Stage', ['x', 'y', 'z'], speed=False, joystick=False)
    
    up_signals = []
    widget.sigStepUpClicked.connect(lambda positioner, axis: up_signals.append((positioner, axis)))
    
    # Should still work with lowercase
    widget.stepXPlus()
    assert up_signals == [('Stage', 'x')]
    
    up_signals.clear()
    
    widget.stepYPlus()
    assert up_signals == [('Stage', 'y')]
    
    up_signals.clear()
    
    widget.stepZPlus()
    assert up_signals == [('Stage', 'z')]


def test_axis_shortcuts_first_positioner_wins(qtbot):
    """Test that when multiple positioners have the same axis,
    the first one registered takes precedence."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    # Add two positioners with overlapping axes
    widget.addPositioner('StageA', ['X'], speed=False, joystick=False)
    widget.addPositioner('StageB', ['X', 'Z'], speed=False, joystick=False)
    
    up_signals = []
    widget.sigStepUpClicked.connect(lambda positioner, axis: up_signals.append((positioner, axis)))
    
    # X should go to StageA (first registered)
    widget.stepXPlus()
    assert up_signals == [('StageA', 'X')]
    
    up_signals.clear()
    
    # Z should go to StageB (only one with Z)
    widget.stepZPlus()
    assert up_signals == [('StageB', 'Z')]


def test_axis_shortcuts_missing_axis_noop(qtbot):
    """Test that calling a shortcut for a non-existent axis
    is a no-op (no signal, no exception)."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    # Add positioner with only X axis
    widget.addPositioner('Stage', ['X'], speed=False, joystick=False)
    
    up_signals = []
    down_signals = []
    widget.sigStepUpClicked.connect(lambda positioner, axis: up_signals.append((positioner, axis)))
    widget.sigStepDownClicked.connect(lambda positioner, axis: down_signals.append((positioner, axis)))
    
    # Y and Z don't exist, should be no-op
    widget.stepYPlus()
    widget.stepYMinus()
    widget.stepZPlus()
    widget.stepZMinus()
    
    assert up_signals == []
    assert down_signals == []
    
    # X should still work
    widget.stepXPlus()
    assert up_signals == [('Stage', 'X')]
    assert down_signals == []
    
    up_signals.clear()
    
    widget.stepXMinus()
    assert up_signals == []
    assert down_signals == [('Stage', 'X')]


def test_generate_shortcuts_picks_up_all_six(qtbot):
    """Test that generateShortcuts() picks up all twelve shortcut methods."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)

    # Generate shortcuts
    shortcuts = generateShortcuts([widget])

    # Primary (Ctrl) + secondary (Ctrl+Shift) sets = 12 shortcuts.
    expected_keys = {
        'stepXPlus', 'stepXMinus',
        'stepYPlus', 'stepYMinus',
        'stepZPlus', 'stepZMinus',
        'stepXPlusSecondary', 'stepXMinusSecondary',
        'stepYPlusSecondary', 'stepYMinusSecondary',
        'stepZPlusSecondary', 'stepZMinusSecondary',
    }

    actual_keys = set(shortcuts.keys())
    assert expected_keys.issubset(actual_keys), f"Missing shortcuts: {expected_keys - actual_keys}"
    assert shortcuts['stepXPlusSecondary']['key'] == 'Ctrl+Shift+Right'


def test_explicit_modifier_targets_named_positioner(qtbot):
    """An explicit 'ctrl' positioner wins the primary set over a legacy
    (no-modifier) positioner registered earlier, and a 'ctrl-shift' positioner
    drives the secondary set."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)

    # Order mirrors the setup file: piezo Stage on ctrl-shift first, an
    # unmodified Galvo claiming X, then the mechanical BSC203 on ctrl.
    widget.addPositioner('Stage X', ['X'], speed=False, joystick=False,
                         shortcutModifier='ctrl-shift')
    widget.addPositioner('Galvo', ['X'], speed=False, joystick=False)
    widget.addPositioner('BSC203', ['X', 'Y', 'Z'], speed=False, joystick=False,
                         shortcutModifier='ctrl')

    up = []
    widget.sigStepUpClicked.connect(lambda p, a: up.append((p, a)))

    # Primary Ctrl set -> BSC203 (explicit), despite Galvo's earlier legacy claim
    widget.stepXPlus()
    assert up == [('BSC203', 'X')]
    up.clear()

    # Secondary Ctrl+Shift set -> the piezo stage
    widget.stepXPlusSecondary()
    assert up == [('Stage X', 'X')]


def test_secondary_set_empty_without_modifier(qtbot):
    """Legacy configs (no shortcutModifier) leave the secondary set unbound,
    so Ctrl+Shift shortcuts are a no-op."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    widget.addPositioner('Stage', ['X', 'Y', 'Z'], speed=False, joystick=False)

    up = []
    widget.sigStepUpClicked.connect(lambda p, a: up.append((p, a)))

    widget.stepXPlusSecondary()
    widget.stepYPlusSecondary()
    widget.stepZPlusSecondary()
    assert up == []

    # Primary still works as before (legacy first-wins)
    widget.stepXPlus()
    assert up == [('Stage', 'X')]
