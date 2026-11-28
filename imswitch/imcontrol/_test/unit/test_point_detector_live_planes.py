"""APD and PMT publish N-D scans and line-step scans while their planes fill.

Before: only a buffer that squeezed to 2-D was published line by line. A z
stack was first shown after its first plane, a 2-D scan with line steps only
once, at its end -- and napari then put the new slider on the middle plane,
still empty. Now every throttled line publishes, with the index of the plane
being written (``livePlaneIndex``), and the scan's empty frame is published
at build so the layer has the new shape at once.

The managers are driven without hardware: built with ``__new__`` and fed the
scan worker's line callbacks directly, the throttle set to publish every line.
"""
import logging
import threading

import numpy as np
import pytest
from qtpy import QtCore

from imswitch.imcontrol.model.managers.detectors.APDManager import APDManager
from imswitch.imcontrol.model.managers.detectors.PMTManager import PMTManager
from imswitch.imcontrol.model.managers.detectors._live_display import (
    LiveDisplayThrottle, display_from_raw,
)

pytestmark = pytest.mark.nohardware


def _make(cls, qapp):
    manager = cls.__new__(cls)
    QtCore.QObject.__init__(manager)             # enough for its signals
    prefix = f'_{cls.__name__}__'
    manager._name = cls.__name__
    manager._ttlmultiplying = False
    manager._trajectoryMode = False
    manager._warned_mock_count_clip = True
    manager._liveThrottle = LiveDisplayThrottle(0.0)
    setattr(manager, prefix + 'pixel_sizes', [0.1, 0.1])
    setattr(manager, prefix + 'newFrameReady', False)
    setattr(manager, prefix + 'logger', logging.getLogger('test'))
    manager._scanLifecycleLock = threading.Lock()
    manager._activeScanGeneration = 1
    manager._preparedScanGeneration = 1
    manager._scanTeardownOperation = None
    manager._completedScanGenerations = set()
    manager._image = np.zeros((1, 1), np.float32)
    manager._image_display = np.zeros((1, 1), np.float32)
    published = []
    manager.sigImageUpdated.connect(
        lambda im, init, scale: published.append(
            (np.array(im, copy=True), manager.livePlaneIndex))
    )
    # A boundary goes through the latest-frame broker; record it the same way.
    manager.updateLatestFrame = lambda init: published.append(
        (np.array(manager.getLatestFrame(False), copy=True), manager.livePlaneIndex))
    return manager, published


def _scan(manager, img_dims, linestep=1):
    """Feed one scan line by line, as ScanWorker does, frame boundaries included."""
    manager._linestep = linestep
    manager.initiateImage(tuple(img_dims) + ((linestep,) if linestep > 1 else ()))
    nx, ny = img_dims[0], img_dims[1]
    outer = img_dims[2:]
    seen = []
    for idx in (np.ndindex(*reversed(outer)) if outer else [()]):
        for y in range(ny * linestep):
            manager.updateImage(np.full(nx, 7.0), tuple(idx) + (y,))
            seen.append(tuple(idx))
        manager._onFrameBoundary(None)
    return seen


@pytest.fixture(params=[APDManager, PMTManager], ids=['APD', 'PMT'])
def manager(request, qapp):
    return _make(request.param, qapp)


def test_a_z_stack_is_published_line_by_line_with_the_plane_being_written(manager):
    detector, published = manager
    _scan(detector, (16, 8, 3))
    assert len(published) >= 3 * 8                        # every line, not every plane
    first, plane = published[0]
    assert first.shape == (3, 8, 16) and plane == (0,)
    assert first[0, 0].max() == 7 and first[1].max() == 0  # the first line is on screen
    planes = [plane for _, plane in published[:-1]]
    assert planes[:8] == [(0,)] * 8
    assert planes.index((1,)) == 9            # after plane 0's boundary frame
    assert planes.index((2,)) == 18


def test_a_two_d_scan_with_line_steps_is_published_before_its_end(manager):
    detector, published = manager
    _scan(detector, (16, 8), linestep=2)
    before_end = published[:-1]
    assert len(before_end) >= 8
    assert before_end[0][0].shape == (2, 8, 16)
    assert before_end[0][1] == (0,)            # line step 0 written first
    assert before_end[1][1] == (1,)            # then line step 1 of the same row


def test_a_time_series_of_z_stacks_carries_both_indices(manager):
    detector, published = manager
    _scan(detector, (8, 4, 2, 3))
    assert published[0][0].shape == (3, 2, 4, 8)
    assert published[0][1] == (0, 0)
    assert (1, 1) in [plane for _, plane in published]


def test_a_plain_two_d_scan_is_unchanged_and_has_no_plane(manager):
    detector, published = manager
    _scan(detector, (16, 8))
    assert all(im.shape == (8, 16) for im, _ in published)
    assert all(plane is None for _, plane in published[:-1])


def test_the_throttle_still_bounds_the_rate(manager):
    detector, published = manager
    detector._liveThrottle = LiveDisplayThrottle(3600.0)
    detector._liveThrottle.reset()
    _scan(detector, (16, 8, 4))
    # one throttled line (the first) plus one boundary per plane
    assert len(published) == 1 + 4


def test_the_empty_frame_is_published_at_build(manager):
    detector, published = manager
    detector._linestep = 1
    detector.initiateImage((16, 8, 5))
    detector._publishLivePreview((0,), force=True)
    (frame, plane), = published
    assert frame.shape == (5, 8, 16) and not frame.any() and plane == (0,)


@pytest.mark.parametrize('raw_shape, linestep, mode, write, display_ndim, shape, plane', [
    ((4, 8, 16), 1, None, (2,), 3, (4, 8, 16), (2,)),
    ((1, 8, 16), 1, None, (0,), 3, (8, 16), None),              # Nz = 1 squeezes away
    ((3, 4, 8, 16), 3, None, (1, 2), 4, (3, 4, 8, 16), (1, 2)),
    ((3, 4, 8, 16), 3, 'sum', (1, 2), 3, (4, 8, 16), (2,)),      # line steps reduced
    ((3, 8, 16), 3, 'max', (1,), 2, (8, 16), None),
    ((1, 16), 1, None, (), 2, (1, 16), None),                     # a single line
])
def test_display_from_raw_maps_the_write_index(raw_shape, linestep, mode, write,
                                               display_ndim, shape, plane):
    im, index = display_from_raw(np.zeros(raw_shape), linestep, mode, 0, display_ndim, write)
    assert im.shape == shape and index == plane
