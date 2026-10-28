"""A saved scan state that needs a feature a panel cannot run is refused there.

The SimplePointScan panel will save scans that the other scan panels can load
as ordinary analog/digital dicts, except for what those dicts cannot express,
such as N back-to-back T frames. Loading such a file into the Advanced panel
used to be possible only by quietly running one frame. A state now declares
``requiredFeatures`` (feature name -> what it needs), and a controller that
does not list the feature in ``supportedStateFeatures`` refuses to apply it,
with the reason, leaving its current parameters untouched.

The rule is additive: no file written before it carries the key, so every
existing state loads exactly as before (docs/simple-point-scan-plan.md, D4).
"""
import copy
from unittest.mock import Mock

import pytest

from imswitch.imcontrol.controller.basecontrollers import (
    ComponentStateApplyMode,
    SuperScanController,
)
from imswitch.imcontrol.controller.controllers.ScanControllerAdvanced import (
    ScanControllerAdvanced,
)

from .test_scan_component_state import ConcreteScanController

ANALOG = {
    'target_device': ['X', 'Y'],
    'axis_length': [100, 100],
    'axis_step_size': [1.0, 1.0],
    'axis_centerpos': [0.0, 0.0],
    'axis_startpos': [-50.0, -50.0],
    'scan_dim_target_device': ['X', 'Y'],
    'sequence_time': 0.1,
}
DIGITAL = {'target_device': ['Laser1'], 'advanced_mode': False}


def _controller(cls=ConcreteScanController):
    controller = cls.__new__(cls)
    controller.__dict__.update(
        _setupInfo=Mock(scan=Mock(scanWidgetType='Advanced')),
        _widget=Mock(),
        _logger=Mock(),
        _commChannel=Mock(),
        positioners={'X': Mock(), 'Y': Mock()},
        TTLDevices={'Laser1': Mock()},
        settingAttr=False,
        settingParameters=False,
        _analogParameterDict=copy.deepcopy(ANALOG),
        _digitalParameterDict=copy.deepcopy(DIGITAL),
        _positionersScan=['X', 'Y'],
        signalDict=None,
        scanInfoDict=None,
    )
    controller.isRunning = False
    controller.getParameters = Mock()
    controller.setParameters = Mock()
    controller.updateScanStageAttrs = Mock()
    controller.updateScanTTLAttrs = Mock()
    controller.runScan = Mock()
    controller.runScanAdvanced = Mock()
    return controller


def _state(**extra):
    analog = copy.deepcopy(ANALOG)
    analog['axis_length'] = [40, 30]
    return {
        'controller': 'ScanControllerSimplePointScan',
        'scanWidgetType': 'Advanced',
        'analogParameterDict': analog,
        'digitalParameterDict': copy.deepcopy(DIGITAL),
        'positionersScan': ['X', 'Y'],
        'mode': {},
        **extra,
    }


@pytest.mark.parametrize('applyMode', list(ComponentStateApplyMode))
def test_a_state_needing_an_unsupported_feature_is_refused_with_its_reason(applyMode):
    controller = _controller()
    state = _state(requiredFeatures={'tFrames': '10 T frames back to back'})

    warnings = controller.applyComponentState(state, applyMode=applyMode)

    assert warnings == [
        'This scan needs 10 T frames back to back, which the Advanced scan '
        'panel cannot run. Scan state was not applied.'
    ]
    # Nothing was applied: parameters, widget and scan all untouched.
    assert controller._analogParameterDict == ANALOG
    controller.setParameters.assert_not_called()
    controller.runScan.assert_not_called()
    controller.runScanAdvanced.assert_not_called()


def test_every_unsupported_feature_is_named():
    controller = _controller()
    state = _state(requiredFeatures={
        'tFrames': '10 T frames back to back',
        'somethingElse': 'another feature',
    })

    [warning] = controller.applyComponentState(
        state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )

    assert '10 T frames back to back; another feature' in warning


def test_a_controller_that_supports_the_feature_applies_the_state():
    class SupportingController(ConcreteScanController):
        supportedStateFeatures = frozenset({'tFrames'})

    controller = _controller(SupportingController)
    state = _state(requiredFeatures={'tFrames': '10 T frames back to back'})

    warnings = controller.applyComponentState(
        state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )

    assert warnings == []
    assert controller._analogParameterDict['axis_length'] == [40, 30]
    controller.setParameters.assert_called_once()


@pytest.mark.parametrize('required', [None, {}])
def test_a_state_without_required_features_loads_exactly_as_before(required):
    """Advanced as today: every file written before the key existed."""
    controller = _controller()
    state = _state()
    if required is not None:
        state['requiredFeatures'] = required

    warnings = controller.applyComponentState(
        state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )

    assert warnings == []
    assert controller._analogParameterDict['axis_length'] == [40, 30]
    controller.setParameters.assert_called_once()


def test_an_unreadable_declaration_is_refused_rather_than_ignored():
    controller = _controller()
    state = _state(requiredFeatures=['tFrames'])

    [warning] = controller.applyComponentState(
        state, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY
    )

    assert 'unreadable form' in warning
    controller.setParameters.assert_not_called()


def test_no_existing_scan_controller_supports_any_extra_feature():
    """The shipped panels run only the common dicts; only a panel that
    writes a feature should list it."""
    assert SuperScanController.supportedStateFeatures == frozenset()
    assert ScanControllerAdvanced.supportedStateFeatures == frozenset()


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
