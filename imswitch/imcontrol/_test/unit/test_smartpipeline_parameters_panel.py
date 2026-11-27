"""The Smart Pipeline panel's Parameters box and its optional ROIs box,
offscreen against fakes: what a pipeline exposes shows as controls, an
edit is checked, kept per pipeline file across restarts, used at the next
arm and applied to an armed pipeline from the next frame; a pipeline that
measures no ROI set shows no ROIs box and arms without ROIs."""
import json

from imswitch.imcommon.model.smartpipeline import Gate, Live, Sink, SmartPipelineDocument, Transform, Action
from imswitch.imcommon.model.workflow.steps import Control, Process, Workflow
from imswitch.imcontrol.model.smartpipeline.run import RUNNING, STOPPED
from imswitch.imcontrol._test.unit.test_smartpipeline_panel import (  # noqa: F401 - fixtures
    CAMERA, STAGE, _frame, _waitFor, controllerModule, folders, panel, qapp, registry,
)


def _exposing_pipeline(tmp_path, name='exposing.json'):
    workflow = Workflow('exposing', [
        Live('cam', detector='Camera'),
        Process('thr', 'threshold', {'method': 'absolute', 'value': 50.0, 'min_area': 5}, inputs=['cam']),
        Process('rp', 'regionprops', {'frame': CAMERA}, inputs=['thr', 'cam']),
        Process('pk', 'pick', {'rule': 'largest'}, inputs=['rp']),
        Gate('found', 'condition', inputs=['pk']),
        Gate('rest', 'min-interval', params={'seconds': 30.0}, inputs=['found']),
        Transform('tf', 'cam2stage', inputs=['rest']),
        Action('mv', 'move-to', {'axes': ['Mock Y:Y', 'Mock X:X'], 'frame': STAGE, 'deadline_s': 2.0},
               inputs=['tf']),
        Sink('log', 'event-log', inputs=['pk']),
    ], controls=[Control('threshold', ['thr.value'], label='Threshold'),
                 Control('cooldown', ['rest.seconds'], label='Cooldown')])
    return str(SmartPipelineDocument(workflow).save(tmp_path / name))


def _roi_pipeline(tmp_path, mask_source):
    workflow = Workflow('rois', [
        Live('cam', detector='Camera'),
        Process('dff', 'roi-dff', {'frame': CAMERA, 'mask_source': mask_source}, inputs=['cam']),
        Sink('log', 'event-log', inputs=['dff']),
    ])
    return str(SmartPipelineDocument(workflow).save(tmp_path / f'rois_{mask_source}.json'))


def test_the_rois_box_shows_only_for_a_pipeline_that_measures_a_roi_set(panel, folders):
    controller, widget = panel.controller, panel.widget
    controller.setPipelinePath(_exposing_pipeline(folders))
    assert not widget.isRoisVisible()
    controller.setPipelinePath(_roi_pipeline(folders, 'segment'))
    assert widget.isRoisVisible()
    controller.setPipelinePath(_roi_pipeline(folders, 'input'))        # labels made by another step
    assert not widget.isRoisVisible()


def test_a_pipeline_without_rois_arms_without_rois_or_a_segmentation(panel, folders):
    controller, widget = panel.controller, panel.widget
    controller.setPipelinePath(_exposing_pipeline(folders))
    widget.setMode('validate')
    controller.arm()
    run = controller._run
    assert run is not None and run.state == RUNNING, widget.issuesLabel.text()
    assert 'ROIs' not in widget.getPreflightText()
    controller.stop()
    _waitFor(lambda: run.state == STOPPED)


def test_exposed_parameters_show_are_checked_and_kept_per_pipeline_file(panel, folders):
    controller, widget = panel.controller, panel.widget
    path = _exposing_pipeline(folders)
    controller.setPipelinePath(path)
    assert not widget.parametersBox.isHidden()
    threshold, cooldown = widget.getParameterEditor('threshold'), widget.getParameterEditor('cooldown')
    assert threshold.value() == 50.0 and cooldown.value() == 30.0 and cooldown.suffix() == ' s'
    assert not widget.resetParametersButton.isEnabled()

    cooldown.setValue(4.0)                     # what Enter in the spin box does
    assert controller.parameterValues() == {'threshold': 50.0, 'cooldown': 4.0}
    assert 'used when the pipeline is armed' in widget.getParametersNote()
    assert widget.resetParametersButton.isEnabled()
    # refused: below the gate's minimum; the editor shows the kept value again
    assert not controller.setParameter('cooldown', -1.0)
    assert 'below' in widget.getParametersNote() and cooldown.value() == 4.0

    # kept for this file (the file itself unchanged), back after a restart of the panel
    state = json.loads(open(controllerModule._stateFilePath, encoding='utf-8').read())
    assert state['parameters'] == {path: {'cooldown': 4.0}}
    assert SmartPipelineDocument.load(path).workflow.step('rest').params['seconds'] == 30.0
    controller.setPipelinePath(_roi_pipeline(folders, 'segment'))
    assert widget.parametersBox.isHidden()     # exposes nothing
    controller._savedParameters = json.loads(open(controllerModule._stateFilePath).read())['parameters']
    controller.setPipelinePath(path)
    assert widget.getParameterEditor('cooldown').value() == 4.0

    controller.resetParameters()
    assert controller.parameterValues()['cooldown'] == 30.0
    assert widget.getParameterEditor('cooldown').value() == 30.0
    assert json.loads(open(controllerModule._stateFilePath).read())['parameters'] == {}


def test_the_panels_values_arm_and_an_edit_while_armed_applies_to_the_running_pipeline(panel, folders):
    controller, widget = panel.controller, panel.widget
    controller.setPipelinePath(_exposing_pipeline(folders))
    controller.setParameter('cooldown', 12.0)
    widget.setMode('validate')
    controller.arm()
    run = controller._run
    assert run is not None and run.state == RUNNING, widget.issuesLabel.text()
    assert run.engine.workflow.step('rest').params['seconds'] == 12.0
    logged = run.log.events('parameters')[0].detail
    assert logged['values'] == {'threshold': 50.0, 'cooldown': 12.0} and logged['changed'] == ['cooldown']

    assert controller.setParameter('threshold', 80.0)
    assert run.engine.workflow.step('thr').params['value'] == 80.0
    assert 'applied to the running pipeline' in widget.getParametersNote()
    event = run.log.events('parameter')[-1]
    assert event.step == 'thr' and event.detail['value'] == 80.0 and event.detail['was'] == 50.0
    panel.commChannel.sigUpdateImage.emit('Camera', _frame(20, 20), False, [0.1, 0.1], True)
    _waitFor(lambda: run.engine.summary.ticks >= 1)
    controller.stop()
    _waitFor(lambda: run.state == STOPPED)
    # the file is untouched: the next arm uses the panel's values again
    assert SmartPipelineDocument.load(controller._path).workflow.step('thr').params['value'] == 50.0


def test_an_action_parameter_that_keeps_its_devices_applies_live(panel, folders):
    # (one that would change them is kept for the next arm: test_smartpipeline_parameters)
    controller, widget = panel.controller, panel.widget
    path = _exposing_pipeline(folders, 'axes.json')
    document = SmartPipelineDocument.load(path)
    document.workflow.controls.append(Control('deadline', ['mv.deadline_s']))
    document.save(path)
    controller.setPipelinePath(path)
    widget.setMode('validate')
    controller.arm()
    run = controller._run
    assert run is not None and run.state == RUNNING, widget.issuesLabel.text()
    assert controller.setParameter('deadline', 1.0)     # same devices: live
    assert run.engine.workflow.step('mv').params['deadline_s'] == 1.0
    controller.stop()
    _waitFor(lambda: run.state == STOPPED)
