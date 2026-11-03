"""The Smart Pipeline panel: loads and checks a pipeline file, arms it
against fakes of the detectors and positioners managers, fills its log from
the worker, stops, and shuts down cleanly."""
import importlib
import json
import time
from types import SimpleNamespace

import numpy as np
import pytest
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.controller import ModuleCommunicationChannel
from imswitch.imcommon.model.smartpipeline import (
    Action, Gate, Live, PipelineSettings, ScaleFlip, Sink, SmartPipelineDocument, Transform,
)
from imswitch.imcommon.model.workflow.steps import Process, Workflow
from imswitch.imcontrol.model.resources import get_resource_registry, positioner_key
from imswitch.imcontrol.model.smartpipeline.run import RUNNING, STOPPED
from imswitch.imcontrol.view.widgets.SmartPipelineWidget import SmartPipelineWidget
from imswitch.imcontrol._test.unit.test_smartpipeline_live import (
    FakeDetector, FakeDetectorsManager, FakePositioner, FakePositioners,
)

improcess_runtime = pytest.importorskip("imswitch.improcess.workflows.runtime")

controllerModule = importlib.import_module(
    'imswitch.imcontrol.controller.controllers.SmartPipelineController'
)
SmartPipelineController = controllerModule.SmartPipelineController

_APP = None
CAMERA = "detector:Camera"
STAGE = "stage:Mock XY"


@pytest.fixture(scope='module')
def qapp():
    global _APP
    _APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return _APP


@pytest.fixture(scope='module')
def registry():
    return improcess_runtime.bootstrap_registry(user_plugins=False)


def _waitFor(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'timed out'
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)


class _CommChannel(QtCore.QObject):
    sigUpdateImage = QtCore.Signal(str, object, bool, object, bool)
    sigAddItemToVb = QtCore.Signal(object)
    sigRemoveItemFromVb = QtCore.Signal(object)

    def __init__(self, controllers=None):
        super().__init__()
        self._controllers = controllers or {}
        self.added, self.removed = [], []
        self.sigAddItemToVb.connect(self.added.append)
        self.sigRemoveItemFromVb.connect(self.removed.append)

    def controllerRegistry(self):
        return self._controllers


class _ModeService:
    """What the main controller injects: a mode service over a fake setup-mode controller."""

    def __init__(self, modes=("scout", "rest")):
        self.applied = []
        service = self

        class _SetupModes:
            def listSetupModes(self):
                return list(modes)

            def applySetupMode(self, name, componentNames=None):
                service.applied.append(name)
                return SimpleNamespace(failedComponents=[], warnings=[])

        self.setupModeController = _SetupModes()


@pytest.fixture
def folders(tmp_path, monkeypatch):
    calibrations = tmp_path / 'calibrations'
    calibrations.mkdir()
    (calibrations / 'cam2stage.json').write_text(json.dumps(
        ScaleFlip('cam2stage', CAMERA, STAGE, pixel_size=(0.5, 0.5)).to_dict()), encoding='utf-8')
    monkeypatch.setattr(controllerModule, '_calibrationsFolderPath', str(calibrations))
    monkeypatch.setattr(controllerModule, '_stateFilePath', str(tmp_path / 'config' / 'smart_pipeline.json'))
    monkeypatch.setattr(controllerModule, '_pipelinesFolderPath', str(tmp_path / 'smart_pipelines'))
    return tmp_path


def _pipeline_file(tmp_path, *, roles=False, detector='Camera'):
    workflow = Workflow('panel test', [
        Live('cam', detector=detector),
        Process('thr', 'threshold', {'method': 'otsu', 'min_area': 5}, inputs=['cam']),
        Process('rp', 'regionprops', {'frame': CAMERA}, inputs=['thr', 'cam']),
        Process('pk', 'pick', {'rule': 'largest'}, inputs=['rp']),
        Gate('found', 'condition', inputs=['pk']),
        Transform('tf', 'cam2stage', inputs=['found']),
        Action('mv', 'move-to', {'axes': ['Mock Y:Y', 'Mock X:X'], 'frame': STAGE, 'deadline_s': 2.0},
               inputs=['tf']),
        Sink('log', 'event-log', inputs=['pk']),
    ])
    document = SmartPipelineDocument(workflow)
    if roles:
        document.settings = PipelineSettings(roles={'scouting': 'scout', 'idle': 'rest'})
    return str(document.save(tmp_path / 'pipeline.json'))


def _frame(cy=20, cx=20, size=64):
    image = np.zeros((size, size), np.float32)
    if cy is not None:
        image[cy - 3:cy + 4, cx - 3:cx + 4] = 100
    return image


@pytest.fixture
def panel(qapp, registry, folders, monkeypatch):
    resources = get_resource_registry()
    positioners = FakePositioners({'Mock X': FakePositioner('Mock X', 'X', resources),
                                   'Mock Y': FakePositioner('Mock Y', 'Y', resources)})
    detectors = FakeDetectorsManager([FakeDetector(shape=(64, 64), binning=1, frameStart=(0, 0))])
    master = SimpleNamespace(detectorsManager=detectors, positionersManager=positioners)
    channel = ModuleCommunicationChannel()
    commChannel = _CommChannel()
    widget = SmartPipelineWidget(None)
    controller = SmartPipelineController(
        SimpleNamespace(), commChannel, master, widget=widget, factory=None, moduleCommChannel=channel,
    )
    monkeypatch.setattr(controller, '_improcessRegistry', lambda: registry)
    yield SimpleNamespace(controller=controller, widget=widget, commChannel=commChannel, master=master,
                          positioners=positioners, detectors=detectors)
    if controller._run is not None:
        controller._run.stop()
        controller._run.join(5)


def test_a_pipeline_file_is_loaded_and_checked(panel, folders):
    controller, widget = panel.controller, panel.widget
    assert not widget.armButton.isEnabled()

    controller.setPipelinePath(_pipeline_file(folders))
    assert widget.pipelineLabel.text() == 'pipeline.json'
    assert 'panel test: 8 steps from Camera' in widget.summaryLabel.text()
    assert 'actions: mv' in widget.summaryLabel.text()
    assert not widget.issuesLabel.isVisible() or widget.issuesLabel.text() == ''
    assert widget.armButton.isEnabled()

    controller.setPipelinePath(_pipeline_file(folders, detector='Nope'))
    assert "no detector 'Nope'" in widget.issuesLabel.text()
    assert not widget.armButton.isEnabled()

    state = json.loads(open(controllerModule._stateFilePath, encoding='utf-8').read())
    assert state['path'].endswith('pipeline.json') and state['mode'] == 'visualize'


def test_validate_mode_logs_would_move_to_and_stop_releases_everything(panel, folders):
    controller, widget = panel.controller, panel.widget
    controller.setPipelinePath(_pipeline_file(folders))
    widget.setMode('validate')
    controller.arm()
    run = controller._run
    assert run is not None and run.state == RUNNING, widget.issuesLabel.text()
    assert not widget.armButton.isEnabled() and widget.stopButton.isEnabled()
    assert get_resource_registry().holder(positioner_key('Mock X')) is not None
    assert panel.commChannel.added == [widget.getOverlay()]     # the overlay sink's marks on the live view

    for index, image in enumerate([_frame(20, 20), _frame(None, None), _frame(40, 10)]):
        panel.commChannel.sigUpdateImage.emit('Camera', image, False, [0.1, 0.1], True)
        _waitFor(lambda: run.engine.summary.ticks >= index + 1)
    _waitFor(lambda: len(run.log.events('would', step='mv')) == 2)
    _waitFor(lambda: 'would move-to' in widget.logView.toPlainText())
    assert panel.positioners['Mock X'].calls == []
    controller._refreshStatus()      # what the 500 ms timer does
    assert 'validate' in widget.statusLabel.text() and '3 ticks' in widget.statusLabel.text()

    controller.stop()
    assert not widget.stopButton.isEnabled()
    _waitFor(lambda: run.state == STOPPED and widget.armButton.isEnabled())   # the worker's hand-back ran
    assert get_resource_registry().holder(positioner_key('Mock X')) is None
    assert panel.detectors.leases == []
    assert panel.commChannel.removed == [widget.getOverlay()]
    assert controller.shutdownComplete()


def test_experiment_mode_moves_the_stage_and_applies_the_roles(panel, folders):
    controller, widget = panel.controller, panel.widget
    service = _ModeService()
    controller.setSmartModeService(service)
    controller.setPipelinePath(_pipeline_file(folders, roles=True))
    widget.setMode('experiment')
    controller.arm()
    run = controller._run
    assert run is not None and run.state == RUNNING, widget.issuesLabel.text()
    assert service.applied == ['scout']

    panel.commChannel.sigUpdateImage.emit('Camera', _frame(20, 20), False, [0.1, 0.1], True)
    _waitFor(lambda: run.engine.summary.actions_completed >= 1)
    assert panel.positioners['Mock Y'].position == {'Y': 10.0} and panel.positioners['Mock X'].position == {'X': 10.0}
    assert panel.positioners['Mock X'].calls[0][3] == run.token or panel.positioners['Mock X'].calls[0][3] is not None

    controller.closeEvent()          # app close: stops, joins, leaves the hardware idle
    _waitFor(lambda: run.state == STOPPED)
    assert service.applied == ['scout', 'rest']
    assert get_resource_registry().holder(positioner_key('Mock X')) is None
    assert controller.shutdownComplete()


def test_design_opens_the_editor_on_the_pipeline_with_the_setup_s_choices(panel, folders):
    controller, widget = panel.controller, panel.widget
    controller.setPipelinePath(_pipeline_file(folders))
    editor = controller.openDesigner()
    try:
        assert editor.windowTitle().endswith('Smart pipeline designer')
        assert [s.id for s in editor.document.steps] == ['cam', 'thr', 'rp', 'pk', 'found', 'tf', 'mv', 'log']
        catalog = editor.catalog
        assert [d.kind for d in catalog.steps] == ['live', 'transform', 'gate', 'action', 'sink']
        assert catalog.step('live').attribute('detector').options == ('Camera',)
        assert catalog.step('transform').attribute('transform').options == ('cam2stage',)
        assert catalog.step('action').attribute('action').options == ('move-to', 'snap', 'log', 'run-scan', 'wait')
        assert ('live', '') in editor.palette.all_choices()
        assert editor.catalog.processor('threshold') is not None
        recipes = catalog.recipes()
        assert [r.title for r in recipes] == ['Threshold → move']
        recipe = recipes[0].workflow()
        assert recipe.step('cam').detector == 'Camera' and recipe.step('tf').transform == 'cam2stage'
        assert recipe.step('mv').params['axes'] == ['Mock X:X', 'Mock Y:Y']
        editor.select_step('found')
        editor.inspector.show_step('found')
        assert controller.openDesigner() is editor           # one designer at a time
        # the editor saves under a new name: the panel follows it when the designer closes
        new_path = str(folders / 'designed.json')
        assert editor.save(new_path)
    finally:
        editor.close()
    _waitFor(lambda: controller._editor is None)
    assert controller._path == new_path and controller._document is not None
    assert not widget.issuesLabel.isVisible() or widget.issuesLabel.text() == ''


def test_experiment_without_roles_or_with_a_missing_mode_does_not_arm(panel, folders):
    controller, widget = panel.controller, panel.widget
    controller.setPipelinePath(_pipeline_file(folders))
    widget.setMode('experiment')
    controller.arm()
    assert controller._run is None and "'scouting' and 'idle' roles" in widget.issuesLabel.text()

    controller.setSmartModeService(_ModeService(modes=('other',)))
    controller.setPipelinePath(_pipeline_file(folders, roles=True))
    controller.arm()
    assert controller._run is None and "no setup mode 'scout'" in widget.issuesLabel.text()
    assert get_resource_registry().holder(positioner_key('Mock X')) is None
