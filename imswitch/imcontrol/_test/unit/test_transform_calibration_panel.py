"""The Transform Calibration panel driven through its ImControl controller,
offscreen, on bead recordings made by the real ImControl on the mock
(``_bead_recordings``): the camera -> scan bead calibration that used to have
its own Hardware-menu window, and the manual point pairs that back it up.

What the old window did and these check on the panel: what each file holds,
the warnings (detector kind, setups, saturated, camera grid), Fit in the
background with Cancel, the numbers, the residual limit, saving under an id
with overwrite protection and archive, the QC figure, verification with
another pair of files, the one-point prediction, the fast-axis choice.
Screenshots go to ``IMSWITCH_TRANSFORM_PANEL_SCREENSHOTS`` when that names a
folder."""
import os
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.model.smartpipeline import Points, apply_transform
from imswitch.imcontrol.controller.controllers.TransformCalibrationController import TransformCalibrationController
from imswitch.imcontrol.model import bead_calibration as bc
from imswitch.imcontrol.model.smartpipeline.calibrations import load_calibrations
from imswitch.imcontrol.view.widgets.TransformCalibrationWidget import TransformCalibrationWidget

from ._bead_recordings import FIELD, A, T, record_mock_bead_files


@pytest.fixture(scope='module')
def files(tmp_path_factory):
    return record_mock_bead_files(tmp_path_factory.mktemp('transform_panel_rec'))


def _setup_hash(files):
    return bc.read_recording(files['apd_hdf5']).attrs.get('setup:hash')


@pytest.fixture
def panel(files, tmp_path, qtbot):
    widget = TransformCalibrationWidget()
    qtbot.addWidget(widget)
    asked = SimpleNamespace(overwrite=[], answer='replace', messages=[], opened=[], copied=[])
    widget.askOverwrite = lambda cid, archived: asked.overwrite.append(cid) or asked.answer
    widget.showMessage = lambda title, text: asked.messages.append(text)
    widget.openPath = lambda path: asked.opened.append(Path(path))
    widget.copyText = lambda text: asked.copied.append(text)
    camera = SimpleNamespace(shape=(800, 800), binning=1, frameStart=(0, 0))
    master = SimpleNamespace(detectorsManager={'Camera': camera})
    running = {'hash': _setup_hash(files)}
    setupInfo = SimpleNamespace(to_dict=lambda: {}, positioners={}, detectors={}, calibrations=None)
    recording = SimpleNamespace(folderEdit=SimpleNamespace(text=lambda: files['out']))
    controller = TransformCalibrationController(
        setupInfo, SimpleNamespace(), master, widget=widget, factory=None, moduleCommChannel=None,
        recordingWidget=recording, calibrationsFolder=tmp_path / 'calibrations')
    controller._runningSetupHash = lambda: running['hash']
    widget._refreshPairWarnings()
    yield SimpleNamespace(controller=controller, widget=widget, asked=asked, camera=camera, running=running,
                          folder=tmp_path / 'calibrations')


def _load_both(p, files, camera='camera_beads_h5', scan='apd_hdf5'):
    assert p.widget.loadPath('source', files[camera])
    assert p.widget.loadPath('target', files[scan])


def _wait(p, qtbot, timeout=120000):
    qtbot.waitUntil(lambda: not p.widget.busy, timeout=timeout)


def _fit(p, qtbot):
    assert p.widget.calibrate()
    _wait(p, qtbot)


def reference_rule_matches(widget, files):
    """The panel's model choice is the command line's."""
    reference = bc.calibrate_from_files(files['camera_beads_h5'], files['apd_hdf5'], save=False)
    return widget.transform.provenance['estimator'] == reference.transform.provenance['estimator']


def _field_error(transform):
    camera_px = (FIELD - T) @ np.linalg.inv(A).T
    return np.abs(transform.map_points(camera_px) - FIELD)


def _shot(widget, name, size=(560, 1350)):
    folder = os.environ.get('IMSWITCH_TRANSFORM_PANEL_SCREENSHOTS')
    if folder:
        os.makedirs(folder, exist_ok=True)
        widget.resize(*size)
        widget.show()
        for _ in range(5):
            QtWidgets.QApplication.processEvents()
        widget.grab().save(os.path.join(folder, name))


def test_file_information_and_the_scan_defaults_when_loaded(panel, files):
    _shot(panel.widget, 'panel_empty.png')
    _load_both(panel, files)
    slots = panel.widget._slots
    camera = slots['source']['info'].text()
    for text in ('800 × 800 px', 'Binning', '0.1 µm', 'Exposure', 'Frames averaged', 'simple_mock_setup.json',
                 'Recorded'):
        assert text in camera, text
    scan = slots['target']['info'].text()
    for text in ('fast X, slow Y', 'X 24 µm × Y 20 µm', 'X 0.1 µm, Y 0.1 µm', 'X 3 µm, Y -2 µm',
                 '240 (fast) × 200 (slow)', 'Dwell', '20 µs', 'Phase delay', 'Advanced', '488 (EXC)'):
        assert text in scan, text
    assert 'Y, X in µm' in slots['target']['coordinates'].text()
    assert 'pixels' in slots['source']['coordinates'].text()
    assert not slots['source']['thumbnail'].pixmap().isNull()
    assert panel.widget.source('target').has_world
    # The bead calibration's choices for a scan Reference.
    assert panel.widget.selectedCorrespondence() == 'beads'
    assert panel.widget.selectedModel() == 'similarity_or_affine'
    assert panel.widget.residualLimitSpin.value() == pytest.approx(0.25)
    assert panel.widget.idEdit.text() == 'cam_to_scan'
    assert panel.widget.pairWarnings() == []
    assert panel.widget.runAllButton.isEnabled()
    assert 'fast_axis' in slots['target']['choiceWidgets']


def test_warnings_for_setups_grid_and_detector_kind(panel, files):
    panel.running['hash'] = 'sha256:another'
    panel.camera.binning = 2
    panel.camera.shape = (400, 400)
    _load_both(panel, files)
    text = '\n'.join(panel.widget.pairWarnings())
    assert 'another setup' in text and 'than the one running' in text
    assert 'grid now differs' in text and 'refuse' in text
    assert panel.widget.pairWarningsLabel.text()
    # The camera snap given as the Reference: pixels to pixels, said so.
    panel.widget.loadPath('target', files['camera_beads_h5'])
    assert any('both are camera recordings' in w for w in panel.widget.pairWarnings())
    # The scan given as the Moving image.
    panel.widget.loadPath('source', files['apd_hdf5'])
    assert 'scan detector' in panel.widget._slots['source']['warnings'].text()
    # Two different setups.
    import shutil
    import h5py
    other = Path(files['out']) / 'other_setup.hdf5'
    shutil.copy(files['apd_hdf5'], other)
    with h5py.File(other, 'r+') as handle:
        group = handle['APD/metadata/setup']
        for key in list(group.attrs):
            if 'hash' in key:
                group.attrs[key] = 'sha256:different'
        if 'hash' in group:
            del group['hash']
            group['hash'] = 'sha256:different'
    panel.widget.loadPath('source', files['camera_beads_h5'])
    panel.widget.loadPath('target', other)
    assert any('different setups' in w for w in panel.widget.pairWarnings())


def test_a_bead_fit_shows_the_numbers_saves_and_drives_the_pipeline(panel, files, qtbot):
    _load_both(panel, files)
    _fit(panel, qtbot)
    w = panel.widget
    transform = w.transform
    assert transform is not None, w.statusLabel.text()
    assert w.verdictLabel.text().startswith('Accepted') and 'mirrored' in w.verdictLabel.text()
    assert transform.units == 'um' and transform.source_frame == 'detector:Camera'
    assert transform.target_frame == 'scanner:galvo'
    assert transform.target_context.extra['axes'] == ['Y', 'X']
    text = w.matrixView.toPlainText()
    assert reference_rule_matches(w, files)
    for snippet in ('Model: ', 'auto: ', 'Residual: RMS', 'nm', 'MIRRORED', 'µm per moving px'):
        assert snippet in text, snippet
    # The same geometry as the command line / calibrate_from_files.
    assert _field_error(transform).max() < 0.08
    reference = bc.calibrate_from_files(files['camera_beads_h5'], files['apd_hdf5'], save=False)
    camera_px = (FIELD - T) @ np.linalg.inv(A).T
    np.testing.assert_allclose(transform.map_points(camera_px), reference.transform.map_points(camera_px),
                               atol=0.005)
    _shot(w, 'panel_bead_fit.png')
    # The inspection workspace draws the pairs in Reference pixels although the fit is in µm.
    w.openWorkspace('transform')
    QtWidgets.QApplication.processEvents()
    xs, ys = w._workspace.transformViewer.correspondence.mapped.getData()
    assert len(xs) and np.all((xs > -5) & (xs < 245)) and np.all((ys > -5) & (ys < 205))
    _shot(w._workspace, 'workspace_bead_fit.png', size=(1280, 820))
    # Save under the id: record + pipeline entry + figure + report.
    paths = w.saveToStore()
    assert paths is not None and not panel.asked.overwrite
    assert (panel.folder / 'transforms' / 'cam_to_scan.json').exists()
    assert (panel.folder / 'cam_to_scan.json').exists()
    assert paths['figure'].name == 'cam_to_scan_qc.png' and paths['figure'].exists()
    assert paths['report'].exists() and 'Residual' in paths['report'].read_text()
    assert 'Record:' in w.matrixView.toPlainText() and 'Pipeline:' in w.matrixView.toPlainText()
    w.qcFigureButton.click()
    w.openFolderButton.click()
    assert panel.asked.opened[0].name == 'cam_to_scan_qc.png'
    assert panel.asked.opened[1] == panel.folder / 'transforms'
    w.copyReportButton.click()
    assert panel.asked.copied and 'MIRRORED' in panel.asked.copied[0]
    # The pipeline names it and maps exactly as the panel does.
    registry = load_calibrations(panel.folder)
    ref = registry.get('cam_to_scan')
    assert ref.target_axes == ('Y', 'X')
    points = np.array([[10.0, 20.0], [400.0, 380.5]])
    moved = apply_transform(Points(points, frame='detector:Camera'), ref)
    assert moved.frame == 'scanner:galvo'
    np.testing.assert_allclose(moved.coordinates, transform.map_points(points), atol=1e-12)
    assert ref.quality()['residual_rms_um'] < 0.05
    # One-point prediction.
    w.predictRowSpin.setValue(400)
    w.predictColSpin.setValue(400)
    w.predictButton.click()
    assert 'Y = ' in w.predictResultLabel.text() and 'X = ' in w.predictResultLabel.text()
    # Verify with another pair (the Zarr twins).
    assert w.verifyWithFiles((files['camera_beads_zarr'], files['apd_zarr']))
    qtbot.waitUntil(lambda: bool(panel.asked.messages), timeout=60000)
    assert 'Reference points have a mapped Moving point' in panel.asked.messages[-1]
    assert w.verification['rms'] < 0.05 and w.verification['grid_problems'] == []


def test_the_psf_free_default_and_the_measured_psf_give_the_same_transform(panel, files, qtbot):
    _load_both(panel, files)
    _fit(panel, qtbot)
    default = panel.widget.transform
    assert panel.widget.localizationResult('source').metadata['scale_source'] == 'estimated'
    values = panel.widget.localizationParameters('source')
    values['measure_psf'] = True
    panel.widget.setLocalizationParameters(values)
    _fit(panel, qtbot)
    measured = panel.widget.transform
    assert panel.widget.localizationResult('source').metadata['scale_source'] == 'measured'
    camera_px = (FIELD - T) @ np.linalg.inv(A).T
    np.testing.assert_allclose(default.map_points(camera_px), measured.map_points(camera_px), atol=0.002)


def test_a_refused_fit_saves_nothing(panel, files, qtbot):
    panel.widget.loadPath('source', files['camera_h5'])      # the raw mock camera: noise
    panel.widget.loadPath('target', files['apd_hdf5'])
    _fit(panel, qtbot)
    assert panel.widget.transform is None
    assert not panel.widget.saveIdButton.isEnabled()
    assert panel.widget.saveToStore() is None
    assert not (panel.folder / 'transforms' / 'cam_to_scan.json').exists()
    # Residuals above the limit: refused, with the reason.
    _load_both(panel, files)
    panel.widget.residualLimitSpin.setValue(0.001)
    _fit(panel, qtbot)
    assert panel.widget.transform is None
    assert panel.widget.verdictLabel.text().startswith('Refused: residuals too large')
    assert not panel.widget.saveIdButton.isEnabled()


def test_overwrite_protection(panel, files, qtbot):
    _load_both(panel, files)
    _fit(panel, qtbot)
    assert panel.widget.saveToStore() is not None
    assert not panel.asked.overwrite
    panel.asked.answer = 'cancel'
    assert panel.widget.saveToStore() is None
    assert panel.asked.overwrite == ['cam_to_scan']
    panel.asked.answer = 'keep'
    assert panel.widget.saveToStore() is not None
    archived = [p.name for p in (panel.folder / 'transforms').glob('cam_to_scan_*.json')]
    assert len(archived) == 1
    assert (panel.folder / 'transforms' / 'cam_to_scan.json').exists()
    registry = load_calibrations(panel.folder)
    assert 'cam_to_scan' in registry and len(registry.ids()) == 2
    assert 'kept as' in panel.widget.matrixView.toPlainText()
    panel.asked.answer = 'replace'
    assert panel.widget.saveToStore() is not None
    assert len(load_calibrations(panel.folder).ids()) == 2


def test_the_panel_stays_responsive_during_a_fit(panel, files, qtbot):
    _load_both(panel, files)
    beats = []
    timer = QtCore.QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: beats.append(time.monotonic()))
    timer.start()
    start = time.monotonic()
    assert panel.widget.calibrate()
    assert panel.widget.busy and panel.widget.busyControls.isVisibleTo(panel.widget)
    assert not panel.widget.runAllButton.isEnabled()
    _wait(panel, qtbot)
    timer.stop()
    elapsed = time.monotonic() - start
    gaps = [b - a for a, b in zip(beats, beats[1:])]
    assert elapsed > 0.3 and len(beats) > 5
    assert max(gaps) < 0.25, (max(gaps), elapsed)
    assert panel.widget.transform is not None


def test_cancel_drops_the_running_fit(panel, files, qtbot):
    _load_both(panel, files)
    assert panel.widget.calibrate()
    panel.widget.cancelButton.click()
    assert not panel.widget.busy and panel.widget.runAllButton.isEnabled()
    assert 'Cancelled' in panel.widget.statusLabel.text()
    qtbot.wait(3000)                      # the dropped result arrives and is ignored
    assert panel.widget.transform is None


def test_the_fast_axis_choice_rereads_the_scan(panel, files):
    _load_both(panel, files)
    choice = panel.widget._slots['target']['choiceWidgets']['fast_axis']
    choice.setCurrentIndex(2)                                   # stepped
    choice.activated.emit(2)
    source = panel.widget.source('target')
    assert source.choices['fast_axis'][1] is False
    assert panel.widget._slots['target']['choiceWidgets']['fast_axis'].currentData() is False


def test_manual_pairs_on_the_recordings_back_up_the_bead_fit(panel, files, qtbot):
    """Clicks on the camera and the scan recording: camera px -> scan µm,
    saved under an id the pipeline loads."""
    _load_both(panel, files)
    _fit(panel, qtbot)
    beads = panel.widget.transform
    pairs = panel.widget.correspondenceResult
    w = panel.widget
    w.setSelectedCorrespondence('manual')
    w.setSelectedModel('similarity_reflect')
    rng = np.random.default_rng(0)
    for k in (0, 3, 6, 9, 12):        # five beads, clicked a pixel or so off; snapping finds the centres
        w.addManualPoint('moving', pairs.source_points[k] + rng.uniform(-1.2, 1.2, 2))
        w.addManualPoint('reference', pairs.target_points[k] + rng.uniform(-1.2, 1.2, 2))
    assert len(w.pairEditor) == 5 and w.transform is not None
    manual = w.transform
    assert manual.units == 'um' and manual.target_context.extra['axes'] == ['Y', 'X']
    assert w.manualPairFit.residuals.max() < 0.05 and w.manualPairFit.loo is not None
    camera_px = (FIELD - T) @ np.linalg.inv(A).T
    np.testing.assert_allclose(manual.map_points(camera_px), beads.map_points(camera_px), atol=0.06)
    w.openPointPicker()
    _shot(w._picker, 'picker_manual_pairs.png', size=(1200, 780))
    _shot(w, 'panel_manual_pairs.png')
    w.idEdit.setText('cam_to_scan_manual')
    assert w.saveToStore() is not None
    ref = load_calibrations(panel.folder).get('cam_to_scan_manual')
    assert ref.target_axes == ('Y', 'X') and ref.source_frame == 'detector:Camera'
    np.testing.assert_allclose(ref.apply(camera_px[:3]), manual.map_points(camera_px[:3]), atol=1e-12)


def test_the_running_setup_is_the_hash_recordings_carry(panel):
    """Seen on the rig: files recorded minutes before were "recorded with
    another setup", because the setup info had changed while running."""
    from imswitch.imcontrol.model import replay

    controller = panel.controller
    del controller._runningSetupHash               # the fixture's stand-in
    controller._commChannel = SimpleNamespace(sharedAttrs={replay.SETUP_HASH_KEY: 'sha256:published'})
    controller._setupInfo = SimpleNamespace(to_dict=lambda: {'changed': 'while running'})
    assert controller._runningSetupHash() == 'sha256:published'
    controller._commChannel = SimpleNamespace()
    assert controller._runningSetupHash() == replay.computeSetupHash({'changed': 'while running'})


def _scan_recording(length, device='ND-GalvoX'):
    attrs = {'ScanStage:target_device': [device, 'ND-GalvoY'], 'ScanStage:axis_length': [length, 10.0],
             'ScanStage:axis_step_size': [0.1, 0.1], 'ScanStage:axis_centerpos': [0.0, 0.0],
             'ScanStage:scan_axis_devices': [device, 'ND-GalvoY']}
    cols = int(round(length / 0.1))
    return bc.Recording(np.zeros((100, cols)), attrs, 'APD', Path('scan.hdf5'), 1, 'float64')


def test_a_fast_axis_that_is_not_a_whole_number_of_steps_is_read_not_refused():
    """Seen on the rig: such a scan made the window say "pass
    fast_axis_swept=True/False", which the window had no way to do."""
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
