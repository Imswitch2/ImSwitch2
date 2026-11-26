"""The Smart Pipeline panel's ROIs, offscreen: segment the camera's frames
with the form's settings, draw on a rotated and mirrored live view, delete
with stable ids, see them (outlines in displayed pixels), save and load, arm
with them (roi-dff reads exactly these ids), edit while armed (a new
generation: the baselines start over), refuse a set made on another grid."""
import importlib
import json
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.controller import ModuleCommunicationChannel
from imswitch.imcommon.model.smartpipeline import Live, Sink, SmartPipelineDocument
from imswitch.imcommon.model.workflow.steps import Process, Workflow
from imswitch.imcontrol.controller.display_transform import DisplayTransform, apply_display_transform
from imswitch.imcontrol.model.smartpipeline.roi_set import RoiSet, polygon_mask
from imswitch.imcontrol.model.smartpipeline.run import RUNNING
from imswitch.imcontrol.view.widgets.SmartPipelineWidget import SmartPipelineWidget
from imswitch.imcontrol._test.unit.test_smartpipeline_live import FakeDetector, FakeDetectorsManager
from imswitch.imcontrol._test.unit.test_smartpipeline_panel import _CommChannel, _waitFor  # noqa: F401

improcess_runtime = pytest.importorskip("imswitch.improcess.workflows.runtime")
controllerModule = importlib.import_module('imswitch.imcontrol.controller.controllers.SmartPipelineController')
SmartPipelineController = controllerModule.SmartPipelineController

SHAPE = (48, 64)          # rows, columns: not square
TRANSFORM = {"displayRotation": "90", "displayFlipX": "True"}


@pytest.fixture(scope='module')
def registry():
    return improcess_runtime.bootstrap_registry(user_plugins=False)


def _cells(gain=1.0):
    image = np.full(SHAPE, 10.0)
    image[5:12, 5:12] = 200.0 * gain
    image[20:30, 30:40] = 200.0
    image[5:10, 45:55] = 200.0
    return image


class _Overlay:
    def __init__(self):
        self.shown, self.drawing = None, []

    def show(self, name, outline, centres, ids, selected=()):
        self.shown = SimpleNamespace(name=name, outline=np.asarray(outline), centres=np.asarray(centres),
                                     ids=list(ids), selected=set(selected))

    def hide(self):
        self.shown = None

    def startDrawing(self, kind):
        self.drawing.append(kind)


@pytest.fixture
def panel(qapp, registry, tmp_path, monkeypatch):
    for name, value in (('_calibrationsFolderPath', tmp_path / 'calibrations'),
                        ('_stateFilePath', tmp_path / 'config' / 'smart_pipeline.json'),
                        ('_pipelinesFolderPath', tmp_path / 'smart_pipelines'),
                        ('_roisFolderPath', tmp_path / 'rois')):
        monkeypatch.setattr(controllerModule, name, str(value))
    (tmp_path / 'calibrations').mkdir()
    detectors = FakeDetectorsManager([FakeDetector(shape=(SHAPE[1], SHAPE[0]), binning=1, frameStart=(0, 0))])
    master = SimpleNamespace(detectorsManager=detectors, positionersManager={})
    setupInfo = SimpleNamespace(detectors={'Camera': SimpleNamespace(managerProperties=TRANSFORM)})
    commChannel = _CommChannel()
    widget = SmartPipelineWidget(None)
    overlay = _Overlay()
    widget.roiOverlay = overlay
    controller = SmartPipelineController(setupInfo, commChannel, master, widget=widget, factory=None,
                                         moduleCommChannel=ModuleCommunicationChannel())
    monkeypatch.setattr(controller, '_improcessRegistry', lambda: registry)
    workflow = Workflow('rois', [
        Live('cam', detector='Camera'),
        Process('dff', 'roi-dff', {'frame': 'detector:Camera', 'mask_source': 'segment', 'baseline_frames': 2,
                                   'threshold': 0.3, 'min_area': 5, 'smooth_sigma': 0.0, 'generation': 4},
                inputs=['cam']),
        Sink('log', 'event-log', inputs=['dff']),
    ])
    path = SmartPipelineDocument(workflow).save(tmp_path / 'rois.json')
    controller.setPipelinePath(str(path))
    yield SimpleNamespace(controller=controller, widget=widget, comm=commChannel, overlay=overlay, tmp=tmp_path)
    if controller._run is not None:
        controller._run.stop()
        controller._run.join(5)


@pytest.fixture(scope='module')
def qapp():
    from qtpy import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _segment(panel, frames=2):
    panel.widget.averageFrames.setValue(frames)
    panel.controller.segmentRois()
    for _ in range(frames):
        panel.comm.sigUpdateImage.emit('Camera', _cells(), False, [0.1, 0.1], True)
    _waitFor(lambda: panel.controller._collect is None and panel.widget.segmentButton.isEnabled())
    _waitFor(lambda: panel.controller._rois is not None and len(panel.controller._rois) > 0)


def test_the_form_starts_from_the_pipelines_segmentation_and_segment_now_makes_rois(panel):
    widget = panel.widget
    assert widget.minArea.value() == 5 and widget.smoothSigma.value() == 0.0
    _segment(panel)
    rois = panel.controller._rois
    assert rois.ids() == [1, 2, 3] and rois.detector == 'Camera' and rois.shape == SHAPE
    assert rois.identity['shape'] == list(SHAPE)
    assert widget.roiList.count() == 3 and '3 ROIs (3 segmented, 0 drawn)' in widget.roiSummaryLabel.text()
    # the overlay: outlines in DISPLAYED pixels (rotated 90 and mirrored), ids at the displayed centroids
    transform = DisplayTransform(90, True, False)
    expected, _ = apply_display_transform(rois.outline(), None, transform)
    shown = panel.overlay.shown
    assert shown.name == 'Camera' and shown.ids == [1, 2, 3] and np.array_equal(shown.outline, expected)
    centroid = rois.table()[0]['centroid']
    marker = np.zeros(SHAPE)
    marker[int(round(centroid[0])), int(round(centroid[1]))] = 1
    shown_at = np.argwhere(apply_display_transform(marker, None, transform)[0])[0]
    assert np.allclose(shown.centres[0], shown_at, atol=0.5)
    # kept for the next start
    assert (panel.tmp / 'rois' / 'Camera_current.tif').exists()


def test_no_frames_says_to_start_the_live_view(panel, monkeypatch):
    monkeypatch.setattr(controllerModule, '_SEGMENT_FRAMES_TIMEOUT_S', 0.2)
    panel.controller.segmentRois()
    _waitFor(lambda: panel.controller._collect is None, timeout=3)
    assert 'start its live view' in panel.widget.issuesLabel.text()


def test_draw_on_the_rotated_view_delete_and_ids_stay(panel):
    controller, widget = panel.controller, panel.widget
    _segment(panel)
    controller.drawRoi('rectangle')
    assert panel.overlay.drawing == ['rectangle']
    # a rectangle on the DISPLAYED image (64 rows x 48 columns after the quarter turn)
    vertices = [[40.5, 2.5], [40.5, 10.5], [50.5, 10.5], [50.5, 2.5]]
    new = controller.addDrawnRoi('rectangle', vertices)
    assert new == 4
    rois = controller._rois
    ids = np.arange(SHAPE[0] * SHAPE[1]).reshape(SHAPE)
    displayed, _ = apply_display_transform(ids, None, DisplayTransform(90, True, False))
    expected = set(displayed[polygon_mask(vertices, displayed.shape)].tolist())
    assert set(ids[rois.labels == 4].tolist()) == expected
    assert '1 drawn' in widget.roiSummaryLabel.text()
    # click selects (the id under the click), delete removes only it; ids are not renumbered
    controller._roiSelection = set()
    controller._onRoiClicked(2)
    assert panel.overlay.shown.selected == {2}
    controller.deleteSelectedRois()
    assert rois.ids() == [1, 3, 4] and [widget.roiList.item(i).data(256) for i in range(3)] == [1, 3, 4]
    # re-segment: new ids for the segmented ROIs, the drawn one kept
    _segment(panel)
    again = controller._rois.ids()
    assert again[0] == 4 and len(again) == 4 and min(again[1:]) > 4      # never an id used before


def test_the_overlay_layers_sit_on_the_live_layer_and_a_drawn_shape_comes_back(qapp):
    from napari.components import LayerList
    from napari.layers import Image, Labels, Points, Shapes
    from qtpy import QtWidgets

    from imswitch.imcontrol.view.widgets._roi_overlay import RoiOverlay

    class Viewer:
        def __init__(self):
            self.layers = LayerList()
            self.layers.append(Image(np.zeros((64, 48)), name='Live: Camera', scale=(0.1, 0.1), translate=(5.0, 7.0)))

        def _add(self, layer):
            self.layers.append(layer)
            return layer

        def add_labels(self, data, **kw):
            return self._add(Labels(data, **kw))

        def add_points(self, data, **kw):
            return self._add(Points(data, **kw))

        def add_shapes(self, **kw):
            return self._add(Shapes(**kw))

    viewer = Viewer()
    overlay = RoiOverlay(viewer)
    outline = np.zeros((64, 48), dtype=np.int32)
    outline[10, 10:20] = 7
    overlay.show('Camera', outline, [[12.0, 15.0]], [7], selected={7})
    labels = viewer.layers[RoiOverlay.LABELS_NAME]
    ids = viewer.layers[RoiOverlay.IDS_NAME]
    assert tuple(labels.scale) == (0.1, 0.1) and tuple(labels.translate) == (5.0, 7.0)
    assert tuple(ids.scale) == (0.1, 0.1) and list(ids.text.values) == ['7']
    drawn = []
    overlay.sigShapeDrawn.connect(lambda kind, vertices: drawn.append((kind, vertices)))
    overlay.startDrawing('ellipse')
    shapes = viewer.layers[RoiOverlay.DRAW_NAME]
    assert shapes.mode == 'add_ellipse' and tuple(shapes.scale) == (0.1, 0.1)
    shapes.add_ellipses([np.array([[20.0, 20.0], [5.0, 8.0]])])
    for _ in range(5):
        QtWidgets.QApplication.processEvents()
    assert drawn and drawn[0][0] == 'ellipse' and len(drawn[0][1]) == 4     # the box corners, displayed pixels
    assert np.allclose(np.mean(drawn[0][1], axis=0), [20.0, 20.0])
    assert len(shapes.data) == 0                                            # it is a ROI now, not a shape
    overlay.hide()
    assert RoiOverlay.LABELS_NAME not in viewer.layers


def test_save_and_load(panel):
    controller = panel.controller
    _segment(panel)
    controller._rois.remove([1])
    path = controller.saveRoisAs(str(panel.tmp / 'mine'))
    controller.clearRois()
    assert len(controller._rois) == 0
    loaded = controller.loadRoisFrom(path)
    assert loaded.ids() == [2, 3] and controller._rois is loaded
    assert panel.widget.roiList.count() == 2


def test_armed_with_the_rois_roi_dff_measures_exactly_them_and_an_edit_starts_over(panel):
    controller, widget, comm = panel.controller, panel.widget, panel.comm
    _segment(panel)
    controller._rois.remove([1])
    widget.setMode('visualize')
    controller.arm()
    run = controller._run
    assert run is not None and run.state == RUNNING, widget.issuesLabel.text()
    step = next(s for s in run.engine.workflow.steps if s.id == 'dff')
    assert step.params['mask_source'] == 'file' and step.params['generation'] == 5
    assert controller._document.workflow.steps[1].params['mask_source'] == 'segment'   # the file is untouched
    for index in range(3):       # frames differ: the source drops a frame that repeats the last one
        comm.sigUpdateImage.emit('Camera', _cells() + index, False, [0.1, 0.1], True)
        _waitFor(lambda: run.engine.summary.ticks >= index + 1)
    state = run.engine._state['dff']
    assert sorted(state['rois']['label'].tolist()) == [2, 3]
    # delete ROI 3 while armed: the next frame measures only ROI 2, from a new generation
    controller._roiSelection = {3}
    controller.deleteSelectedRois()
    assert step.params['generation'] == 6
    comm.sigUpdateImage.emit('Camera', _cells(1.01), False, [0.1, 0.1], True)
    _waitFor(lambda: run.engine.summary.ticks >= 4)
    _waitFor(lambda: 'rois' in run.engine._state['dff'])
    assert run.engine._state['dff']['rois']['label'].tolist() == [2]
    assert run.log.events('rois-changed')
    controller.stop()
    run.join(5)


def test_a_set_made_on_another_grid_is_refused_at_arm_with_what_to_do(panel):
    controller, widget = panel.controller, panel.widget
    _segment(panel)
    controller._rois.identity = {**controller._rois.identity, 'binning': [2, 2]}
    widget.setMode('visualize')
    controller.arm()
    assert controller._run is None
    assert 'not armed' in widget.issuesLabel.text() and 'binning' in widget.issuesLabel.text()
    widget.setUseRois(False)                       # the pipeline segments by itself instead
    controller.arm()
    assert controller._run is not None
    controller.stop()
    controller._run.join(5)
    assert json.loads(open(controllerModule._stateFilePath).read())['use_rois'] is False


def test_the_state_indicator_counters_and_the_frames_watchdog(panel, monkeypatch):
    controller, widget, comm = panel.controller, panel.widget, panel.comm
    controller._refreshStatus()
    assert widget.getRunState()[0] == 'Idle'
    widget.setUseRois(False)
    widget.setMode('validate')
    controller.arm()
    run = controller._run
    assert run is not None, widget.issuesLabel.text()
    comm.sigUpdateImage.emit('Camera', _cells(), False, [0.1, 0.1], True)
    _waitFor(lambda: run.engine.summary.ticks >= 1)
    controller._refreshStatus()
    state, counters = widget.getRunState()
    assert state.startswith('Warming up') and 'frames 1' in counters and 'triggers 0' in counters
    for index in range(1, 4):
        comm.sigUpdateImage.emit('Camera', _cells() + index, False, [0.1, 0.1], True)
        _waitFor(lambda: run.engine.summary.ticks >= index + 1)
    controller._refreshStatus()
    assert widget.getRunState()[0] == 'Armed — dry run (nothing scans)'
    # no frames: the watchdog disarms, with the reason shown
    monkeypatch.setattr(controllerModule, '_FRAMES_WATCHDOG_S', 0.2)
    import time as _time
    _time.sleep(0.3)
    controller._refreshStatus()
    _waitFor(lambda: run.state in ('stopped', 'failed'))
    _waitFor(lambda: widget.armButton.isEnabled())
    controller._refreshStatus()
    state, _ = widget.getRunState()
    assert state.startswith('Stopped: no frame from Camera for 0.2 s'), state
    assert run.log.events('disarmed')


def test_a_calibration_changed_on_disk_disarms(panel, tmp_path):
    controller, widget = panel.controller, panel.widget
    calibration = panel.tmp / 'calibrations' / 'x.json'
    calibration.write_text('{}')
    controller._watch = {'files': {str(calibration): 0.0}, 'lasers': [], 'since': None}
    assert 'x.json changed on disk' in controller._watchdog(SimpleNamespace())
    controller._watch = {'files': {}, 'lasers': ['561'], 'since': None}
    controller._master.lasersManager = {'561': SimpleNamespace(isUsable=False)}
    assert "laser '561' is no longer usable" in controller._watchdog(SimpleNamespace())


def test_counters_from_the_log():
    from imswitch.imcontrol.model.smartpipeline.engine import LogEntry

    controller = SimpleNamespace(_counters={'scans': 0, 'skipped': {}, 'would': 0, 'triggers': 0, 'last': '',
                                            'scanning': None})
    count = SmartPipelineController._count
    count(controller, LogEntry(5, 0.0, 'scan', 'started', {'action': 'run-scan',
                                                           'inputs': [{'first_row': {'label': 7}}]}))
    assert controller._counters['scanning'] == 7 and controller._counters['triggers'] == 1
    count(controller, LogEntry(5, 0.1, 'scan', 'done', {'result': {'scans': 1, 'trigger': {'label': 7},
                                                                   'saved': ['/x/event0001_R0_roi7_rec_APD.hdf5'],
                                                                   'skipped': []}}))
    count(controller, LogEntry(9, 0.2, 'scan', 'done', {'result': {'scans': 0, 'skipped': [
        {'reason': 'the box centre is outside the scanner range'}]}}))
    c = controller._counters
    assert c['scans'] == 1 and c['skipped'] == {'outside the scan range': 1} and c['scanning'] is None
    assert c['last'].startswith('skipped: the box centre')
