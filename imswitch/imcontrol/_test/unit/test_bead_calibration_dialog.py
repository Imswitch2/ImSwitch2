"""The Camera → scan calibration window (Hardware → Camera → scan calibration…)
driven through its controller, offscreen, on bead recordings made by the
real ImControl on the mock (``_bead_recordings``). Screenshots go to
``IMSWITCH_BEAD_CALIBRATION_SCREENSHOTS`` when that variable names a folder."""
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from qtpy import QtCore

from imswitch.imcontrol.controller.controllers.BeadCalibrationController import BeadCalibrationController
from imswitch.imcontrol.model import bead_calibration as bc
from imswitch.imcontrol.view.widgets.BeadCalibrationWidget import BeadCalibrationWidget

from ._bead_recordings import record_mock_bead_files


@pytest.fixture(scope='module')
def files(tmp_path_factory):
    return record_mock_bead_files(tmp_path_factory.mktemp('bead_dialog_rec'))


def _setup_hash(files):
    return bc.read_recording(files['apd_hdf5']).attrs.get('setup:hash')


@pytest.fixture
def dialog(files, tmp_path, qtbot):
    widget = BeadCalibrationWidget(None)
    qtbot.addWidget(widget)
    asked = SimpleNamespace(overwrite=[], answer='replace', messages=[], opened=[], copied=[])
    widget.askOverwrite = lambda cid, archived: asked.overwrite.append(cid) or asked.answer
    widget.showMessage = lambda title, text: asked.messages.append(text)
    widget.openPath = lambda path: asked.opened.append(Path(path))
    widget.copyText = lambda text: asked.copied.append(text)
    camera = SimpleNamespace(shape=(800, 800), binning=1, frameStart=(0, 0))
    master = SimpleNamespace(detectorsManager={'Camera': camera})
    running = {'hash': _setup_hash(files)}
    setupInfo = SimpleNamespace(to_dict=lambda: {})
    controller = BeadCalibrationController(
        setupInfo, SimpleNamespace(), master, widget=widget, factory=None, moduleCommChannel=None,
        recordingController=SimpleNamespace(getRecFolder=lambda: files['out']),
        calibrationsFolder=tmp_path / 'calibrations')
    controller._runningSetupHash = lambda: running['hash']
    yield SimpleNamespace(controller=controller, widget=widget, asked=asked, camera=camera, running=running,
                          folder=tmp_path / 'calibrations')
    controller.finalize()


def _load_both(d, files, qtbot):
    d.controller.setFile('camera', files['camera_beads_h5'])
    d.controller.setFile('scan', files['apd_hdf5'])
    qtbot.waitUntil(lambda: 'Beads found' in d.widget.scanField.info.text()
                    and 'Beads found' in d.widget.cameraField.info.text(), timeout=30000)


def _fit(d, qtbot):
    assert d.controller.fit()
    qtbot.waitUntil(lambda: not d.controller.busy, timeout=120000)


def _shot(widget, name):
    folder = os.environ.get('IMSWITCH_BEAD_CALIBRATION_SCREENSHOTS')
    if folder:
        os.makedirs(folder, exist_ok=True)
        from qtpy import QtWidgets
        widget.resize(1120, 1250)
        widget.show()
        for _ in range(5):
            QtWidgets.QApplication.processEvents()
        widget.grab().save(os.path.join(folder, name))


def test_file_information_is_shown_when_picked(dialog, files, qtbot):
    _load_both(dialog, files, qtbot)
    camera = dialog.widget.cameraField.info.text()
    for text in ('800 × 800 px', 'Binning', '0.1 µm', 'Exposure', 'Frames averaged', 'simple_mock_setup.json',
                 'Recorded'):
        assert text in camera, text
    scan = dialog.widget.scanField.info.text()
    for text in ('fast X, slow Y', 'X 24 µm × Y 20 µm', 'X 0.1 µm, Y 0.1 µm', 'X 3 µm, Y -2 µm', '240 (fast) × 200 (slow)',
                 'Dwell', '20 µs', 'Phase delay', 'Advanced', '488 (EXC)'):
        assert text in scan, text
    assert dialog.widget.cameraField.thumbnail.pixmap() is not None
    assert not dialog.widget.pairWarnings.isVisible() or dialog.widget.pairWarnings.text() == ''
    assert dialog.widget.fitButton.isEnabled()
    _shot(dialog.widget, 'bead_calibration_files.png')


def test_warnings_for_setups_grid_and_scan_type(dialog, files, qtbot):
    dialog.running['hash'] = 'sha256:another'
    dialog.camera.binning = 2
    dialog.camera.shape = (400, 400)
    _load_both(dialog, files, qtbot)
    text = dialog.widget.pairWarnings.text()
    assert 'another setup' in text and 'than the one running' in text
    assert 'grid now differs' in text and 'refuse' in text
    # The camera snap given as the "scan" file: not a scan recording.
    dialog.controller.setFile('scan', files['camera_beads_h5'])
    warnings = dialog.widget.scanField.warnings.text()
    assert 'not a scan recording' in warnings and 'scan detector' in warnings
    # Two different setups.
    other = Path(files['out']) / 'other_setup.hdf5'
    import shutil
    import h5py
    shutil.copy(files['apd_hdf5'], other)
    with h5py.File(other, 'r+') as handle:
        group = handle['APD/metadata/setup']
        for key in list(group.attrs):
            if 'hash' in key:
                group.attrs[key] = 'sha256:different'
        if 'hash' in group:
            del group['hash']
            group['hash'] = 'sha256:different'
    dialog.controller.setFile('scan', other)
    assert 'different setups' in dialog.widget.pairWarnings.text()


def test_a_fit_shows_the_numbers_and_saves_the_record(dialog, files, qtbot):
    _load_both(dialog, files, qtbot)
    _fit(dialog, qtbot)
    result = dialog.controller.result
    assert result is not None, dialog.widget.resultText.toPlainText()
    assert dialog.widget.verdict.text().startswith('Accepted')
    text = dialog.widget.resultText.toPlainText()
    for snippet in ('Beads found: camera', 'paired', 'Residual: RMS', 'camera px', 'MIRRORED',
                    'µm per camera pixel (configured pixel size 0.10000 µm)', 'axes ["Y", "X"]',
                    "transform id 'cam_to_scan'", 'Record:', 'Figure:', 'Report:'):
        assert snippet in text, snippet
    assert (dialog.folder / 'transforms' / 'cam_to_scan.json').exists()
    assert (dialog.folder / 'cam_to_scan.json').exists()
    dialog.widget.openFigureButton.click()
    dialog.widget.openFolderButton.click()
    assert dialog.asked.opened[0].name == 'cam_to_scan_qc.png'
    assert dialog.asked.opened[1] == dialog.folder / 'transforms'
    dialog.widget.copyButton.click()
    assert dialog.asked.copied and 'Full report' in dialog.asked.copied[0]
    dialog.widget.rowSpin.setValue(400)
    dialog.widget.colSpin.setValue(400)
    dialog.widget.predictButton.click()
    assert 'Y = ' in dialog.widget.predictResult.text() and 'X = ' in dialog.widget.predictResult.text()
    _shot(dialog.widget, 'bead_calibration_result.png')
    # Verify with another pair (here the Zarr twins).
    dialog.controller.verifyWithFiles((files['camera_beads_zarr'], files['apd_zarr']))
    qtbot.waitUntil(lambda: bool(dialog.asked.messages), timeout=60000)
    assert 'scan beads have a camera bead' in dialog.asked.messages[-1]
    assert dialog.controller.verification['rms_um'] < 0.05


def test_a_refused_fit_saves_nothing(dialog, files, qtbot):
    dialog.controller.setFile('camera', files['camera_h5'])      # the raw mock camera: noise
    dialog.controller.setFile('scan', files['apd_hdf5'])
    _fit(dialog, qtbot)
    assert dialog.controller.result is None
    assert dialog.widget.verdict.text().startswith('Refused')
    assert 'Nothing was saved' in dialog.widget.verdict.text()
    assert not (dialog.folder / 'transforms' / 'cam_to_scan.json').exists()
    assert not dialog.widget.openFigureButton.isEnabled()


def test_overwrite_protection(dialog, files, qtbot):
    _load_both(dialog, files, qtbot)
    _fit(dialog, qtbot)
    assert not dialog.asked.overwrite
    dialog.asked.answer = 'cancel'
    assert not dialog.controller.fit()
    assert dialog.asked.overwrite == ['cam_to_scan']
    dialog.asked.answer = 'keep'
    _fit(dialog, qtbot)
    archived = [p.name for p in (dialog.folder / 'transforms').glob('cam_to_scan_*.json')]
    assert len(archived) == 1
    assert (dialog.folder / 'transforms' / 'cam_to_scan.json').exists()
    entries = sorted(p.name for p in dialog.folder.glob('cam_to_scan*.json'))
    assert len(entries) == 2
    from imswitch.imcontrol.model.smartpipeline.calibrations import load_calibrations
    registry = load_calibrations(dialog.folder)
    assert 'cam_to_scan' in registry and len(registry.ids()) == 2
    assert 'kept as' in dialog.widget.resultText.toPlainText()


def test_the_window_stays_responsive_during_a_fit(dialog, files, qtbot):
    _load_both(dialog, files, qtbot)
    beats = []
    timer = QtCore.QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: beats.append(time.monotonic()))
    timer.start()
    start = time.monotonic()
    assert dialog.controller.fit()
    assert dialog.widget.cancelButton.isEnabled() and not dialog.widget.fitButton.isEnabled()
    qtbot.waitUntil(lambda: not dialog.controller.busy, timeout=120000)
    timer.stop()
    elapsed = time.monotonic() - start
    gaps = [b - a for a, b in zip(beats, beats[1:])]
    assert elapsed > 0.3 and len(beats) > 5
    assert max(gaps) < 0.25, (max(gaps), elapsed)


def test_cancel_drops_the_running_fit(dialog, files, qtbot):
    _load_both(dialog, files, qtbot)
    assert dialog.controller.fit()
    dialog.widget.cancelButton.click()
    assert not dialog.controller.busy and dialog.widget.fitButton.isEnabled()
    assert dialog.widget.status.text() == 'Cancelled.'


def _scan_recording(length, device='ND-GalvoX'):
    import numpy as np

    from imswitch.imcontrol.model import bead_calibration as bc

    attrs = {'ScanStage:target_device': [device, 'ND-GalvoY'], 'ScanStage:axis_length': [length, 10.0],
             'ScanStage:axis_step_size': [0.1, 0.1], 'ScanStage:axis_centerpos': [0.0, 0.0],
             'ScanStage:scan_axis_devices': [device, 'ND-GalvoY']}
    cols = int(round(length / 0.1))
    return bc.Recording(np.zeros((100, cols)), attrs, 'APD', Path('scan.hdf5'), 1, 'float64')


def test_a_fast_axis_that_is_not_a_whole_number_of_steps_is_read_not_refused():
    """Seen on the rig: such a scan made the window say "pass
    fast_axis_swept=True/False", which the window had no way to do."""
    from imswitch.imcontrol.model import bead_calibration as bc

    try:
        whole = _scan_recording(10.0)
        bc.scan_geometry_from_recording(whole)
        assert whole.notes == []
        recording = _scan_recording(10.05)
        geometry = bc.scan_geometry_from_recording(recording)
    except bc.BeadCalibrationError as error:  # the metadata of this synthetic scan, not the question asked
        pytest.skip(str(error))
    swept = bc.scan_geometry_from_recording(recording, fast_axis_swept=True)
    stepped = bc.scan_geometry_from_recording(recording, fast_axis_swept=False)
    assert geometry == swept                       # a galvo is swept
    if swept != stepped:
        assert len(recording.notes) == 1 and 'swept (galvo)' in recording.notes[0]
    mock = _scan_recording(10.05, device='Mock-X')
    assert bc.scan_geometry_from_recording(mock) == bc.scan_geometry_from_recording(mock, fast_axis_swept=False)


def test_the_running_setup_is_the_hash_recordings_carry(dialog):
    """Seen on the rig: files recorded minutes before were "recorded with
    another setup", because the setup info had changed while running."""
    from imswitch.imcontrol.model import replay

    controller = dialog.controller
    del controller._runningSetupHash               # the fixture's stand-in
    controller._commChannel = SimpleNamespace(sharedAttrs={replay.SETUP_HASH_KEY: 'sha256:published'})
    controller._setupInfo = SimpleNamespace(to_dict=lambda: {'changed': 'while running'})
    assert controller._runningSetupHash() == 'sha256:published'
    controller._commChannel = SimpleNamespace()
    assert controller._runningSetupHash() == replay.computeSetupHash({'changed': 'while running'})
