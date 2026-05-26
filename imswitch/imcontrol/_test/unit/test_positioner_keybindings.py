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
    """Test that generateShortcuts() picks up all six shortcut methods."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    # Generate shortcuts
    shortcuts = generateShortcuts([widget])
    
    # Should have 6 shortcuts (stepXPlus, stepXMinus, stepYPlus, stepYMinus, stepZPlus, stepZMinus)
    expected_keys = {
        'stepXPlus', 'stepXMinus',
        'stepYPlus', 'stepYMinus',
        'stepZPlus', 'stepZMinus'
    }
    
    actual_keys = set(shortcuts.keys())
    assert expected_keys.issubset(actual_keys), f"Missing shortcuts: {expected_keys - actual_keys}"
