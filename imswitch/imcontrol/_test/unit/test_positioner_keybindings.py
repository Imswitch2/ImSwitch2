"""Tests for PositionerWidget step triggering (Phase 3c dynamic shortcuts)."""
import pytest
from imswitch.imcontrol.view.widgets import PositionerWidget

pytestmark = pytest.mark.nohardware


def test_step_axis_basic(qtbot):
    """Test that stepAxis method emits the correct signals for all axes and directions."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    # Add a single positioner with X, Y, Z axes
    widget.addPositioner('Stage', ['X', 'Y', 'Z'], speed=False, joystick=False)
    
    # Connect spies to both signals
    up_signals = []
    down_signals = []
    widget.sigStepUpClicked.connect(lambda positioner, axis: up_signals.append((positioner, axis)))
    widget.sigStepDownClicked.connect(lambda positioner, axis: down_signals.append((positioner, axis)))
    
    # Test X axis plus
    widget.stepAxis('Stage', 'X', 'plus')
    assert up_signals == [('Stage', 'X')]
    assert down_signals == []
    
    up_signals.clear()
    down_signals.clear()
    
    # Test X axis minus
    widget.stepAxis('Stage', 'X', 'minus')
    assert up_signals == []
    assert down_signals == [('Stage', 'X')]
    
    up_signals.clear()
    down_signals.clear()
    
    # Test Y axis plus
    widget.stepAxis('Stage', 'Y', 'plus')
    assert up_signals == [('Stage', 'Y')]
    assert down_signals == []
    
    up_signals.clear()
    down_signals.clear()
    
    # Test Y axis minus
    widget.stepAxis('Stage', 'Y', 'minus')
    assert up_signals == []
    assert down_signals == [('Stage', 'Y')]
    
    up_signals.clear()
    down_signals.clear()
    
    # Test Z axis plus
    widget.stepAxis('Stage', 'Z', 'plus')
    assert up_signals == [('Stage', 'Z')]
    assert down_signals == []
    
    up_signals.clear()
    down_signals.clear()
    
    # Test Z axis minus
    widget.stepAxis('Stage', 'Z', 'minus')
    assert up_signals == []
    assert down_signals == [('Stage', 'Z')]


def test_step_axis_preserves_case(qtbot):
    """Test that stepAxis preserves the original axis case from config."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    # Add positioner with lowercase axes (some configs may use this)
    widget.addPositioner('Stage', ['x', 'y', 'z'], speed=False, joystick=False)
    
    up_signals = []
    widget.sigStepUpClicked.connect(lambda positioner, axis: up_signals.append((positioner, axis)))
    
    # stepAxis should emit the axis name as given
    widget.stepAxis('Stage', 'x', 'plus')
    assert up_signals == [('Stage', 'x')]
    
    up_signals.clear()
    
    widget.stepAxis('Stage', 'y', 'plus')
    assert up_signals == [('Stage', 'y')]
    
    up_signals.clear()
    
    widget.stepAxis('Stage', 'z', 'plus')
    assert up_signals == [('Stage', 'z')]


def test_step_axis_multiple_positioners(qtbot):
    """Test that stepAxis correctly targets specific positioners when multiple exist."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    # Add two positioners with overlapping axes
    widget.addPositioner('StageA', ['X'], speed=False, joystick=False)
    widget.addPositioner('StageB', ['X', 'Z'], speed=False, joystick=False)
    
    up_signals = []
    widget.sigStepUpClicked.connect(lambda positioner, axis: up_signals.append((positioner, axis)))
    
    # With dynamic per-positioner actions, we can call stepAxis for either positioner
    widget.stepAxis('StageA', 'X', 'plus')
    assert up_signals == [('StageA', 'X')]
    
    up_signals.clear()
    
    widget.stepAxis('StageB', 'X', 'plus')
    assert up_signals == [('StageB', 'X')]
    
    up_signals.clear()
    
    # StageB also has Z
    widget.stepAxis('StageB', 'Z', 'plus')
    assert up_signals == [('StageB', 'Z')]
