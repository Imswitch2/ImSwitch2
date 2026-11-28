"""A z stack from the APD manager through the image controller into the
viewer, end to end without hardware: the layer has the stack's shape before
the first pixel, shows the plane being written while it fills, follows it,
and is levelled from the first filled line.

The APD is driven as in test_point_detector_live_planes; the viewer is the
real ImageWidget on a canvas-less napari model.
"""
import numpy as np
import pytest

from imswitch.imcontrol._test.unit._napari_model_viewer import imageControllerRig
from imswitch.imcontrol._test.unit.test_point_detector_live_planes import _make
from imswitch.imcontrol.model.managers.detectors.APDManager import APDManager

pytestmark = pytest.mark.nohardware


def test_a_z_stack_is_visible_from_its_first_line_on_the_plane_being_written(qapp):
    rig = imageControllerRig()
    apd, _published = _make(APDManager, qapp)
    rig.detectors['APDred'] = apd
    apd.sigImageUpdated.connect(
        lambda im, init, scale: rig.controller.update('APDred', im, init, scale, True))
    rig.widget.setImage('APDred', np.ones((16, 16), np.uint16), (0.1, 0.1))   # a 2-D scan before
    rig.controller._onScanRunStarting()

    apd._linestep = 1
    apd.initiateImage((16, 8, 5))
    apd._publishLivePreview((0, ), force=True)                 # what initiateScan does at build
    layer = rig.widget.imgLayers['APDred']
    assert layer.data.shape == (5, 8, 16)
    assert rig.viewer.dims.current_step[0] == 0                # not napari's middle plane

    for z in range(3):
        for y in range(8):
            apd.updateImage(np.full(16, 100.0 + z), (z, y))
            if z == 2 and y == 0:
                layer = rig.widget.imgLayers['APDred']
                assert rig.viewer.dims.current_step[0] == 2    # following plane 2
                assert np.asarray(layer.data)[2, 0].max() == 102
        apd._onFrameBoundary(None)
    low, high = rig.widget.imgLayers['APDred'].contrast_limits
    assert 95 <= low <= 100 and 100 <= high <= 110             # levelled on line 1, then kept
    rig.widget.close()
