"""What SimpleMoNaLISA owns -- screening lasers, the camera trigger, the
scan's geometry -- is kept until it is really handed back (review
2026-10-17: two P1s and a P2 in the cloak).

- A Live start whose rollback cannot switch a laser off lost track of it:
  Stop returned success with the laser on.
- A page switch refused while acquiring released the screening trigger
  override first, so the camera left external_frame under the acquisition.
- Back from Advanced, a centre offset was parsed away and the next mirror
  wrote every centre as 0, moving the acquisition without a word.
"""
import pytest

from imswitch.imcontrol.controller.basecontrollers import ComponentStateApplyMode

from .test_scan_cloak_contract import CloakRig, _monalisaSetupWithPowerMode


def _separated_rig(monkeypatch, refused):
    """The MoNaLISA cloak with laser writes in ``refused`` -- (name, on) --
    raising, two screening lasers A and B, and the camera live."""
    rig = CloakRig('SimpleMoNaLISA', _monalisaSetupWithPowerMode('separated'))
    workflow = rig.channel.liveScreeningWorkflow
    lasers = {'A': False, 'B': False}

    def setActive(name, active):
        if (name, bool(active)) in refused:
            raise OSError(f'{name} did not take {"ON" if active else "OFF"}')
        lasers[name] = bool(active)
    monkeypatch.setattr(workflow, 'get_laser_active', lambda name: lasers[name])
    monkeypatch.setattr(workflow, 'set_laser_active', setActive)
    trigger = {}
    monkeypatch.setattr(workflow, 'get_trigger_setting', lambda n: trigger.get(n, 'internal'))
    monkeypatch.setattr(workflow, 'set_trigger_mode', lambda n, v: trigger.__setitem__(n, v))
    monkeypatch.setattr(workflow, 'restore_trigger_setting',
                        lambda n, v: trigger.__setitem__(n, v))
    monkeypatch.setattr(rig.scan, '_missingLiveViewDetectors', lambda: [])
    monkeypatch.setattr(rig.scan, '_selectedLiveSources', lambda: {'screening'})
    return rig, lasers


def test_a_failed_live_start_keeps_the_laser_it_could_not_switch_off(qtbot, monkeypatch):
    """A switches on, B refuses, A's rollback OFF fails: A stays Simple's,
    screening stays active, and Stop -- or close -- switches it off."""
    refused = {('B', True), ('A', False)}
    rig, lasers = _separated_rig(monkeypatch, refused)
    try:
        monkeypatch.setattr(rig.scan, '_screeningLaserTargets', lambda sources=None: {'A', 'B'})
        rig.scan._onScreeningRequested()
        assert lasers == {'A': True, 'B': False}
        assert rig.scan.__dict__['_activeScreeningLasers'] == {'A'}
        assert rig.scan.__dict__['_screeningActive'] is True
        assert not rig.scan._stopScreening()         # A still refuses: still tracked
        assert rig.scan.__dict__['_activeScreeningLasers'] == {'A'}
        refused.clear()
        assert rig.scan._stopScreening()
        assert lasers == {'A': False, 'B': False}
        assert rig.scan.__dict__['_screeningActive'] is False
    finally:
        refused.clear()
        rig.close()


def test_close_switches_off_a_laser_a_failed_live_left_on(qtbot, monkeypatch):
    refused = {('B', True), ('A', False)}
    rig, lasers = _separated_rig(monkeypatch, refused)
    monkeypatch.setattr(rig.scan, '_screeningLaserTargets', lambda sources=None: {'A', 'B'})
    rig.scan._onScreeningRequested()
    assert lasers['A'] is True
    refused.clear()
    rig.close()
    assert lasers == {'A': False, 'B': False}


def test_a_failed_change_of_the_live_set_keeps_what_may_be_on(qtbot, monkeypatch):
    """Screening with A; B is added, switches on but reads back off, and its
    rollback OFF fails: B may be on, so A and B both stay tracked and Stop
    switches both off."""
    refused = set()
    rig, lasers = _separated_rig(monkeypatch, refused)
    try:
        targets = {'A'}
        monkeypatch.setattr(rig.scan, '_screeningLaserTargets',
                            lambda sources=None: set(targets))
        rig.scan._onScreeningRequested()
        assert lasers == {'A': True, 'B': False}
        readback = {'B': False}                      # B reports off whatever it is
        monkeypatch.setattr(rig.channel.liveScreeningWorkflow, 'get_laser_active',
                            lambda name: readback.get(name, lasers[name]))
        refused.add(('B', False))
        targets.add('B')
        rig.scan._onLiveSourceToggled('another', True)
        assert lasers == {'A': True, 'B': True}      # B is on, nobody was told
        assert rig.scan.__dict__['_activeScreeningLasers'] == {'A', 'B'}
        assert rig.scan.__dict__['_screeningActive'] is True
        refused.clear()
        readback.clear()
        assert rig.scan._stopScreening()
        assert lasers == {'A': False, 'B': False}
    finally:
        refused.clear()
        rig.close()


def test_a_switch_refused_while_acquiring_leaves_the_camera_trigger_alone(qtbot, monkeypatch):
    rig = CloakRig('SimpleMoNaLISA', _monalisaSetupWithPowerMode('separated'))
    busy = {'now': False}
    try:
        state = {'Camera': 'internal'}
        workflow = rig.channel.liveScreeningWorkflow
        monkeypatch.setattr(workflow, 'get_trigger_setting', lambda n: state[n])
        monkeypatch.setattr(workflow, 'set_trigger_mode', lambda n, v: state.__setitem__(n, v))
        monkeypatch.setattr(workflow, 'restore_trigger_setting',
                            lambda n, v: state.__setitem__(n, v))
        rig.scan._ensureSimpleCameraTriggerOverride(('Camera',), 'internal')
        rig.scan._beginAcquisitionTriggerOverride(requireLiveView=False)
        assert state['Camera'] == 'external_frame'
        snapshots = (dict(rig.scan.__dict__['_screeningTriggerSnapshot']),
                     dict(rig.scan.__dict__['_acquisitionTriggerSnapshot']))
        rig.view.setRunning(True)
        busy['now'] = True
        monkeypatch.setattr(rig.scan, '_isBusy', lambda: busy['now'])
        rig.panel.advancedButton.click()
        assert rig.scan.scanPage() == 'simple' and rig.panel.page() == 'simple'
        assert 'Stop the scan before switching panels' in rig.panel.noteLabel.text()
        assert state['Camera'] == 'external_frame'
        assert (rig.scan.__dict__['_screeningTriggerSnapshot'],
                rig.scan.__dict__['_acquisitionTriggerSnapshot']) == snapshots
    finally:
        busy['now'] = False
        rig.close()


def _centre_of(rig, device):
    analog = rig.scan._readBackendWidget()[0]
    return analog['axis_centerpos'][analog['target_device'].index(device)]


@pytest.mark.parametrize('axis', ['X', 'Y'])
def test_a_centre_offset_from_advanced_is_kept_not_reset(qtbot, axis):
    rig = CloakRig('SimpleMoNaLISA')
    try:
        device = rig.limits().axis_device(axis)
        assert rig.scan.setScanPage('advanced')
        rig.panel.backend.setScanCenterPos(device, 4.0)
        assert _centre_of(rig, device) == 4.0
        assert rig.scan.setScanPage('simple')
        assert not rig.scan.cloakOwnsBackend()          # shown as Advanced / custom
        assert 'centred at 4' in rig.scan.profileBinding().reason
        assert _centre_of(rig, device) == 4.0          # the backend keeps it
    finally:
        rig.close()


@pytest.mark.parametrize('axis', ['X', 'Y', 'Z'])
def test_the_model_refuses_any_centre_it_cannot_hold(axis):
    from imswitch.imcontrol.model.scan_cloak import PlanNotRepresentable
    from imswitch.imcontrol.model.simple_monalisa_scan import dicts_to_plan, plan_to_dicts
    rig = CloakRig('SimpleMoNaLISA')
    try:
        limits, plan = rig.limits(), rig.scan.acquisitionPlan()
        analog, digital = plan_to_dicts(plan, limits)
        index = analog['target_device'].index(limits.axis_device(axis))
        analog['axis_centerpos'][index] = -2.5
        with pytest.raises(PlanNotRepresentable, match=f'{axis} is centred at -2.5'):
            dicts_to_plan(analog, digital, limits, reference_plan=plan)
    finally:
        rig.close()


def test_a_saved_scan_with_a_centre_offset_loads_keeping_it(qtbot):
    source = CloakRig('SimpleMoNaLISA')
    try:
        state = source.scan.getComponentState()
    finally:
        source.close()
    device = None
    target = CloakRig('SimpleMoNaLISA')
    try:
        device = target.limits().axis_device('X')
        analog = state['analogParameterDict']
        analog['axis_centerpos'][analog['target_device'].index(device)] = 4.0
        target.scan.applyComponentState(state, applyMode=ComponentStateApplyMode.STARTUP_RESTORE)
        assert not target.scan.cloakOwnsBackend()
        assert _centre_of(target, device) == 4.0
    finally:
        target.close()
