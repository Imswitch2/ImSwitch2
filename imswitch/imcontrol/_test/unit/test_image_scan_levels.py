"""A scan-driven detector is levelled once per run, on its first real frame,
from the filled pixels.

Real ImageController and ImageWidget (canvas-less napari model). Before: it
was never levelled (it publishes ``init=True``), so a fresh layer stayed at
contrast (0, 1).
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


# -- the first real frame is levelled --------------------------------------------

def test_a_scan_driven_detector_is_levelled_on_its_first_real_frame_only(rig):
    rig.controller._onScanRunStarting()
    _publish(rig, 'APDred', _stack(), (0,))                        # empty: nothing to level
    assert tuple(rig.widget.imgLayers['APDred'].contrast_limits) == (0, 1)
    first = _stack()
    first[0, :2] = np.arange(100, 164).reshape(2, 32)
    _publish(rig, 'APDred', first, (0,))
    low, high = rig.widget.imgLayers['APDred'].contrast_limits
    assert 100 <= low < 110 and 150 < high <= 163                  # the zeros ignored

    rig.widget.setImageDisplayLevels('APDred', 0, 1000)            # the user's levels
    _publish(rig, 'APDred', _stack(filled_planes=3, value=5000), (3,))
    assert tuple(rig.widget.imgLayers['APDred'].contrast_limits) == (0, 1000)


def test_each_run_levels_again_and_cameras_are_not_touched(rig):
    rig.controller._onScanRunStarting()
    _publish(rig, 'APDred', _stack(filled_planes=1, value=40), (1,))
    rig.controller._onScanRunEnded()
    rig.controller._onScanRunStarting()
    _publish(rig, 'APDred', _stack(filled_planes=1, value=400), (1,))
    assert rig.widget.imgLayers['APDred'].contrast_limits[1] >= 399
    rig.widget.setImage('Camera', np.zeros((32, 32)), (0.1, 0.1))
    rig.widget.setImageDisplayLevels('Camera', 0, 7)
    rig.controller.update('Camera', np.full((32, 32), 3000, np.uint16), True, [0.1, 0.1], False)
    assert tuple(rig.widget.imgLayers['Camera'].contrast_limits) == (0, 7)
