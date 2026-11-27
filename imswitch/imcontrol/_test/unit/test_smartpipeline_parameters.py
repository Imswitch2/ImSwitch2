"""A pipeline's exposed parameters on the imcontrol side: what the shipped
roi-dff pipeline exposes, a value checked as its control checks it, the
changes it makes, and an edit applied to the running engine between ticks
(a gate's interval) or refused live when it would change an action's
devices."""
import os

import numpy as np
import pytest

from imswitch.imcommon.model.smartpipeline import Action, Gate, Live, Points, Sink, SmartPipelineDocument, Transform
from imswitch.imcommon.model.workflow.steps import Control, Process, Workflow
from imswitch.imcontrol.model.smartpipeline import Engine, EngineError, RunMode
from imswitch.imcontrol.model.smartpipeline.catalog import catalog
from imswitch.imcontrol.model.smartpipeline.mock import ArrayFrameSource, FunctionStepRunner, MockActions
from imswitch.imcontrol.model.smartpipeline.parameters import (
    apply_changes,
    changes_for,
    checked_value,
    exposed_parameters,
    live_refusal,
)

SHIPPED = os.path.join(os.path.dirname(__file__), '..', '..', '..', '_data', 'user_defaults', 'smart_pipelines')


def test_the_shipped_roi_dff_pipeline_exposes_its_threshold_and_cooldown():
    runtime = pytest.importorskip("imswitch.improcess.workflows.runtime")
    registry = runtime.bootstrap_registry(user_plugins=False)
    document = SmartPipelineDocument.load(os.path.join(SHIPPED, 'roi_dff_triggered_apd_scan.json'))
    parameters = {p.name: p for p in exposed_parameters(document.workflow, registry=registry, actions=catalog())}
    assert list(parameters) == ['dff_threshold', 'frames_above', 'cooldown_s', 'max_scans']
    threshold = parameters['dff_threshold']
    assert threshold.usable and threshold.label == 'dF/F threshold' and threshold.spec.type == 'float'
    assert threshold.file_value == 0.3 and threshold.targets == [('dff', 'threshold')]
    cooldown = parameters['cooldown_s']
    assert cooldown.unit == 's' and cooldown.file_value == 5.0 and cooldown.spec.min == pytest.approx(1e-3)
    assert parameters['max_scans'].spec.type == 'int' and parameters['max_scans'].file_value == 10
    assert checked_value(threshold, 0.5) == 0.5
    assert checked_value(parameters['max_scans'], 3) == 3
    with pytest.raises(ValueError, match='below'):
        checked_value(cooldown, 0.0)
    with pytest.raises(ValueError):
        checked_value(parameters['max_scans'], 'many')
    changes = changes_for(parameters.values(), {'dff_threshold': 0.5, 'cooldown_s': 2.0, 'unknown': 1})
    assert changes == {('dff', 'threshold'): 0.5, ('rest', 'seconds'): 2.0}
    apply_changes(document.workflow, changes)
    assert document.workflow.step('dff').params['threshold'] == 0.5
    assert document.workflow.step('rest').params['seconds'] == 2.0
    # without ImProcess the processing step's field is unknown: shown, not settable
    without = {p.name: p for p in exposed_parameters(document.workflow, registry=None, actions=catalog())}
    assert not without['dff_threshold'].usable and without['dff_threshold'].problem
    assert without['cooldown_s'].usable


def test_the_shipped_mock_demo_exposes_its_threshold():
    runtime = pytest.importorskip("imswitch.improcess.workflows.runtime")
    document = SmartPipelineDocument.load(os.path.join(SHIPPED, 'threshold_move_demo.json'))
    parameters = exposed_parameters(document.workflow, registry=runtime.bootstrap_registry(user_plugins=False),
                                    actions=catalog())
    assert [(p.name, p.file_value, p.usable) for p in parameters] == [('threshold', 250.0, True),
                                                                      ('settle_s', 2.0, True)]


def _bright(inputs, params, state):
    return Points([[1.0, 2.0]], frame='detector:Camera')


def _pipeline():
    return SmartPipelineDocument(Workflow('p', [
        Live('cam', detector='Camera'),
        Process('c', 'bright', {}, inputs=['cam']),
        Gate('rest', 'min-interval', params={'seconds': 10.0}, inputs=['c']),
        Transform('tf', 'identity', inputs=['rest']),
        Action('note', 'log', {'message': 'hit'}, inputs=['tf']),
        Sink('log', 'event-log', inputs=['rest.fired']),
    ], controls=[Control('cooldown', ['rest.seconds'])]))


def test_an_edit_while_armed_applies_from_the_next_tick_and_is_logged():
    from imswitch.imcommon.model.smartpipeline import Frame, TransformRegistry
    from imswitch.imcommon.model.smartpipeline.transforms import OneToOne

    now = [0.0]
    engine = Engine(_pipeline(), source=ArrayFrameSource([]), steps=FunctionStepRunner({'bright': _bright}),
                    actions=MockActions(), transforms=TransformRegistry([OneToOne()]), mode=RunMode.VISUALIZE,
                    clock=lambda: now[0])
    engine.arm()
    frame = np.zeros((4, 4), np.float32)

    def tick(t, sequence):
        now[0] = t
        return engine.tick(Frame(frame, 'Camera', sequence=sequence, t=t)).started

    assert tick(0.0, 1) == 'note'           # the first trigger opens the gate
    assert tick(2.0, 2) is None             # 10 s interval: closed
    engine.set_params({('rest', 'seconds'): 1.0}, why='test')
    assert tick(3.0, 3) == 'note'           # 1 s interval from now on
    entry = engine.log.events('parameter')[-1]
    assert entry.step == 'rest' and entry.detail == {'key': 'seconds', 'value': 1.0, 'was': 10.0, 'why': 'test'}
    with pytest.raises(EngineError, match='no step'):
        engine.set_params({('nope', 'x'): 1})


def test_an_action_edit_that_changes_its_devices_waits_for_the_next_arm():
    from imswitch.imcontrol.model.smartpipeline.live_actions import live_backend

    workflow = Workflow('p', [Live('cam', detector='Camera'),
                              Action('mv', 'move-to', {'positioner': 'XY', 'axes': ['Y', 'X'], 'frame': 'stage:XY',
                                                       'deadline_s': 5.0}, inputs=['cam'])])
    backend = live_backend({})
    assert live_refusal(workflow, {('mv', 'deadline_s'): 2.0}, backend) == ''
    assert 'other devices' in live_refusal(workflow, {('mv', 'positioner'): 'Z'}, backend)
    assert live_refusal(workflow, {('rest', 'seconds'): 2.0}, backend) == ''
