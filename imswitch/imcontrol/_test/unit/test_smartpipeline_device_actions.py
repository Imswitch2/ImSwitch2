"""The device actions of the smart pipeline -- flip-mirror, laser,
setup-mode -- against fakes of the panels they go through: in the catalogue
only when the setup has the device, sorted by category, checked at arm,
"would …" in a dry run, performed in an experiment, and failing soft (the
action fails with the reason; nothing raises) when a device is absent or
refuses."""
import time

import numpy as np
import pytest

from imswitch.imcommon.model.smartpipeline import (
    Action, Frame, Gate, Live, PipelineSettings, SmartPipelineDocument,
)
from imswitch.imcommon.model.workflow.steps import Workflow
from imswitch.imcontrol.model.smartpipeline import ArmError, Engine, RunMode
from imswitch.imcontrol.model.smartpipeline.catalog import SetupDevices, catalog
from imswitch.imcontrol.model.smartpipeline.live_actions import (
    FlipMirrorAdapter, LaserAdapter, RotateAdapter, SetupModeAdapter, live_backend,
)
from imswitch.imcontrol.model.smartpipeline.mock import ArrayFrameSource, FunctionStepRunner
from imswitch.imcontrol.model.smartpipeline.ports import ActionRequest
from imswitch.imcontrol._test.unit.test_smartpipeline_panel import folders, panel, qapp, registry  # noqa: F401

DEVICES = SetupDevices(flip_mirrors={'Snouty': ('widefield', 'light sheet'), 'Wheel': ('a', 'b', 'c')},
                       lasers={'488': False, 'LED': True}, setup_modes=('scout', 'event'))


class FakeFlipPanel:
    def __init__(self, refuse=()):
        self.moves, self.refuse = [], set(refuse)

    def move_flip(self, name, state):
        if name in self.refuse:
            return None            # what the panel returns for a follower / unknown mirror
        self.moves.append((name, int(state)))
        return True


class FakeLaserPanel:
    def __init__(self, readOnly=False, refuseValue=False, stuck=False):
        self.active, self.values, self.calls = {}, {}, []
        self.readOnly, self.refuseValue, self.stuck = readOnly, refuseValue, stuck

    def setLaserValue(self, name, value):
        self.calls.append(('value', name, value))
        if self.refuseValue:
            return False
        self.values[name] = value
        return True

    def setLaserActive(self, name, active):
        if self.readOnly:
            raise RuntimeError('Laser controls are read-only while a scan is active.')
        self.calls.append(('active', name, active))
        if not self.stuck:
            self.active[name] = bool(active)

    def getLaserActive(self, name):
        return self.active.get(name, False)


def _request(action, params, mode='experiment'):
    return ActionRequest(step_id='a', action=action, params=params, inputs=[], mode=mode, tick=1, epoch=1)


def test_device_actions_appear_only_with_the_device_and_sort_by_category():
    plain = catalog()
    assert 'flip-mirror' not in plain and 'laser' not in plain and 'setup-mode' not in plain
    full = catalog(DEVICES)
    assert [(category, [s.id for s in specs]) for category, specs in full.by_category()] == [
        ('Scan', ['run-scan']), ('Motion', ['move-to']), ('Light', ['laser']),
        ('Devices', ['flip-mirror', 'setup-mode']), ('Recording', ['snap']), ('Other', ['log', 'wait'])]
    flip = full.get('flip-mirror')
    mirror = next(f for f in flip.fields if f.key == 'mirror')
    assert mirror.options == ('Snouty', 'Wheel') and flip.params['mirror'] == 'Snouty'
    assert next(f for f in flip.fields if f.key == 'position').max == 2
    assert catalog(SetupDevices(flip_mirrors={'M': ('0', '1')})).ids()[-1] == 'flip-mirror'
    # setup modes unavailable in this session: no setup-mode action; available but none saved: free text
    assert 'setup-mode' not in catalog(SetupDevices(setup_modes=None))
    assert catalog(SetupDevices(setup_modes=())).get('setup-mode').fields[0].type == 'text'


def test_the_designer_lists_actions_grouped_with_their_device_choices():
    from imswitch.imcommon.model.smartpipeline.descriptors import pipeline_descriptors

    action = next(d for d in pipeline_descriptors(actions=catalog(DEVICES)) if d.kind == 'action')
    field = action.attribute('action')
    assert field.options[:3] == ('run-scan', 'move-to', 'laser')
    assert 'Devices › Flip mirror (flip-mirror)' in field.option_labels
    fields = {f.key: f for f in action.params_fields(Action('a', 'flip-mirror', {}, inputs=['x']))}
    assert fields['mirror'].type == 'select' and fields['position'].type == 'int'


def test_flip_mirror_moves_through_the_panel_and_fails_soft():
    panel = FakeFlipPanel(refuse={'Wheel'})
    adapter = FlipMirrorAdapter(lambda: {'FlipMirror': panel}, DEVICES.flip_mirrors)
    assert adapter.resources({'mirror': 'Snouty', 'position': 1}) == set()
    assert adapter.plan(_request('flip-mirror', {'mirror': 'Snouty', 'position': 1}))['message'] == \
        "would set flip mirror 'Snouty' to position 1 (light sheet)"
    done = adapter.start(_request('flip-mirror', {'mirror': 'Snouty', 'position': 1}), None)
    assert done.done and done.result == {'mirror': 'Snouty', 'position': 1, 'name': 'light sheet'}
    assert panel.moves == [('Snouty', 1)]
    refused = adapter.start(_request('flip-mirror', {'mirror': 'Wheel', 'position': 2}), None)
    assert refused.outcome == 'failed' and 'did not move' in refused.error
    assert 'no flip mirror' in adapter.problems({'mirror': 'Nope', 'position': 0})[0]
    assert 'positions 0..1' in adapter.problems({'mirror': 'Snouty', 'position': 5})[0]
    absent = FlipMirrorAdapter(lambda: {}, DEVICES.flip_mirrors).start(
        _request('flip-mirror', {'mirror': 'Snouty', 'position': 0}), None)
    assert absent.outcome == 'failed' and 'no Flip Mirrors panel' in absent.error


def test_laser_switches_through_the_panel_and_fails_soft():
    panel = FakeLaserPanel()
    adapter = LaserAdapter(lambda: {'Laser': panel}, DEVICES.lasers)
    params = {'laser': '488', 'enabled': True, 'set_power': True, 'power': 12.5}
    assert adapter.plan(_request('laser', params))['message'] == "would switch laser '488' on at 12.5"
    done = adapter.start(_request('laser', params), None)
    assert done.result == {'laser': '488', 'enabled': True, 'power': 12.5}
    assert panel.calls == [('value', '488', 12.5), ('active', '488', True)]
    off = adapter.start(_request('laser', {'laser': '488', 'enabled': False, 'set_power': False}), None)
    assert off.done and panel.active['488'] is False
    for fake, words in ((FakeLaserPanel(readOnly=True), 'read-only'), (FakeLaserPanel(refuseValue=True), 'refused'),
                        (FakeLaserPanel(stuck=True), 'did not switch on')):
        failed = LaserAdapter(lambda fake=fake: {'Laser': fake}, DEVICES.lasers).start(_request('laser', params), None)
        assert failed.outcome == 'failed' and words in failed.error
    assert 'on/off laser' in adapter.problems({'laser': 'LED', 'enabled': True, 'set_power': True, 'power': 1})[0]
    assert 'no laser' in adapter.problems({'laser': '561', 'enabled': True})[0]


def test_setup_mode_applies_and_fails_soft():
    applied = []
    adapter = SetupModeAdapter(lambda mode: applied.append(mode) or (None if mode == 'event' else 'components failed: X'),
                               lambda: ['scout', 'event'])
    assert adapter.start(_request('setup-mode', {'mode': 'event'}), None).result == {'mode': 'event'}
    failed = adapter.start(_request('setup-mode', {'mode': 'scout'}), None)
    assert failed.outcome == 'failed' and 'components failed' in failed.error
    unknown = adapter.start(_request('setup-mode', {'mode': 'gone'}), None)
    assert unknown.outcome == 'failed' and 'no setup mode' in unknown.error and applied == ['event', 'scout']


def _always(inputs, params, state):
    from imswitch.imcommon.model.smartpipeline import Value

    return Value(1.0)


def _engine(mode, backend, mirror='Snouty'):
    document = SmartPipelineDocument(Workflow('flip on trigger', [
        Live('cam', detector='Camera'),
        Gate('once', 'once', inputs=['cam']),
        Action('flip', 'flip-mirror', {'mirror': mirror, 'position': 1}, inputs=['once.fired']),
        Action('off', 'laser', {'laser': '488', 'enabled': False}, inputs=['flip']),
    ]))
    document.settings = PipelineSettings(roles={'scouting': 'scout', 'idle': 'scout'})
    return Engine(document, source=ArrayFrameSource([]), steps=FunctionStepRunner({}), actions=backend,
                  mode=mode, wait_actions=True)


def _tick(engine):
    engine.tick(Frame(np.zeros((4, 4), np.float32), 'Camera', sequence=1, t=0.0))


def test_a_trigger_flips_the_mirror_and_switches_the_laser_off_in_an_experiment():
    flip, laser = FakeFlipPanel(), FakeLaserPanel()
    laser.active['488'] = True
    backend = live_backend({}, devices=DEVICES, controllers=lambda: {'FlipMirror': flip, 'Laser': laser})
    engine = _engine(RunMode.EXPERIMENT, backend)
    engine.arm()
    _tick(engine)
    deadline = time.monotonic() + 5
    while engine.busy and time.monotonic() < deadline:
        engine._poll_completion()
        time.sleep(0.01)
    assert flip.moves == [('Snouty', 1)] and laser.active['488'] is False
    assert engine.summary.error is None and engine.summary.actions_completed == 2


def test_a_dry_run_says_what_it_would_do_and_moves_nothing():
    flip, laser = FakeFlipPanel(), FakeLaserPanel()
    backend = live_backend({}, devices=DEVICES, controllers=lambda: {'FlipMirror': flip, 'Laser': laser})
    engine = _engine(RunMode.VALIDATE, backend)
    engine.arm()
    _tick(engine)
    messages = [entry.detail['message'] for entry in engine.log.events('would')]
    assert messages[0] == "would set flip mirror 'Snouty' to position 1 (light sheet)"
    assert flip.moves == [] and laser.calls == []


def test_arming_refuses_an_action_whose_device_the_setup_does_not_have():
    backend = live_backend({}, devices=DEVICES, controllers=lambda: {})
    with pytest.raises(ArmError, match="no flip mirror 'Gone'"):
        _engine(RunMode.VALIDATE, backend, mirror='Gone').arm()
    _engine(RunMode.VISUALIZE, backend, mirror='Gone').arm()      # actions skipped: arms
    # a setup without flip mirrors does not know the action at all
    bare = live_backend({}, devices=SetupDevices(lasers={'488': False}), controllers=lambda: {})
    with pytest.raises(ArmError, match="unknown action 'flip-mirror'"):
        _engine(RunMode.VALIDATE, bare).arm()


def test_the_panel_offers_the_device_actions_of_its_setup(panel):
    from types import SimpleNamespace

    controller = panel.controller
    assert 'flip-mirror' not in controller._catalog() and 'laser' not in controller._catalog()

    class Mirrors(dict):
        def hasDevices(self):
            return bool(self)

        def getAllDeviceNames(self):
            return list(self)

    panel.master.flipMirrorsManager = Mirrors(
        Snouty=SimpleNamespace(get_state_names=lambda: {0: 'widefield', 1: 'light sheet'}))
    panel.master.lasersManager = Mirrors({'488': SimpleNamespace(isBinary=False)})
    devices = controller._setupDevices()
    assert devices.flip_mirrors == {'Snouty': ('widefield', 'light sheet')} and devices.lasers == {'488': False}
    assert devices.setup_modes is None            # no setup-mode service in this session
    assert {'flip-mirror', 'laser'} <= set(controller._catalog().ids())
    assert 'setup-mode' not in controller._catalog()
    assert 'rotate' not in controller._catalog()
    panel.master.rotatorsManager = [('HWP', SimpleNamespace())]     # a MultiManager iterates (name, manager)
    assert controller._setupDevices().rotators == {'HWP': None} and 'rotate' in controller._catalog()


# -- rotate ----------------------------------------------------------------------------

class FakeRotator:
    def __init__(self, name, fallBack=False, fails=False):
        self.name, self.position, self.isSimulated = name, 0.0, False
        self.calls, self.fallBack, self.fails = [], fallBack, fails

    def move_abs(self, value, owner=None):
        self.calls.append(('abs', value, owner))
        if self.fails:
            raise RuntimeError('mount answered NAK')
        self.position = float(value)
        self.isSimulated = self.fallBack

    def move_rel(self, value, owner=None):
        self.calls.append(('rel', value, owner))
        self.position += float(value)


class FakeExecutor:
    def __init__(self, hang=False):
        self.hang, self.steps = hang, []

    def run_step(self, resource, label, fn, deadline_s):
        self.steps.append((resource, label, deadline_s))
        if self.hang:
            return False, f'{label} did not finish within {deadline_s:g} s'
        try:
            fn()
        except Exception as exc:
            return False, f'{type(exc).__name__}: {exc}'
        return True, ''


def test_rotate_is_offered_with_the_setups_rotators_in_the_motion_category():
    assert 'rotate' not in catalog() and 'rotate' not in catalog(DEVICES)
    full = catalog(SetupDevices(rotators={'HWP': None, 'QWP': (0.0, 180.0)}))
    spec = full.get('rotate')
    assert spec.category == 'Motion' and spec.params['rotator'] == 'HWP'
    fields = {f.key: f for f in spec.fields}
    assert fields['rotator'].options == ('HWP', 'QWP') and fields['position'].type == 'float'
    assert fields['position'].suffix == 'deg' and fields['relative'].type == 'bool'
    # position is a field a control can expose (float, with its unit)
    from imswitch.imcommon.model.smartpipeline.descriptors import param_field

    step = Action('turn', 'rotate', {'rotator': 'HWP', 'position': 22.5}, inputs=['x'])
    assert param_field(step, 'position', full).suffix == 'deg'


def test_rotate_reserves_the_rotator_moves_with_the_token_and_refreshes_the_panel():
    hwp = FakeRotator('HWP')
    shown, executor = [], FakeExecutor()
    adapter = RotateAdapter({'HWP': hwp}, {'HWP': None, 'QWP': (0.0, 180.0)}, executor=executor, notify=shown.append)
    assert adapter.resources({'rotator': 'HWP'}) == {'rotator:HWP'}
    assert adapter.plan(_request('rotate', {'rotator': 'HWP', 'position': 22.5}))['message'] == \
        "would rotate 'HWP' to 22.5 deg"
    done = adapter.start(_request('rotate', {'rotator': 'HWP', 'position': 22.5, 'deadline_s': 5.0}), 'tok')
    assert done.result == {'rotator': 'HWP', 'relative': False, 'requested_deg': 22.5, 'position_deg': 22.5}
    assert hwp.calls == [('abs', 22.5, 'tok')] and shown == ['HWP']
    assert executor.steps == [('rotator:HWP', 'rotate HWP', 5.0)]
    by = adapter.start(_request('rotate', {'rotator': 'HWP', 'position': -5, 'relative': True}), 'tok')
    assert by.done and hwp.position == 17.5 and hwp.calls[-1] == ('rel', -5.0, 'tok')


def test_rotate_is_checked_at_arm_and_fails_soft():
    adapter = RotateAdapter({'HWP': FakeRotator('HWP')}, {'HWP': None, 'QWP': (0.0, 180.0)},
                            executor=FakeExecutor())
    assert 'no rotator' in adapter.problems({'rotator': 'Nope', 'position': 1.0})[0]
    assert 'not a number' in adapter.problems({'rotator': 'HWP', 'position': 'ten'})[0]
    assert 'from 0 to 180' in adapter.problems({'rotator': 'QWP', 'position': 200.0})[0]
    assert adapter.problems({'rotator': 'QWP', 'position': 200.0, 'relative': True}) == []
    refused = RotateAdapter({'HWP': FakeRotator('HWP', fails=True)}, {'HWP': None}, executor=FakeExecutor())
    failed = refused.start(_request('rotate', {'rotator': 'HWP', 'position': 1.0}), None)
    assert failed.outcome == 'failed' and 'NAK' in failed.error
    fell = RotateAdapter({'HWP': FakeRotator('HWP', fallBack=True)}, {'HWP': None}, executor=FakeExecutor())
    failed = fell.start(_request('rotate', {'rotator': 'HWP', 'position': 1.0}), None)
    assert failed.outcome == 'failed' and 'simulation' in failed.error
    stuck = RotateAdapter({'HWP': FakeRotator('HWP')}, {'HWP': None}, executor=FakeExecutor(hang=True))
    unfinished = stuck.start(_request('rotate', {'rotator': 'HWP', 'position': 1.0}), None)
    assert unfinished.outcome == 'failed' and not unfinished.ended and stuck.quarantined == {'rotator:HWP'}
    stuck.released('rotator:HWP')
    assert stuck.quarantined == set()


def test_a_trigger_rotates_in_an_experiment_with_the_reservation_and_arm_refuses_an_unknown_rotator():
    from imswitch.imcontrol.model.resources import get_resource_registry
    from imswitch.imcontrol.model.smartpipeline.run import PipelineRun

    hwp = FakeRotator('HWP')
    devices = SetupDevices(rotators={'HWP': None})
    backend = live_backend({}, devices=devices, rotators={'HWP': hwp}, executor=FakeExecutor())
    assert backend.resources_for(Workflow('p', [Live('cam', detector='Camera'),
                                                Action('turn', 'rotate', {'rotator': 'HWP', 'position': 45.0},
                                                       inputs=['cam'])])) == {'rotator:HWP'}
    document = SmartPipelineDocument(Workflow('turn on trigger', [
        Live('cam', detector='Camera'),
        Gate('once', 'once', inputs=['cam']),
        Action('turn', 'rotate', {'rotator': 'HWP', 'position': 45.0}, inputs=['once.fired']),
    ]))
    document.settings = PipelineSettings(roles={'scouting': '(unchanged)', 'idle': '(unchanged)'})
    class _Source(ArrayFrameSource):
        def start(self):
            pass

    run = PipelineRun(document, source=_Source([np.zeros((4, 4), np.float32)]),
                      steps=FunctionStepRunner({}), actions=backend, mode=RunMode.EXPERIMENT,
                      apply_mode=lambda name: None, resource_registry=get_resource_registry())
    run.arm()
    assert 'rotator:HWP' in run.reserved
    token = run.token
    run.start()
    assert run.join(5)
    assert hwp.calls and hwp.calls[0][:2] == ('abs', 45.0) and hwp.calls[0][2] == token
    assert get_resource_registry().holder('rotator:HWP') is None      # released at stop
    bad = SmartPipelineDocument(Workflow('p', [Live('cam', detector='Camera'), Gate('once', 'once', inputs=['cam']),
                                               Action('turn', 'rotate', {'rotator': 'Gone', 'position': 1.0},
                                                      inputs=['once.fired'])]))
    with pytest.raises(ArmError, match="no rotator 'Gone'"):
        Engine(bad, source=ArrayFrameSource([]), steps=FunctionStepRunner({}), actions=backend,
               mode=RunMode.VALIDATE).arm()
