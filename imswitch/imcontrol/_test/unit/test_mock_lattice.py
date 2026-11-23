"""The MoNaLISA mock camera: a foci lattice over the synthetic sample.

Each frame of a mock Hamamatsu with a ``mockSample`` shows the sample's
brightness under fixed foci, the sample moved by the stage position the scan
waveforms give at that frame's camera trigger. A scan over one lattice period
then holds the whole sample, which the pinhole assembly below shows.
"""
import json
import time

import numpy as np
import pytest
from qtpy import QtCore

from imswitch.imcontrol.model import DetectorInfo, SetupInfo
from imswitch.imcontrol.model.interfaces.hamamatsu_mock import MockHamamatsu
from imswitch.imcontrol.model.managers.NidaqManager import NidaqManager
from imswitch.imcontrol.model.managers.detectors.HamamatsuManager import HamamatsuManager
from imswitch.imcontrol.model.managers.detectors._mock_lattice import (
    MockLattice,
    MockLatticeFrames,
    trigger_positions_um,
)

SETUP = 'imswitch/_data/user_defaults/imcontrol_setups/simple_monalisa_mock_scan_setup.json'
PROPERTY = {'axes': {'X': 5.0, 'Y': 5.0}, 'latticePeriodUm': 0.72}
SHAPE = (120, 120)
PIXEL_UM = 0.1


def _qapp():
    return QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])


def _wait_for(predicate, timeout=2.0):
    app = _qapp()
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def _lattice(**overrides):
    return MockLattice.from_property({**PROPERTY, **overrides})


def _scan_signals(n_steps, step_um, um_per_volt=5.0, samples_per_position=4):
    """Raster of n_steps x n_steps positions, one camera pulse per position."""
    ys, xs = np.meshgrid(np.arange(n_steps), np.arange(n_steps), indexing='ij')
    x = np.repeat(xs.ravel() * step_um / um_per_volt, samples_per_position)
    y = np.repeat(ys.ravel() * step_um / um_per_volt, samples_per_position)
    ttl = np.tile([0, 1, 1, 0][:samples_per_position], n_steps * n_steps).astype(bool)
    return {'scanSignalsDict': {'X': x, 'Y': y}, 'TTLCycleSignalsDict': {'Camera': ttl}}


def test_property_is_optional_and_validated():
    assert MockLattice.from_property(None) is None
    lattice = _lattice()
    assert lattice.period_um == pytest.approx(0.72)
    assert lattice.sample.axes == (('X', 5.0), ('Y', 5.0))
    with pytest.raises(ValueError):
        _lattice(latticePeriodUm=0)
    with pytest.raises(ValueError):
        MockLattice.from_property({'latticePeriodUm': 0.72})


def test_frame_is_a_lattice_with_the_configured_period():
    lattice = _lattice(peakCounts=1000.0, offsetCounts=0.0)
    lattice.sample.texture()
    frame = lattice.expected_counts(SHAPE, PIXEL_UM)

    fx, fy = lattice.foci_um(SHAPE, PIXEL_UM)
    expected = (SHAPE[0] * PIXEL_UM / 0.72) * (SHAPE[1] * PIXEL_UM / 0.72)
    assert fx.size == pytest.approx(expected, rel=0.1)
    # Neighbouring foci one period apart along x.
    xs = np.unique(np.round(fx, 6))
    assert np.diff(xs) == pytest.approx(0.72)

    # The frame's spectrum peaks at the lattice frequency.
    profile = frame.sum(axis=0) - frame.sum(axis=0).mean()
    spectrum = np.abs(np.fft.rfft(profile))
    frequency = np.fft.rfftfreq(profile.size, d=PIXEL_UM)
    assert frequency[np.argmax(spectrum[1:]) + 1] == pytest.approx(1 / 0.72, rel=0.05)


def test_focus_brightness_follows_the_stage():
    lattice = _lattice(offsetCounts=0.0)
    fx, fy = lattice.foci_um(SHAPE, PIXEL_UM)
    stage = (0.31, -0.17)
    frame = lattice.expected_counts(SHAPE, PIXEL_UM, stage)

    cx = fx / PIXEL_UM + 0.5 * (SHAPE[1] - 1)
    cy = fy / PIXEL_UM + 0.5 * (SHAPE[0] - 1)
    col, row = np.rint(cx).astype(int), np.rint(cy).astype(int)
    # The focus centre is off the pixel centre: divide out the spot there.
    sigma_px = lattice.spot_sigma_um / PIXEL_UM
    spot = np.exp(-0.5 * ((col - cx) ** 2 + (row - cy) ** 2) / sigma_px ** 2)
    seen = frame[row, col] / (lattice.peak_counts * spot)
    expected = lattice.sample.brightness(fx - stage[0], fy - stage[1])
    assert seen == pytest.approx(expected, rel=1e-3, abs=1e-4)
    assert not np.allclose(frame, lattice.expected_counts(SHAPE, PIXEL_UM, (0.0, 0.0)))


def test_trigger_positions_come_from_the_waveforms_at_each_rising_edge():
    signals = _scan_signals(3, 0.2)
    positions = trigger_positions_um(signals, 'Camera', (('X', 5.0), ('Y', 5.0)))
    assert positions.shape == (9, 2)
    assert positions[:3, 0] == pytest.approx([0.0, 0.2, 0.4])
    assert positions[:3, 1] == pytest.approx([0.0, 0.0, 0.0])
    assert positions[3] == pytest.approx([0.0, 0.2])

    # An axis the scan does not drive reads 0; no camera TTL means no placement.
    del signals['scanSignalsDict']['Y']
    assert trigger_positions_um(signals, 'Camera', (('X', 5.0), ('Y', 5.0)))[:, 1] == \
        pytest.approx(np.zeros(9))
    assert trigger_positions_um(signals, 'Other', (('X', 5.0),)) is None


def test_mock_camera_draws_triggered_frames_in_scan_order():
    camera = MockHamamatsu()
    camera.setPropertyValue('subarray_hsize', SHAPE[1])
    camera.setPropertyValue('subarray_vsize', SHAPE[0])
    source = MockLatticeFrames(_lattice(), PIXEL_UM, seed=1)
    camera.mock_frame_source = source
    camera.startAcquisition()
    camera.setPropertyValue('trigger_source', 2)

    source.setScan([(0.0, 0.0), (0.36, 0.0), (0.36, 0.36)])
    camera.mockTrigger(2)
    frames, shape = camera.getFrames()

    assert len(frames) == 2 and shape == (SHAPE[1], SHAPE[0])
    assert all(f.shape == SHAPE and f.dtype == np.uint16 for f in frames)
    lattice = source.lattice
    for frame, stage in zip(frames, [(0.0, 0.0), (0.36, 0.0)]):
        expected = lattice.expected_counts(SHAPE, PIXEL_UM, stage)
        assert np.corrcoef(frame.ravel(), expected.ravel())[0, 1] > 0.9

    # Live (internal trigger) shows the stage where the scan left it.
    camera.setPropertyValue('trigger_source', 1)
    live = camera.getLast()
    expected = lattice.expected_counts(SHAPE, PIXEL_UM, (0.36, 0.0))
    assert np.corrcoef(live.ravel(), expected.ravel())[0, 1] > 0.9


def test_mock_readout_time_is_seconds_and_follows_the_roi_height():
    camera = MockHamamatsu()
    assert camera.getPropertyValue('timing_readout_time')[0] == pytest.approx(0.01)
    camera.setPropertyValue('subarray_mode', b'ON')
    camera.setPropertyValue('subarray_vsize', 512)
    assert camera.getPropertyValue('timing_readout_time')[0] == pytest.approx(0.0025)


def _setup_camera():
    setup = json.load(open(SETUP, encoding='utf-8'))
    return setup['detectors']['Camera']


def test_shipped_monalisa_mock_camera_has_trigger_control_and_readout():
    from imswitch.imcontrol.controller.controllers.SettingsController import SettingsController

    camera = _setup_camera()
    assert camera['managerName'] == 'HamamatsuManager'
    manager = HamamatsuManager(DetectorInfo(**camera), 'Camera')
    name, internal, external = SettingsController._detectorTriggerSpec(manager)
    assert name == 'Trigger source'
    assert internal in manager.parameters[name].options
    assert external in manager.parameters[name].options
    # 512 rows of 2048: 2.5 ms, well under the cloak's 6 ms default.
    rows = camera['managerProperties']['hamamatsu']['subarray_vsize']
    assert manager.parameters['Readout time'].value == pytest.approx(0.01 * rows / 2048)
    assert manager.parameters['Readout time'].value < 0.006


def _simulated_setup():
    return SetupInfo.from_json(json.dumps({
        'detectors': {'Camera': _setup_camera()},
        'lasers': {}, 'positioners': {},
        'scan': {
            'scanWidgetType': 'PointScan', 'scanDesigner': 'GalvoScanDesigner',
            'scanDesignerParams': {}, 'TTLCycleDesigner': 'PointScanTTLCycleDesigner',
            'TTLCycleDesignerParams': {}, 'sampleRate': 100000,
        },
        'nidaq': {'simulation': True, 'timerCounterChannel': None, 'startTrigger': False},
    }), infer_missing=True)


def test_simulated_scan_over_one_period_holds_the_sample():
    """Assemble each focus's pixel at (focus - stage): that is the sample."""
    _qapp()
    setup = _simulated_setup()
    nidaq = NidaqManager(setup)
    camera = _setup_camera()
    camera['managerProperties']['hamamatsu'].update(
        subarray_hsize=SHAPE[1], subarray_vsize=SHAPE[0],
        image_width=SHAPE[1], image_height=SHAPE[0])
    manager = HamamatsuManager(DetectorInfo(**camera), 'Camera', nidaqManager=nidaq)
    manager.setParameter('Trigger source', 'External "frame-trigger"')
    manager.startAcquisition()
    done = []
    nidaq.sigScanDone.connect(lambda: done.append(True))
    n_steps, step_um = 6, 0.12   # 6 x 0.12 µm = one 0.72 µm period
    try:
        nidaq.runScan(_scan_signals(n_steps, step_um),
                      {'img_dims': [n_steps, n_steps], 'scan_samples_total': 4 * n_steps ** 2})
        assert _wait_for(lambda: done)
        frames = []
        assert _wait_for(lambda: frames.extend(manager.getChunk()) or len(frames) >= n_steps ** 2)
    finally:
        manager.stopAcquisition()
    assert len(frames) == n_steps ** 2

    lattice = MockLattice.from_property(camera['managerProperties']['mockSample'])
    pixel_um = camera['managerProperties']['cameraPixelSizeUm']
    fx, fy = lattice.foci_um(SHAPE, pixel_um)
    col = np.rint(fx / pixel_um + 0.5 * (SHAPE[1] - 1)).astype(int)
    row = np.rint(fy / pixel_um + 0.5 * (SHAPE[0] - 1)).astype(int)
    seen, truth = [], []
    for k, frame in enumerate(frames):
        stage = ((k % n_steps) * step_um, (k // n_steps) * step_um)
        seen.append(frame[row, col].astype(float))
        truth.append(lattice.sample.brightness(fx - stage[0], fy - stage[1]))
    assert np.corrcoef(np.concatenate(seen), np.concatenate(truth))[0, 1] > 0.9
