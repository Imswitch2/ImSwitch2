"""Point-scan managers publish each frame with its own scan's geometry (D1).

For a scan whose ``scanInfoDict`` carries a frame geometry, the APD, PMT and
Time Tagger managers publish display frames as ``ScanFrame`` views, from every
place they publish: the throttled mid-scan preview, the latest frame, and the
chunk drained for a recording (re-attached by the display latch). Each such
scan gets fresh display storage, so a frame published for scan k keeps its
pixels when scan k+1 starts, even if k+1's build then fails.

A scan without a geometry (every scan the existing panels run) takes exactly
today's path: same buffer reuse, the very same array objects published, no
geometry anywhere. Those tests are the "Advanced as today" half.
"""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from imswitch.imcontrol.model import DetectorInfo
from imswitch.imcontrol.model.managers.detectors import _frame_publication as publication
from imswitch.imcontrol.model.managers.detectors.APDManager import APDManager
from imswitch.imcontrol.model.managers.detectors.PMTManager import PMTManager
from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import (
    SwabianTimeTaggerManager,
)
from imswitch.imcontrol.model.scan_frame import (
    AxisGeometry,
    FRAME_GEOMETRY_KEY,
    FrameGeometry,
    ScanFrame,
    frame_geometry_of,
)

DIMS = (4, 3)  # (Nx, Ny) as the ScanWorker hands them to initiateImage


def _geometry(iteration, first=0.0):
    return FrameGeometry(
        axes=(AxisGeometry('X', 0.5, 4, first), AxisGeometry('Y', 0.5, 3, first)),
        run=1, iteration=iteration,
    )


def _scanInfo(geometry):
    return {FRAME_GEOMETRY_KEY: geometry.to_dict()} if geometry else {}


def _nidaq():
    nidaq = Mock()
    for name in ('sigScanBuilt', 'sigScanStarted', 'sigScanDone'):
        getattr(nidaq, name).connect = Mock()
    return nidaq


def _apd():
    info = DetectorInfo(
        analogChannel=None, digitalLine=None, managerName='APDManager',
        managerProperties={'ctrInputLine': 0, 'terminal': '/Dev1/PFI0',
                           'deviceName': 'Dev1'},
        forAcquisition=True,
    )
    return APDManager(info, 'APD', _nidaq())


def _pmt():
    info = DetectorInfo(
        analogChannel=None, digitalLine=None, managerName='PMTManager',
        managerProperties={'analogInputLine': 0, 'deviceName': 'Dev1'},
        forAcquisition=True,
    )
    return PMTManager(info, 'PMT', _nidaq())


MANAGERS = {'APD': _apd, 'PMT': _pmt}


def _newFrameFlag(manager):
    return f'_{type(manager).__name__}__newFrameReady'


def _prepare(manager, geometry):
    """What initiateScan + the ScanWorker do to the display storage."""
    publication.remember_scan_geometry(manager, _scanInfo(geometry))
    manager.initiateImage(DIMS)


def _writeLine(manager, value):
    manager._image[..., 0, :] = value


@pytest.fixture(params=sorted(MANAGERS))
def manager(request, qtbot):
    return MANAGERS[request.param]()


# ---------------------------------------------------------------------------
# Advanced as today: no geometry, nothing changes
# ---------------------------------------------------------------------------

def test_without_geometry_a_same_shape_buffer_is_reused(manager):
    _prepare(manager, None)
    first = manager._image
    _prepare(manager, None)
    assert manager._image is first


def test_without_geometry_the_same_objects_are_published(manager, qtbot):
    _prepare(manager, None)
    assert manager.getLatestFrame(is_save=False) is manager._image_display
    assert manager.getLatestFrame(is_save=True) is manager._image

    received = []
    manager.sigImageUpdated.connect(lambda im, init, scale: received.append(im))
    manager._liveThrottle.reset()
    manager.updateImage(np.ones(4), (0,))
    assert received and received[0] is manager._image

    manager.__dict__[_newFrameFlag(manager)] = True
    payload = manager.drainChunk()
    assert payload.display_geometry is None
    assert not isinstance(payload.display, ScanFrame)


# ---------------------------------------------------------------------------
# With a geometry
# ---------------------------------------------------------------------------

def test_every_display_publication_carries_the_geometry(manager, qtbot):
    geometry = _geometry(1)
    _prepare(manager, geometry)

    latest = manager.getLatestFrame(is_save=False)
    assert frame_geometry_of(latest) == geometry
    assert np.shares_memory(latest, manager._image_display)

    received = []
    manager.sigImageUpdated.connect(lambda im, init, scale: received.append(im))
    manager._liveThrottle.reset()
    manager.updateImage(np.ones(4), (0,))
    assert frame_geometry_of(received[0]) == geometry
    assert np.shares_memory(received[0], manager._image)

    manager.__dict__[_newFrameFlag(manager)] = True
    assert manager.drainChunk().display_geometry == geometry


def test_saved_frames_are_plain_arrays(manager):
    """Recording writers get what they always got."""
    _prepare(manager, _geometry(1))
    saved = manager.getLatestFrame(is_save=True)
    assert saved is manager._image
    assert not isinstance(saved, ScanFrame)


def test_the_display_latch_keeps_the_geometry(manager):
    """While a recording consumes chunks, the display frame comes from the
    latch, which used to reduce it to a plain array."""
    geometry = _geometry(1)
    _prepare(manager, geometry)
    manager.startChunkConsumer('recording')
    manager.__dict__[_newFrameFlag(manager)] = True

    shown = manager.getLatestFrameShared(is_save=False)

    assert frame_geometry_of(shown) == geometry


def test_a_retained_frame_keeps_its_pixels_when_the_next_scan_starts(manager):
    """Plan D1, review 2 point 3: with an identity display transform napari's
    layer.data is the published buffer; the next scan must not rewrite it."""
    first = _geometry(1, first=0.0)
    _prepare(manager, first)
    _writeLine(manager, 7)
    retained = manager.getLatestFrame(is_save=True).copy()
    shown = manager.getLatestFrame(is_save=False)
    shownBefore = np.array(shown, copy=True)
    publishedRaw = manager._image

    second = _geometry(2, first=10.0)
    _prepare(manager, second)            # same shape: fresh storage anyway
    _writeLine(manager, 3)

    assert manager._image is not publishedRaw
    np.testing.assert_array_equal(publishedRaw, retained)
    np.testing.assert_array_equal(shown, shownBefore)
    assert frame_geometry_of(shown) == first
    assert frame_geometry_of(manager.getLatestFrame(is_save=False)) == second


def test_a_scan_that_never_gets_storage_changes_nothing(manager):
    """A build that fails or is refused before its detectors allocate leaves
    the shown frame and its geometry exactly as they were."""
    first = _geometry(1)
    _prepare(manager, first)
    shown = manager.getLatestFrame(is_save=False)

    publication.remember_scan_geometry(manager, _scanInfo(_geometry(2, first=10.0)))

    assert frame_geometry_of(manager.getLatestFrame(is_save=False)) == first
    assert frame_geometry_of(shown) == first


def test_a_frame_read_off_the_manager_thread_is_a_snapshot(manager):
    """The live-view worker's queued path: its frame may be applied after
    more of the same scan was written, so it gets its own pixels."""
    geometry = _geometry(1)
    _prepare(manager, geometry)
    result = {}

    def read():
        result['frame'] = manager.getLatestFrame(is_save=False)

    worker = threading.Thread(target=read)
    worker.start()
    worker.join()

    frame = result['frame']
    assert frame_geometry_of(frame) == geometry
    assert not np.shares_memory(frame, manager._image_display)


# ---------------------------------------------------------------------------
# Time Tagger: fresh storage every scan already; publication points only
# ---------------------------------------------------------------------------

def _timeTagger(geometry):
    tagger = SwabianTimeTaggerManager.__new__(SwabianTimeTaggerManager)
    tagger.__dict__.update(
        _newFrameReady=True, _rawReady=False, _rawDelivered=True,
        _image_raw=None,
        # What its __del__ needs when the double is collected (as in
        # test_detector_stop_contract's Time Tagger double).
        _logger=Mock(), acquisition=False, _teardownScanThread=lambda: None,
    )
    publication.begin_frame_storage(tagger)
    tagger._image_display = np.zeros((1, 3, 4), dtype=np.float32)
    publication.commit_frame_storage(tagger, geometry)
    return tagger


def test_the_time_tagger_publishes_with_its_geometry():
    geometry = _geometry(1)
    tagger = _timeTagger(geometry)
    assert frame_geometry_of(tagger.getLatestFrame()) == geometry
    assert tagger.drainChunk().display_geometry == geometry


def test_the_time_tagger_without_geometry_publishes_as_today():
    tagger = _timeTagger(None)
    assert tagger.getLatestFrame() is tagger._image_display
    assert tagger.drainChunk().display_geometry is None


# ---------------------------------------------------------------------------
# The pairing itself
# ---------------------------------------------------------------------------

def test_a_read_that_straddles_a_storage_swap_gets_no_geometry():
    holder = SimpleNamespace()
    publication.commit_frame_storage(holder, _geometry(1))

    def readDuringSwap():
        publication.begin_frame_storage(holder)
        publication.commit_frame_storage(holder, _geometry(2))
        return 'pixels'

    value, geometry = publication.read_with_frame_geometry(holder, readDuringSwap)
    assert value == 'pixels'
    assert geometry is None

    value, geometry = publication.read_with_frame_geometry(holder, lambda: 'pixels')
    assert geometry == _geometry(2)


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
