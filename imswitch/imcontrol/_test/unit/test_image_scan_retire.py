"""A detector not taking part in a scan drops the stack an earlier scan left
in its live layer, so the viewer's slider goes with it.

Real ImageController and ImageWidget (canvas-less napari model). Before: a
detector left out of a 2-D scan after a 3-D one kept its 3-D layer, and the
slider stayed.
"""
import logging
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from imswitch.imcontrol._test.unit._napari_model_viewer import imageControllerRig, stack as _stack
from imswitch.imcontrol.model.managers._scan_execution import ScanExecutionCoordinator

pytestmark = pytest.mark.nohardware


@pytest.fixture
def rig(qapp):
    rig = imageControllerRig()
    yield rig
    rig.widget.close()


def _publish(rig, *args, **kwargs):
    rig.publish(*args, **kwargs)


# -- a detector left out of the scan ------------------------------------------

def test_a_detector_not_in_the_new_scan_drops_its_stack_and_the_slider(rig, caplog):
    _publish(rig, 'APDred', _stack(filled_planes=8), (7,))
    _publish(rig, 'APDgreen', _stack(filled_planes=8), (7,))
    rig.viewer.dims.set_current_step(0, 5)
    red = rig.widget.imgLayers['APDred']
    red.colormap, red.contrast_limits = 'magma', (3, 40)

    with caplog.at_level(logging.INFO):
        rig.controller._onNidaqScanBuilt({'participants': ['APDgreen']}, {}, [])
    _publish(rig, 'APDgreen', np.zeros((32, 32), np.uint16), None, (0.1, 0.1))

    red = rig.widget.imgLayers['APDred']
    assert red.data.shape == (32, 32)
    assert rig.viewer.dims.ndim == 2
    assert red.colormap.name == 'magma' and tuple(red.contrast_limits) == (3, 40)
    assert 'APDred is not in this scan' in caplog.text and '(8, 32, 32)' in caplog.text


def test_the_reduced_layer_keeps_the_plane_that_was_shown(rig):
    stack = np.arange(4)[:, None, None] * np.ones((4, 32, 32), np.uint16)
    _publish(rig, 'APDred', stack, (3,))
    rig.viewer.dims.set_current_step(0, 2)
    rig.controller._onNidaqScanBuilt({'participants': ['Camera']}, {}, [])
    assert np.all(rig.widget.imgLayers['APDred'].data == 2)


def test_a_reduced_stack_does_not_come_back_through_a_sum_box(rig):
    rig.detectors['APDred'].displayAxisNames = ('z', 'y', 'x')
    _publish(rig, 'APDred', _stack(filled_planes=8), (7,))
    rig.controller._onNidaqScanBuilt({'participants': ['APDgreen']}, {}, [])
    rig.controller._onSumAxesChanged(('z',))
    assert rig.widget.imgLayers['APDred'].data.ndim == 2


def test_participants_come_from_the_excluded_list_or_the_coordinator(rig):
    _publish(rig, 'APDred', _stack(filled_planes=8), (7,))
    rig.controller._onNidaqScanBuilt({'excludedDetectors': ['APDred']}, {}, [])
    assert rig.widget.imgLayers['APDred'].data.ndim == 2

    _publish(rig, 'APDgreen', _stack(filled_planes=8), (7,))
    coordinator = ScanExecutionCoordinator.__new__(ScanExecutionCoordinator)
    rig.master.scanExecutionCoordinator = coordinator
    with patch.object(ScanExecutionCoordinator, 'activeToken',
                      new=SimpleNamespace(participants=('APDred',))):
        rig.controller._onScanIterationStarted()            # the TriggerScope path
    assert rig.widget.imgLayers['APDgreen'].data.ndim == 2


def test_a_participant_and_a_two_d_layer_are_left_alone(rig):
    _publish(rig, 'APDred', _stack(filled_planes=8), (7,))
    rig.widget.setImage('Camera', np.ones((32, 32)), (0.1, 0.1))
    rig.controller._onNidaqScanBuilt({'participants': ['APDred']}, {}, [])
    assert rig.widget.imgLayers['APDred'].data.shape == (8, 32, 32)
    rig.controller._onNidaqScanBuilt({'engine': 'step'}, {}, [])   # no participants: nothing
    assert rig.widget.imgLayers['APDred'].data.shape == (8, 32, 32)
