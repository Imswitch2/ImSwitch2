"""Follow acquisition: during a scan the sliders follow the plane being
written, never against the user.

Real ImageController and ImageWidget (canvas-less napari model). Before: a new
z stack opened on napari's middle plane, empty until half the stack was done.
"""

import numpy as np
import pytest

from imswitch.imcontrol._test.unit._napari_model_viewer import imageControllerRig, stack as _stack

pytestmark = pytest.mark.nohardware


@pytest.fixture
def rig(qapp):
    rig = imageControllerRig()
    yield rig
    rig.widget.close()


def _publish(rig, *args, **kwargs):
    rig.publish(*args, **kwargs)


# -- following the plane being written -----------------------------------------

def test_a_new_stack_opens_on_the_plane_being_written_not_the_middle(rig):
    _publish(rig, 'APDred', np.ones((32, 32), np.uint16), None, (0.1, 0.1))
    rig.controller._onScanRunStarting()
    _publish(rig, 'APDred', _stack(), (0,))                      # the empty frame at build
    assert rig.viewer.dims.current_step[0] == 0
    _publish(rig, 'APDred', _stack(filled_planes=3), (3,))
    assert rig.viewer.dims.current_step[0] == 3


def test_moving_the_slider_during_a_scan_stops_following_until_the_next_run(rig):
    rig.controller._onScanRunStarting()
    _publish(rig, 'APDred', _stack(), (2,))
    rig.viewer.dims.set_current_step(0, 6)                       # the user looks elsewhere
    assert not rig.widget.followAcquisition
    assert not rig.widget.sumAxesWidget.followCheck.isChecked()
    _publish(rig, 'APDred', _stack(filled_planes=4), (4,))
    assert rig.viewer.dims.current_step[0] == 6                  # never fights the user

    rig.controller._onScanRunEnded()
    rig.controller._onScanRunStarting()
    assert rig.widget.followAcquisition
    _publish(rig, 'APDred', _stack(), (1,))
    assert rig.viewer.dims.current_step[0] == 1


def test_unticking_follow_by_hand_is_kept_across_runs(rig):
    rig.widget.sumAxesWidget.followCheck.setChecked(False)
    rig.controller._onScanRunStarting()
    _publish(rig, 'APDred', _stack(), (5,))
    assert not rig.widget.followAcquisition
    assert rig.viewer.dims.current_step[0] != 5
    rig.widget.sumAxesWidget.followCheck.setChecked(True)
    _publish(rig, 'APDred', _stack(), (5,))
    assert rig.viewer.dims.current_step[0] == 5


def test_napari_placing_sliders_for_a_new_layer_is_not_a_user_move(rig):
    rig.controller._onScanRunStarting()
    _publish(rig, 'APDred', _stack(), (2,))
    rig.widget.setImage('Camera', np.ones((4, 32, 32)), (1.0, 0.1, 0.1))   # another stack arrives
    rig.widget.setResultLayers('job', [(np.zeros((5, 32, 32)), {'name': 'recon'}, 'image')])
    assert rig.widget.followAcquisition


def test_following_a_summed_axis_is_skipped(rig):
    rig.detectors['APDred'].displayAxisNames = ('line steps', 'z', 'y', 'x')
    rig.controller._onScanRunStarting()
    rig.controller._onSumAxesChanged(('line steps',))
    frame = np.zeros((2, 6, 32, 32), np.uint16)
    _publish(rig, 'APDred', frame, (1, 4), (1.0, 0.5, 0.1, 0.1))
    assert rig.widget.imgLayers['APDred'].data.shape == (6, 32, 32)
    assert rig.viewer.dims.current_step[0] == 4
