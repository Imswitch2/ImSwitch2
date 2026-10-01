"""Single-axis scans through the APD/PMT consumer chain.

Phase C of docs/galvo-designer-single-axis-findings.md. The contract under
test (review-specified): a 1-axis scan normalizes to ONE line of N pixels —
logical loop dims ``[N, 1]``, spatial buffers ``(1, N)``, chunks
``(frames, 1, N)``, a two-dimensional display scale — and the pixels are
actually WRITTEN: the line-insert used to dispatch on the squeezed buffer
rank, so a ``(1, N)`` buffer (rank 1 squeezed) hit neither branch and every
pixel was silently dropped. Tests therefore assert pixel contents and final
shapes, not merely that initialization does not raise.
"""
import logging
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from imswitch.imcommon.framework import SignalInterface
from imswitch.imcontrol.model import DetectorInfo
from imswitch.imcontrol.model.managers.detectors.APDManager import (
    APDManager,
    ScanWorker as APDScanWorker,
)
from imswitch.imcontrol.model.managers.detectors.PMTManager import PMTManager
from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import (
    GalvoScanDesigner,
)
from imswitch.imcontrol.model.signaldesigners.PointScanTTLCycleDesigner import (
    PointScanTTLCycleDesigner,
)

from .test_galvo_signal_goldens import _params, _setup_sted_like


def _z_only_scan_info():
    """Real designer output for the stepped Z-piezo-only scan (20 px)."""
    setup = _setup_sted_like()
    setup.positioners["PiezoZ"].managerProperties["smoothScan"] = False
    params = _params(["PiezoZ", "GalvoX", "GalvoY"],
                     [10.0, 1.0, 1.0], [0.5, 1.0, 1.0],
                     centers=[5.0, 0.0, 0.0])
    _sig, _positions, info = GalvoScanDesigner().make_signal(params, setup)
    return info


def _mock_nidaq():
    nidaq = Mock()  # Mock attribute access makes isSimulated truthy
    return nidaq


def _apd():
    info = DetectorInfo(
        analogChannel=None, digitalLine=None, managerName='APDManager',
        managerProperties={'ctrInputLine': 0, 'terminal': '/Dev1/PFI0',
                           'deviceName': 'Dev1'},
        forAcquisition=True,
    )
    return APDManager(info, 'APD', _mock_nidaq())


def test_apd_z_only_scan_end_to_end_pixels():
    """Worker normalization + buffer allocation + line insert + frame
    boundary + chunk, all with real designer scanInfo and asserted pixels."""
    apd = _apd()
    scanInfo = _z_only_scan_info()
    assert scanInfo["img_dims"] == [20]  # designer stays truthful (1 axis)

    worker = APDScanWorker(apd, scanInfo, {'TTLCycleSignalsDict': {}})

    # review contract: logical loop dims [N, 1]; spatial buffer (1, N)
    assert worker._loop_dims == [20, 1]
    assert worker._img_dims == [20, 1]
    assert worker._output_image_dims == (20, 1)
    assert apd._image.shape == (1, 20)
    # 2-D display scale, singleton y padded with the fast step
    assert len(apd.scale) == 2
    assert apd.scale == [0.5, 0.5]

    # line insert: pixels must actually LAND (old squeeze-rank dispatch
    # wrote nothing for a (1, N) buffer)
    pixels = np.arange(20, dtype=np.float64)
    apd.updateImage(pixels, pos=(0,))
    np.testing.assert_array_equal(apd._image[0], np.arange(20))

    # frame boundary publishes a 2-D (1, N) display frame
    apd.updateLatestFrame = lambda init: None
    apd._onFrameBoundary()
    assert apd._image_display.shape == (1, 20)
    np.testing.assert_array_equal(apd._image_display[0], np.arange(20))

    # chunk contract: (frames, 1, N)
    chunk = apd.getChunk()
    assert chunk.shape == (1, 1, 20)
    np.testing.assert_array_equal(chunk[0, 0], np.arange(20))


def test_apd_multi_axis_scan_unaffected():
    """The same worker path for a normal XZ scan keeps its 2-D behavior."""
    apd = _apd()
    setup = _setup_sted_like()
    params = _params(["GalvoX", "PiezoZ", "GalvoY"],
                     [5.0, 10.0, 1.0], [0.1, 0.5, 1.0])
    _sig, _positions, info = GalvoScanDesigner().make_signal(params, setup)
    worker = APDScanWorker(apd, info, {'TTLCycleSignalsDict': {}})
    assert worker._loop_dims == [50, 20]
    assert apd._image.shape == (20, 50)
    assert apd.scale == [0.5, 0.1]


def _light_pmt():
    """PMTManager via the established light-construction test pattern."""
    pmt = PMTManager.__new__(PMTManager)
    SignalInterface.__init__(pmt)
    pmt._chunkConsumers = {}
    pmt._chunkConsumersWarned = set()
    pmt._chunkConsumersOverflowed = set()
    pmt._chunkConsumersLock = Lock()
    pmt._DetectorManager__logger = logging.getLogger('test.PMTManager')
    pmt._DetectorManager__image = np.array([])
    pmt._image = np.array([])
    pmt._image_display = np.array([])
    pmt._linestep = 1
    pmt._ttlmultiplying = False
    pmt.updateLatestFrame = lambda _init: None
    pmt._acceptScanWorkerGeneration = lambda _gen: True
    pmt._liveThrottle = SimpleNamespace(due=lambda: False, reset=lambda: None)
    return pmt


def test_pmt_single_line_buffers_and_pixels():
    """PMT initiateImage pads a bare (N,) to one line, the line insert
    writes, and the boundary publishes a 2-D (1, N) display frame."""
    pmt = _light_pmt()
    pmt.initiateImage((16,))
    assert pmt._image.shape == (1, 16)
    assert pmt._image_display.shape == (16, 1)  # logical alloc, replaced below

    pixels = np.linspace(0.0, 1.5, 16, dtype=np.float64)
    pmt.updateImage(pixels, pos=(0,))
    np.testing.assert_allclose(pmt._image[0], pixels.astype(np.float32))

    pmt._onFrameBoundary()
    assert pmt._image_display.shape == (1, 16)
    np.testing.assert_allclose(pmt._image_display[0],
                               pixels.astype(np.float32))

    chunk = pmt.getChunk()
    assert chunk.shape == (1, 1, 16)


def test_ttl_designer_accepts_single_axis_scan_info():
    """PointScanTTLCycleDesigner must build its cycle from a 1-axis
    scanInfo (scan_samples = [per_pixel, per_line], one line)."""
    scanInfo = _z_only_scan_info()
    ttlparams = {'target_device': ['640'],
                 'TTL_sequence': ['h1'],
                 'TTL_sequence_axis': ['None'],
                 'sequence_time': 2e-05}
    sig = PointScanTTLCycleDesigner().make_signal(
        ttlparams, _setup_sted_like(), scanInfo)
    assert '640' in sig
    assert len(sig['640']) == scanInfo['scan_samples_total']
