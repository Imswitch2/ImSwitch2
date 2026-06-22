"""Tests for Phase 3c positioner shortcutModifier alias expansion and per-positioner dynamic actions."""
import pytest
from dataclasses import dataclass
from typing import List, Optional
from unittest.mock import Mock

from qtpy import QtWidgets

from imswitch.imcontrol.controller.ShortcutManager import (
    ShortcutManager, computePositionerJogDefaults,
)
from imswitch.imcontrol.view.widgets import PositionerWidget
from imswitch.imcommon.model import ShortcutScope

pytestmark = pytest.mark.nohardware


@dataclass
class MockPositionerInfo:
    """Mock PositionerInfo for testing."""
    axes: List[str]
    shortcutModifier: Optional[str] = None


@dataclass
class MockSetupInfo:
    """Mock SetupInfo for testing."""
    positioners: dict
    shortcuts: Optional[dict] = None


def _registerPositionerJogActionsForTest(shortcutManager, setupInfo, positionerWidget):
    """Register jog actions exactly as production does, using the SHARED
    production computePositionerJogDefaults (no reimplementation), so these tests
    exercise real default-key logic rather than a copy."""
    if not getattr(setupInfo, 'positioners', None):
        return

    jogDefaults = computePositionerJogDefaults(setupInfo.positioners)
    for positionerName, positionerInfo in setupInfo.positioners.items():
        for axis in positionerInfo.axes:
            for direction, label in (('plus', '+'), ('minus', '-')):
                actionId = f'positioner.{positionerName}.{axis}.{direction}'
                defaultKey = jogDefaults.get(actionId)
                shortcutManager.registerAction(
                    actionId=actionId,
                    displayName=f'{positionerName} {axis} {label}',
                    callback=(lambda *_, pName=positionerName, ax=axis, d=direction:
                              positionerWidget.stepAxis(pName, ax, d)),
                    defaultKeySequence=defaultKey,
                    scope=ShortcutScope.Application,
                    owner=positionerWidget,
                    initiallyBound=(defaultKey is not None),
                )


def test_shortcut_modifier_ctrl_expansion(qtbot):
    """Test that shortcutModifier='ctrl' expands to Ctrl+Arrow/Y/A keys."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    setupInfo = MockSetupInfo(
        positioners={
            'Stage': MockPositionerInfo(axes=['X', 'Y', 'Z'], shortcutModifier='ctrl')
        }
    )
    
    manager = ShortcutManager()
    _registerPositionerJogActionsForTest(manager, setupInfo, widget)
    
    # Check that the correct default keys were assigned
    catalog = manager.getAllActions()
    
    assert catalog['positioner.Stage.X.plus'].defaultKeySequence == 'Ctrl+Right'
    assert catalog['positioner.Stage.X.minus'].defaultKeySequence == 'Ctrl+Left'
    assert catalog['positioner.Stage.Y.plus'].defaultKeySequence == 'Ctrl+Up'
    assert catalog['positioner.Stage.Y.minus'].defaultKeySequence == 'Ctrl+Down'
    assert catalog['positioner.Stage.Z.plus'].defaultKeySequence == 'Ctrl+Y'
    assert catalog['positioner.Stage.Z.minus'].defaultKeySequence == 'Ctrl+A'


def test_shortcut_modifier_ctrl_shift_expansion(qtbot):
    """Test that shortcutModifier='ctrl-shift' expands to Ctrl+Shift+Arrow/Y/A keys."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    setupInfo = MockSetupInfo(
        positioners={
            'Piezo': MockPositionerInfo(axes=['X', 'Y', 'Z'], shortcutModifier='ctrl-shift')
        }
    )
    
    manager = ShortcutManager()
    _registerPositionerJogActionsForTest(manager, setupInfo, widget)
    
    catalog = manager.getAllActions()
    
    assert catalog['positioner.Piezo.X.plus'].defaultKeySequence == 'Ctrl+Shift+Right'
    assert catalog['positioner.Piezo.X.minus'].defaultKeySequence == 'Ctrl+Shift+Left'
    assert catalog['positioner.Piezo.Y.plus'].defaultKeySequence == 'Ctrl+Shift+Up'
    assert catalog['positioner.Piezo.Y.minus'].defaultKeySequence == 'Ctrl+Shift+Down'
    assert catalog['positioner.Piezo.Z.plus'].defaultKeySequence == 'Ctrl+Shift+Y'
    assert catalog['positioner.Piezo.Z.minus'].defaultKeySequence == 'Ctrl+Shift+A'


def test_legacy_first_come_single_positioner(qtbot):
    """Test that a single positioner with no shortcutModifier gets Ctrl+Arrow keys (legacy)."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    setupInfo = MockSetupInfo(
        positioners={
            'Stage': MockPositionerInfo(axes=['X', 'Y', 'Z'], shortcutModifier=None)
        }
    )
    
    manager = ShortcutManager()
    _registerPositionerJogActionsForTest(manager, setupInfo, widget)
    
    catalog = manager.getAllActions()
    
    # First (and only) positioner with no modifier gets the Ctrl+Arrow set
    assert catalog['positioner.Stage.X.plus'].defaultKeySequence == 'Ctrl+Right'
    assert catalog['positioner.Stage.X.minus'].defaultKeySequence == 'Ctrl+Left'
    assert catalog['positioner.Stage.Y.plus'].defaultKeySequence == 'Ctrl+Up'
    assert catalog['positioner.Stage.Y.minus'].defaultKeySequence == 'Ctrl+Down'
    assert catalog['positioner.Stage.Z.plus'].defaultKeySequence == 'Ctrl+Y'
    assert catalog['positioner.Stage.Z.minus'].defaultKeySequence == 'Ctrl+A'


def test_legacy_first_come_multiple_positioners(qtbot):
    """Test that only the FIRST positioner with no shortcutModifier gets Ctrl keys per axis."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    # Python 3.7+ dicts preserve insertion order
    setupInfo = MockSetupInfo(
        positioners={
            'StageA': MockPositionerInfo(axes=['X', 'Y'], shortcutModifier=None),
            'StageB': MockPositionerInfo(axes=['X', 'Z'], shortcutModifier=None),
        }
    )
    
    manager = ShortcutManager()
    _registerPositionerJogActionsForTest(manager, setupInfo, widget)
    
    catalog = manager.getAllActions()
    
    # StageA is first, so it gets X and Y
    assert catalog['positioner.StageA.X.plus'].defaultKeySequence == 'Ctrl+Right'
    assert catalog['positioner.StageA.Y.plus'].defaultKeySequence == 'Ctrl+Up'
    
    # StageB is second, so it doesn't get X (already claimed), but gets Z (new axis)
    assert catalog['positioner.StageB.X.plus'].defaultKeySequence is None
    assert catalog['positioner.StageB.Z.plus'].defaultKeySequence == 'Ctrl+Y'


def test_explicit_ctrl_overrides_legacy(qtbot):
    """Test that explicit 'ctrl' positioner overrides legacy first-come claim."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    # Galvo is first with no modifier, BSC203 is second with explicit ctrl
    setupInfo = MockSetupInfo(
        positioners={
            'Galvo': MockPositionerInfo(axes=['X'], shortcutModifier=None),
            'BSC203': MockPositionerInfo(axes=['X', 'Y', 'Z'], shortcutModifier='ctrl'),
        }
    )
    
    manager = ShortcutManager()
    _registerPositionerJogActionsForTest(manager, setupInfo, widget)
    
    catalog = manager.getAllActions()
    
    # Galvo (first, no modifier) gets X since it's first
    assert catalog['positioner.Galvo.X.plus'].defaultKeySequence == 'Ctrl+Right'
    
    # BSC203 (explicit ctrl) also gets Ctrl keys for all its axes
    assert catalog['positioner.BSC203.X.plus'].defaultKeySequence == 'Ctrl+Right'
    assert catalog['positioner.BSC203.Y.plus'].defaultKeySequence == 'Ctrl+Up'
    assert catalog['positioner.BSC203.Z.plus'].defaultKeySequence == 'Ctrl+Y'
    
    # NOTE: This will create a CONFLICT that the ShortcutManager will resolve
    # The conflict resolution is tested separately


def test_config_override_replaces_alias(qtbot):
    """Test that explicit shortcuts config overrides shortcutModifier alias."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    setupInfo = MockSetupInfo(
        positioners={
            'Stage': MockPositionerInfo(axes=['X', 'Y'], shortcutModifier='ctrl')
        },
        shortcuts={
            'positioner.Stage.X.plus': 'F1',  # Override the default Ctrl+Right
            'positioner.Stage.X.minus': None,  # Explicitly disable
        }
    )
    
    manager = ShortcutManager()
    _registerPositionerJogActionsForTest(manager, setupInfo, widget)
    manager.loadConfigOverrides(setupInfo.shortcuts)
    manager.computeEffectiveBindings()
    
    effective = manager.getEffectiveBindings()
    
    # X.plus overridden to F1
    assert effective['positioner.Stage.X.plus'] == 'F1'
    
    # X.minus explicitly disabled (not in effective bindings)
    assert 'positioner.Stage.X.minus' not in effective
    
    # Y.plus keeps the alias default
    assert effective['positioner.Stage.Y.plus'] == 'Ctrl+Up'


def test_conflict_resolution(qtbot):
    """Test that ShortcutManager resolves conflicts when multiple positioners want same key."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    # Both positioners want Ctrl+Right
    setupInfo = MockSetupInfo(
        positioners={
            'StageA': MockPositionerInfo(axes=['X'], shortcutModifier='ctrl'),
            'StageB': MockPositionerInfo(axes=['X'], shortcutModifier='ctrl'),
        }
    )
    
    manager = ShortcutManager()
    _registerPositionerJogActionsForTest(manager, setupInfo, widget)
    manager.computeEffectiveBindings()
    
    effective = manager.getEffectiveBindings()
    
    # Only one should win (first registered)
    if 'positioner.StageA.X.plus' in effective:
        assert effective['positioner.StageA.X.plus'] == 'Ctrl+Right'
        assert 'positioner.StageB.X.plus' not in effective
    else:
        assert 'positioner.StageB.X.plus' in effective
        assert effective['positioner.StageB.X.plus'] == 'Ctrl+Right'
        assert 'positioner.StageA.X.plus' not in effective
    
    # Check that a warning was issued
    warnings = manager.getConflictWarnings()
    assert len(warnings) > 0
    assert 'Ctrl+Right' in warnings[0]


def test_mixed_modifiers(qtbot):
    """Test multiple positioners with different shortcutModifiers coexist."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    setupInfo = MockSetupInfo(
        positioners={
            'Piezo': MockPositionerInfo(axes=['X'], shortcutModifier='ctrl-shift'),
            'Stage': MockPositionerInfo(axes=['Y'], shortcutModifier='ctrl'),
            'Galvo': MockPositionerInfo(axes=['Z'], shortcutModifier=None),
        }
    )
    
    manager = ShortcutManager()
    _registerPositionerJogActionsForTest(manager, setupInfo, widget)
    manager.computeEffectiveBindings()
    
    effective = manager.getEffectiveBindings()
    
    # Each positioner gets its own keys with no conflicts
    assert effective['positioner.Piezo.X.plus'] == 'Ctrl+Shift+Right'
    assert effective['positioner.Stage.Y.plus'] == 'Ctrl+Up'
    assert effective['positioner.Galvo.Z.plus'] == 'Ctrl+Y'


def test_unknown_axis_no_default(qtbot):
    """Test that axes not in the standard X/Y/Z set get no default binding."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    setupInfo = MockSetupInfo(
        positioners={
            'Custom': MockPositionerInfo(axes=['A', 'B'], shortcutModifier='ctrl')
        }
    )
    
    manager = ShortcutManager()
    _registerPositionerJogActionsForTest(manager, setupInfo, widget)
    
    catalog = manager.getAllActions()
    
    # Custom axes with no key mapping get None as default
    assert catalog['positioner.Custom.A.plus'].defaultKeySequence is None
    assert catalog['positioner.Custom.B.plus'].defaultKeySequence is None
    
    # They're registered but not initially bound
    assert catalog['positioner.Custom.A.plus'].initiallyBound is False


def test_case_insensitive_modifier(qtbot):
    """Test that shortcutModifier is case-insensitive and handles various formats."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    
    # Test various valid formats
    setupInfo = MockSetupInfo(
        positioners={
            'Stage1': MockPositionerInfo(axes=['X'], shortcutModifier='CTRL'),
            'Stage2': MockPositionerInfo(axes=['Y'], shortcutModifier='Ctrl+Shift'),
            'Stage3': MockPositionerInfo(axes=['Z'], shortcutModifier='shift-ctrl'),
        }
    )
    
    manager = ShortcutManager()
    _registerPositionerJogActionsForTest(manager, setupInfo, widget)
    
    catalog = manager.getAllActions()
    
    assert catalog['positioner.Stage1.X.plus'].defaultKeySequence == 'Ctrl+Right'
    assert catalog['positioner.Stage2.Y.plus'].defaultKeySequence == 'Ctrl+Shift+Up'
    assert catalog['positioner.Stage3.Z.plus'].defaultKeySequence == 'Ctrl+Shift+Y'


def test_positioner_qaction_trigger_uses_registered_axis(qtbot):
    """Regression: QAction's checked bool must not replace the positioner name."""
    widget = PositionerWidget({})
    qtbot.addWidget(widget)
    setupInfo = MockSetupInfo(
        positioners={
            'Stage': MockPositionerInfo(axes=['X'], shortcutModifier='ctrl')
        }
    )
    manager = ShortcutManager()
    _registerPositionerJogActionsForTest(manager, setupInfo, widget)
    manager.computeEffectiveBindings()

    mainWindow = QtWidgets.QMainWindow()
    qtbot.addWidget(mainWindow)
    shortcutsMenu = mainWindow.menuBar().addMenu('&Shortcuts')
    stepUpSignals = []
    widget.sigStepUpClicked.connect(
        lambda positionerName, axis: stepUpSignals.append((positionerName, axis))
    )

    manager.build(shortcutsMenu, mainWindow)
    qtAction = next(
        action
        for action in shortcutsMenu.actions()
        if action.text() == 'Stage X +'
    )
    qtAction.trigger()

    assert stepUpSignals == [('Stage', 'X')]
