"""Replay apply layer: the one path from a recording to the hardware.

The panel controllers and managers here are spies.  The tests assert the
red-zone properties as negatives: nothing is called before Apply (or after
Cancel), emission is never switched on, stages and rotators never move
without the opt-in, an out-of-range value never reaches a setter, and a
failing setter is recorded for that value without stopping the rest or
escaping the menu slot.
"""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.model import SharedAttributes, dirtools
from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode
from imswitch.imcontrol.controller.controllers.ReplayController import ReplayController
from imswitch.imcontrol.model import replay
from imswitch.imcontrol.model.managers.RecordingManager import HDF5Storer, ZarrStorer


# ------------------------------------------------------------------ fakes

class _Managers(dict):
    def __iter__(self):
        return iter(list(self.items()))


class _Laser:
    def __init__(self, wavelength, lo=0, hi=200, binary=False):
        self.wavelength, self.valueRangeMin, self.valueRangeMax = wavelength, lo, hi
        self.valueRangeStep, self.valueUnits, self.isBinary = 1, 'mW', binary
        self.isModulated = False
        self.calls = []

    def setValue(self, value):
        self.calls.append(('setValue', value))

    def setEnabled(self, enabled):
        self.calls.append(('setEnabled', enabled))


class _Positioner:
    def __init__(self, axes):
        self.axes, self.forScanning, self.forPositioning = list(axes), False, True
        self.calls = []

    def setPosition(self, value, axis):
        self.calls.append(('setPosition', axis, value))

    def move(self, value, axis):
        self.calls.append(('move', axis, value))


class _Param:
    def __init__(self, value, units='ms'):
        self.value, self.valueUnits, self.editable, self.group = value, units, True, ''


class _Detector:
    model, fullShape, supportedBinnings, forAcquisition = 'mock', (512, 512), [1, 2], True
    configOwnedParameters = frozenset()

    def __init__(self):
        self.parameters = {'exposure': _Param(10.0)}
        self.calls = []

    def setParameter(self, name, value):
        self.calls.append(('setParameter', name, value))

    def setBinning(self, value):
        self.calls.append(('setBinning', value))


class _Rotator:
    def __init__(self):
        self.calls = []

    def move_abs(self, value):
        self.calls.append(('move_abs', value))


class _LaserPanel:
    def __init__(self, master, refuse=False, raises=None):
        self.master, self.refuse, self.raises, self.calls = master, refuse, raises, []
        self._widget = SimpleNamespace(isLaserActive=lambda name: False)

    def setLaserValue(self, name, value):
        self.calls.append(('setLaserValue', name, value))
        if self.raises:
            raise self.raises
        if self.refuse:
            return False
        self.master.lasersManager[name].setValue(value)
        return True

    def setLaserActive(self, name, active):  # must never be called by replay
        self.calls.append(('setLaserActive', name, active))
        self.master.lasersManager[name].setEnabled(active)


class _PositionerPanel:
    def __init__(self, master):
        self.master, self.calls = master, []

    def setPositioner(self, name, axis, value):
        self.calls.append(('setPositioner', name, axis, value))
        self.master.positionersManager[name].setPosition(value, axis)


class _RotatorPanel:
    def __init__(self, master):
        self.master, self.calls = master, []

    def setRotatorPosition(self, name, value):
        self.calls.append(('setRotatorPosition', name, value))
        self.master.rotatorsManager[name].move_abs(value)


class _SettingsPanel:
    def __init__(self, master, raises=None):
        self.master, self.raises, self.calls = master, raises, []

    def setDetectorBinning(self, name, value):
        self.calls.append(('binning', name, value))
        if self.raises:
            raise self.raises
        self.master.detectorsManager[name].setBinning(value)

    def setDetectorROI(self, name, start, shape):
        self.calls.append(('roi', name, start, shape))

    def setDetectorParameterExact(self, name, param, value):
        self.calls.append(('param', name, param, value))
        self.master.detectorsManager[name].setParameter(param, value)


class _ScanPanel:
    def __init__(self):
        self._analogParameterDict = {'target_device': ['Stage'], 'axis_length': [1.0]}
        self._digitalParameterDict = {'target_device': ['488'], 'sequence_time': 1e-5}
        self.positioners = {'Stage': None}
        self.TTLDevices = {'488': None, '561': None}
        self.calls = []

    def getComponentState(self):
        return {'analogParameterDict': dict(self._analogParameterDict),
                'digitalParameterDict': dict(self._digitalParameterDict),
                'positionersScan': ['Stage']}

    def applyComponentState(self, state, *, applyMode):
        self.calls.append((state, applyMode))
        return []


def _master():
    return SimpleNamespace(
        lasersManager=_Managers({'488': _Laser(488), '561': _Laser(561, hi=100)}),
        positionersManager=_Managers({'Stage': _Positioner(['X', 'Y'])}),
        detectorsManager=_Managers({'Cam': _Detector()}),
        rotatorsManager=_Managers({'hwp': _Rotator()}),
        flipMirrorsManager=_Managers(),
        pulseGeneratorManager=None,
    )


class _Dialog:
    """Stands in for ReplaySettingsDialog: decides, records what it saw."""

    def __init__(self, decide=None, accept=True, raises=None):
        self.decide, self.accept, self.raises = decide, accept, raises
        self.plans, self.results = [], []
        self.callsAtAsk = None

    def askToApply(self, plan, *, canRemember=False):
        self.plans.append(plan)
        self.callsAtAsk = _hardwareCalls(self.master)
        if self.raises:
            raise self.raises
        if self.decide:
            self.decide(plan)
        return self.accept

    def rememberRequested(self):
        return False

    def showResult(self, result):
        self.results.append(result)


def _hardwareCalls(master):
    calls = []
    for attr in ('lasersManager', 'positionersManager', 'detectorsManager', 'rotatorsManager'):
        for name, manager in getattr(master, attr):
            calls += [(name,) + c for c in manager.calls]
    return calls


@pytest.fixture
def rig(qapp):
    master = _master()
    commChannel = SimpleNamespace(sharedAttrs=SharedAttributes())
    dialog = _Dialog()
    dialog.master = master
    setupInfo = SimpleNamespace(to_dict=lambda: {'lasers': {'488': {}}}, scan=None,
                                flipMirrors=None)
    controller = ReplayController(setupInfo, commChannel, master, widget=dialog,
                                  factory=None, moduleCommChannel=None)
    panels = {'Laser': _LaserPanel(master), 'Positioner': _PositionerPanel(master),
              'Rotator': _RotatorPanel(master), 'Settings': _SettingsPanel(master),
              'Scan': _ScanPanel()}
    controller.setControllers(panels)
    controller.setSetupName('current.json')
    return SimpleNamespace(controller=controller, master=master, panels=panels,
                           dialog=dialog, attrs=commChannel.sharedAttrs)


FILE = {
    'Laser:488:Value': np.float64(50.0), 'Laser:488:Enabled': np.bool_(True),
    'Laser:561:Value': np.float64(1e6), 'Laser:561:Enabled': np.bool_(True),
    'Positioner:Stage:X:Position': 100.0,
    'Rotator:hwp:Position': 45.0,
    'Detector:Cam:Binning': 2, 'Detector:Cam:Param:exposure': 20.0,
    'ScanStage:target_device': ['Stage'], 'ScanStage:axis_length': [5.0],
    'ScanTTL:target_device': ['488'], 'ScanTTL:sequence_time': 2e-5,
}


def _parsed(rig, attrs=FILE):
    return rig.controller.parse(dict(attrs), sourceFile='/data/secret/run1.h5',
                                dataset='Cam', sourceFormat='HDF5')


# ------------------------------------------------------- nothing before Apply

def test_nothing_reaches_hardware_before_apply_or_after_cancel(rig):
    rig.dialog.accept = False
    result = rig.controller.replayInteractive(_parsed(rig))
    assert result is None
    assert rig.dialog.callsAtAsk == []
    assert _hardwareCalls(rig.master) == []
    assert all(p.calls == [] for p in rig.panels.values())
    assert not any(k[0] == 'replay' for k, _ in rig.attrs)


def test_resolving_and_planning_touch_no_hardware(rig):
    rig.controller.plan(_parsed(rig))
    rig.controller.snapshot()
    rig.controller.publishSetupMetadata()
    assert _hardwareCalls(rig.master) == []


# ------------------------------------------------------- the safety rules

def test_apply_never_switches_emission_never_moves_and_skips_out_of_range(rig):
    result = rig.controller.replayInteractive(_parsed(rig))
    calls = _hardwareCalls(rig.master)
    assert ('488', 'setValue', 50.0) in calls
    assert not any(c[1] == 'setEnabled' and c[2] for c in calls)   # decision 1
    assert not any(c[1] in ('setPosition', 'move', 'move_abs') for c in calls)  # decision 2
    assert ('561', 'setValue', 1e6) not in calls                     # decision 3
    assert not any(c[0] == '561' for c in calls)
    assert rig.panels['Laser'].calls == [('setLaserValue', '488', 50.0)]
    skipped = {(i, k): r for i, k, r in result.skipped}
    assert 'Outside' in skipped[('laser:561', 'Value')]
    assert 'Moves not enabled' in skipped[('positioner:Stage:X', 'Position')]
    assert rig.dialog.results == [result]


def test_moves_happen_only_with_the_opt_in(rig):
    def allowMoves(plan):
        plan.allowMoves = True
    rig.dialog.decide = allowMoves
    rig.controller.replayInteractive(_parsed(rig))
    calls = _hardwareCalls(rig.master)
    assert ('Stage', 'setPosition', 'X', 100.0) in calls
    assert ('hwp', 'move_abs', 45.0) in calls


def test_clamp_applies_the_range_limit_not_the_stored_value(rig):
    def clamp(plan):
        value = next(v for v in plan.item('laser:561').values if v.key == 'Value')
        replay.setValueAction(value, replay.CLAMP)
    rig.dialog.decide = clamp
    rig.controller.replayInteractive(_parsed(rig))
    assert ('561', 'setValue', 100.0) in _hardwareCalls(rig.master)


def test_apply_layer_refuses_an_out_of_range_action_even_if_one_slips_through(rig):
    plan = rig.controller.plan(_parsed(rig))
    action = replay.ReplayAction('laser:561', replay.LASER, '561', 'Value', 1e6)
    with pytest.raises(RuntimeError, match='outside'):
        rig.controller._applyOne(action, plan)
    move = replay.ReplayAction('positioner:Stage:X', replay.POSITIONER, 'Stage:X', 'Position', 1.0)
    with pytest.raises(RuntimeError, match='moves not enabled'):
        rig.controller._applyOne(move, plan)
    assert _hardwareCalls(rig.master) == []


def test_scan_parameters_are_applied_once_and_atomically(rig):
    rig.controller.replayInteractive(_parsed(rig))
    scanCalls = rig.panels['Scan'].calls
    assert len(scanCalls) == 1
    state, mode = scanCalls[0]
    assert mode is ComponentStateApplyMode.STARTUP_RESTORE
    assert state['analogParameterDict'] == {'target_device': ['Stage'], 'axis_length': [5.0]}
    assert state['digitalParameterDict']['sequence_time'] == 2e-5


def test_failures_are_recorded_per_value_and_the_rest_still_applies(rig):
    rig.panels['Settings'].raises = RuntimeError('camera busy')
    rig.panels['Laser'].refuse = True
    result = rig.controller.replayInteractive(_parsed(rig))
    failed = {(i, k): r for i, k, r in result.failed}
    assert failed[('detector:Cam', 'Binning')] == 'camera busy'
    assert 'refused' in failed[('laser:488', 'Value')]
    applied = {(i, k) for i, k, _, _ in result.applied}
    assert ('detector:Cam', 'Param:exposure') in applied
    assert ('scan:parameters', 'parameters') in applied


def test_a_dialog_error_never_escapes_the_menu_slot(rig):
    rig.dialog.raises = RuntimeError('boom')
    result = rig.controller.replayInteractive(_parsed(rig))
    assert result.failed and 'boom' in result.failed[0][2]
    assert _hardwareCalls(rig.master) == []


def test_fully_matching_file_is_still_confirmed(rig):
    rig.dialog.accept = False
    rig.controller.replayInteractive(_parsed(rig, {'Laser:488:Value': 5.0}))
    assert len(rig.dialog.plans) == 1
    assert _hardwareCalls(rig.master) == []


# ------------------------------------------------------- provenance

def test_provenance_is_published_after_apply_with_file_name_only(rig):
    rig.controller.replayInteractive(_parsed(rig))
    attrs = dict(rig.attrs)
    assert attrs[('replay', 'source_file')] == 'run1.h5'
    assert '/data/secret' not in json.dumps({':'.join(k): v for k, v in attrs.items()},
                                            default=str)
    mapping = json.loads(attrs[('replay', 'mapping')])
    entry = next(m for m in mapping if m['id'] == 'laser:561')
    assert 'Value' in entry['skipped']


def test_no_provenance_when_nothing_was_applied(rig):
    rig.dialog.decide = lambda plan: [plan.retarget(i.id, None) for i in plan.items
                                      if i.replayable]
    rig.controller.replayInteractive(_parsed(rig))
    assert not any(k[0] == 'replay' for k, _ in rig.attrs)


def test_setup_metadata_is_published(rig):
    rig.controller.publishSetupMetadata()
    attrs = dict(rig.attrs)
    assert attrs[('metadata', 'schema_version')] == replay.METADATA_SCHEMA_VERSION
    assert attrs[('setup', 'name')] == 'current.json'
    assert attrs[('setup', 'hash')].startswith('sha256:')
    inventory = attrs[('setup', 'inventory')]
    assert inventory['lasers']['561']['valueRangeMax'] == 100
    assert inventory['scan']['ttlDevices'] == ['488', '561']


# ------------------------------------------------------- unattended (API)

@pytest.mark.parametrize('storer,suffix', [(HDF5Storer, 'snap_Cam.h5'), (ZarrStorer, 'snap.zarr')])
def test_unattended_replay_applies_only_exact_in_range_values(rig, tmp_path, storer, suffix):
    class _DetMan:
        def __getitem__(self, name):
            return SimpleNamespace(dtype=np.dtype(np.uint16), pixelSizeUm=[1.0, 0.1, 0.1])

    attrs = dict(FILE, **{'Laser:Foreign:Value': 1.0})
    storer(str(tmp_path / 'snap'), _DetMan()).snap(
        {'Cam': np.zeros((8, 8), np.uint16)}, {'Cam': attrs})
    out = rig.controller.replaySettingsFromFile(str(tmp_path / suffix))
    applied = {(a['item'], a['key']) for a in out['result']['applied']}
    assert ('laser:488', 'Value') in applied
    assert ('laser:561', 'Value') not in applied
    assert ('laser:Foreign', 'Value') not in applied
    calls = _hardwareCalls(rig.master)
    assert not any(c[1] in ('setPosition', 'move', 'move_abs', 'setEnabled') for c in calls)
    assert json.dumps(out, default=str)  # JSON-able for script callers

    described = rig.controller.planReplayFromFile(str(tmp_path / suffix))
    assert {i['id'] for i in described['items']} >= {'laser:488', 'laser:Foreign'}


# ------------------------------------------------------- remembered mapping

def test_remembered_mapping_prefills_the_next_dialog(rig, tmp_path, monkeypatch):
    monkeypatch.setattr(dirtools.UserFileDirs, 'Root', str(tmp_path))
    attrs = {'Laser:Old:Value': 5.0, 'setup:hash': 'sha256:other-rig'}

    def choose(plan):
        plan.retarget('laser:Old', '561')
    rig.dialog.decide = choose
    rig.dialog.rememberRequested = lambda: True
    rig.controller.replayInteractive(_parsed(rig, attrs))
    saved = json.loads((tmp_path / 'imcontrol_replay_mappings.json').read_text())
    assert list(saved.values()) == [{'laser:Old': '561'}]

    rig.dialog.decide, rig.dialog.accept = None, False
    rig.controller.replayInteractive(_parsed(rig, attrs))
    assert rig.dialog.plans[-1].item('laser:Old').target == '561'
