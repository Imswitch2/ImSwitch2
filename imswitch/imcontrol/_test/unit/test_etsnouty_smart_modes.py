import json
import os
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.SmartMicroscopyModeService import (
    ApplyResult,
    PreflightResult,
)
import imswitch.imcontrol.controller.controllers.EtSnoutyController as etsnouty_module
from imswitch.imcontrol.controller.controllers.EtSnoutyController import EtSnoutyController
from imswitch.imcontrol.model.EtSnoutyPaths import (
    ETSNOUTY_ROOT_ENV,
    getEtSnoutyPath,
    getEtSnoutyRoot,
)


pytestmark = pytest.mark.nohardware


class _NullLogger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _Signal:
    def __init__(self):
        self.emitted = []
        self.connected = []
        self.disconnected = []

    def emit(self, *args):
        self.emitted.append(args)

    def connect(self, slot):
        self.connected.append(slot)

    def disconnect(self, slot):
        self.disconnected.append(slot)
        if slot not in self.connected:
            raise RuntimeError('slot was not connected')
        self.connected.remove(slot)


class _FakeLaser:
    def __init__(self):
        self.enabled = []

    def setEnabled(self, enabled):
        self.enabled.append(enabled)


class _FakeLasersManager:
    def __init__(self):
        self.laser = _FakeLaser()
        self.calls = []

    def execOn(self, name, callback):
        self.calls.append(name)
        callback(self.laser)


class _Check:
    def __init__(self, checked):
        self._checked = checked

    def isChecked(self):
        return self._checked


class _Button:
    def __init__(self):
        self.texts = []

    def setText(self, text):
        self.texts.append(text)


class _FakeWidget:
    def __init__(self, endless=False):
        self.endlessScanCheck = _Check(endless)
        self.initiateButton = _Button()


class _FakeSmartModeService:
    def __init__(self, roles=None, results=None, preflightResult=None):
        self.roles = roles or {
            'scouting': 'Scout mode',
            'event': 'Event mode',
        }
        self.results = results or {}
        self.preflightResult = preflightResult or PreflightResult(ok=True)
        self.applyCalls = []
        self.preflightCalls = []

    def resolveMode(self, workflowName, role):
        assert workflowName == 'EtSnouty'
        return self.roles.get(role)

    def preflight(self, workflowName, roles=None):
        assert workflowName == 'EtSnouty'
        self.preflightCalls.append(list(roles or []))
        return self.preflightResult

    def applyRole(self, workflowName, role):
        assert workflowName == 'EtSnouty'
        self.applyCalls.append((workflowName, role))
        if role in self.results:
            return self.results[role]
        return ApplyResult(
            applied=True,
            ok=True,
            modeName=self.roles.get(role),
            warnings=[],
            failedComponents=[],
        )


def _make_controller(enabled=True, roles=None, results=None, preflightResult=None, endless=False):
    controller = EtSnoutyController.__new__(EtSnoutyController)
    controller._setupInfo = SimpleNamespace(
        smartMicroscopyModeSwitchingEnabled={'EtSnouty': enabled} if enabled is not None else None
    )
    controller._smartModeService = _FakeSmartModeService(
        roles=roles,
        results=results,
        preflightResult=preflightResult,
    )
    controller._commChannel = SimpleNamespace(
        sigSetConfig=_Signal(),
        sigSetVisibleLayers=_Signal(),
        sigRunScanTriggerScopePLSRMulticolor=_Signal(),
        sigToggleBlockScanWidget=_Signal(),
        sigScanEnded=_Signal(),
        sigClockWidefield=_Signal(),
        sigUpdateImage=_Signal(),
        sigInitiateEtSnouty=_Signal(),
        sigRemoveItemFromVb=_Signal(),
        sigAddItemToVb=_Signal(),
    )
    controller._master = SimpleNamespace(lasersManager=_FakeLasersManager())
    controller._widget = _FakeWidget(endless=endless)
    controller._logger = _NullLogger()
    controller._EtSnoutyController__logger = _NullLogger()
    controller._EtSnoutyController__detLog = {}
    controller._EtSnoutyController__running = False
    controller.detectorFast = 'FastCamera'
    controller.laserFast = 'FastLaser'
    controller.ClockWidefield = False
    controller.updateScatter = lambda *args, **kwargs: None
    controller.resetParamVals = lambda: None
    return controller


def test_etsnouty_default_paths_are_not_absolute_lab_paths():
    assert 'C:/Users/Snouty' not in etsnouty_module._logsDir
    assert 'C:/Users/Snouty' not in etsnouty_module._paramsDir
    assert 'C:/Users/Snouty' not in etsnouty_module._binaryMask


def test_etsnouty_root_can_be_configured(monkeypatch, tmp_path):
    monkeypatch.setenv(ETSNOUTY_ROOT_ENV, str(tmp_path))

    assert getEtSnoutyRoot() == str(tmp_path)
    assert getEtSnoutyPath('analysis_pipelines') == os.path.join(
        str(tmp_path), 'analysis_pipelines'
    )


def test_etsnouty_millis_is_portable_monotonic():
    first = etsnouty_module._millis()
    second = etsnouty_module._millis()

    assert isinstance(first, float)
    assert second >= first


def test_set_config_uses_smart_modes_when_enabled():
    controller = _make_controller(enabled=True)

    assert controller.setConfig(widefield=True) is True
    assert controller.setConfig(widefield=False) is True

    assert controller._smartModeService.applyCalls == [
        ('EtSnouty', 'scouting'),
        ('EtSnouty', 'event'),
    ]
    assert controller._commChannel.sigSetConfig.emitted == []
    assert controller._commChannel.sigSetVisibleLayers.emitted == [(('FastCamera',),)]
    assert controller._master.lasersManager.laser.enabled == [True, False]
    assert controller._EtSnoutyController__detLog['smart_mode_scouting'] == 'Scout mode'
    assert controller._EtSnoutyController__detLog['smart_mode_event'] == 'Event mode'


def test_set_config_does_not_enable_fast_laser_when_scouting_mode_fails():
    controller = _make_controller(
        enabled=True,
        roles={
            'scouting': 'Scout mode',
            'event': 'Event mode',
            'idle': 'Idle mode',
        },
        results={
            'scouting': ApplyResult(
                applied=True,
                ok=False,
                modeName='Scout mode',
                warnings=['FlipMirror: failed to move'],
                failedComponents=['FlipMirror'],
            )
        },
    )

    assert controller.setConfig(widefield=True) is False

    assert controller._smartModeService.applyCalls == [
        ('EtSnouty', 'scouting'),
        ('EtSnouty', 'idle'),
    ]
    assert controller._master.lasersManager.laser.enabled == [False]
    assert controller._commChannel.sigSetVisibleLayers.emitted == []


def test_set_config_keeps_legacy_sigsetconfig_when_flag_disabled():
    controller = _make_controller(enabled=False)

    assert controller.setConfig(widefield=True) is True
    assert controller.setConfig(widefield=False) is True

    assert controller._smartModeService.applyCalls == []
    assert controller._commChannel.sigSetConfig.emitted == [
        ('Widefield imaging',),
        ('Light sheet imaging',),
    ]
    assert controller._commChannel.sigSetVisibleLayers.emitted == [(('FastCamera',),)]


def test_start_preflight_requires_scouting_and_event_roles():
    controller = _make_controller(
        enabled=True,
        roles={'scouting': 'Scout mode'},
    )

    assert controller._preflightSmartModeRolesForStart() is False
    assert controller._smartModeService.preflightCalls == []
    assert 'smart_mode_missing_required_roles_error' in controller._EtSnoutyController__detLog


def test_start_preflight_includes_optional_resume_and_idle_roles_when_configured():
    controller = _make_controller(
        enabled=True,
        roles={
            'scouting': 'Scout mode',
            'event': 'Event mode',
            'resume': 'Resume mode',
            'idle': 'Idle mode',
        },
    )

    assert controller._preflightSmartModeRolesForStart() is True
    assert controller._smartModeService.preflightCalls == [
        ['scouting', 'event', 'resume', 'idle'],
    ]


def test_run_slow_scan_applies_event_once_before_triggering_scan():
    controller = _make_controller(enabled=True)

    assert controller.runSlowScan() is True

    assert controller._smartModeService.applyCalls == [('EtSnouty', 'event')]
    assert controller._commChannel.sigRunScanTriggerScopePLSRMulticolor.emitted == [()]
    assert controller._master.lasersManager.laser.enabled == [False, True]


class _ScanPanel:
    """A scan panel that records what the event left on the channel when it
    was asked to run."""

    def __init__(self, channel, error=None):
        self.channel = channel
        self.error = error
        self.runs = []

    def runScanExternal(self, recalculateSignals, isNonFinalPartOfSequence):
        if self.error:
            raise self.error
        self.runs.append((recalculateSignals, isNonFinalPartOfSequence,
                          self.channel.sharedAttrs[etsnouty_module.SCAN_EVENT_KEY]))


class _SharedAttrs(dict):
    pass


def _with_scan_panel(controller, error=None):
    """The controller with a Scan panel chosen as its slow scan (M6)."""
    channel = controller._commChannel
    channel.sharedAttrs = _SharedAttrs()
    panel = _ScanPanel(channel, error)
    channel.asked = []
    channel.getScanSource = lambda key: channel.asked.append(key) or panel
    channel.getScanSourceNames = lambda: ['Scan']
    sources = []
    controller._widget.setSlowScanSources = sources.extend
    controller._widget.getSlowScanSource = lambda: 'Scan'
    controller._EtSnoutyController__detLog.update(
        fastscan_x_center=12.5, fastscan_y_center=40.0, coord_transf_start='03s000001us')
    controller._EtSnoutyController__pipelinename = 'cd_vesicle_prox'
    return panel, sources


def test_the_slow_scan_can_be_a_scan_panels_program():
    """An event runs the chosen panel's program once, and the run carries the
    event; the TriggerScope routine is not started."""
    controller = _make_controller(enabled=True)
    panel, _ = _with_scan_panel(controller)

    assert controller.runSlowScan() is True

    assert controller._commChannel.asked == ['Scan']
    assert controller._commChannel.sigRunScanTriggerScopePLSRMulticolor.emitted == []
    [(recalculate, nonFinal, event)] = panel.runs
    assert (recalculate, nonFinal) == (True, False)
    event = json.loads(event)
    assert event['workflow'] == 'EtSnouty' and event['scan_source'] == 'Scan'
    assert event['position_px'] == [12.5, 40.0] and event['detector'] == 'FastCamera'
    assert event['pipeline'] == 'cd_vesicle_prox' and event['detected'] == '03s000001us'
    # The run ends: later runs are not this event's.
    controller._acceptTriggeredScanEnded()
    assert controller._commChannel.sharedAttrs[etsnouty_module.SCAN_EVENT_KEY] == ''



def test_an_event_at_numpy_coordinates_still_runs_its_scan():
    """A pipeline's coordinates are numpy scalars (``peak_local_max`` gives
    int64, an image float32): json refused them, so the event's program
    never ran (review 2026-10-16)."""
    import numpy as np
    controller = _make_controller(enabled=True)
    panel, _ = _with_scan_panel(controller)
    controller._EtSnoutyController__detLog.update(
        fastscan_x_center=np.int64(12), fastscan_y_center=np.float32(40.5))
    assert controller.runSlowScan() is True
    [(_recalculate, _nonFinal, event)] = panel.runs
    assert json.loads(event)['position_px'] == [12, 40.5]


def test_a_scan_panel_that_cannot_run_fails_the_event_and_leaves_no_event_behind():
    controller = _make_controller(enabled=True)
    _with_scan_panel(controller, error=RuntimeError('No controller exposes runScanExternal'))

    assert controller.runSlowScan() is False

    assert controller._triggeredScanInFlight is False
    assert controller._commChannel.sharedAttrs[etsnouty_module.SCAN_EVENT_KEY] == ''


def test_the_routine_stays_the_default_and_the_panels_are_offered():
    controller = _make_controller(enabled=True)
    _, sources = _with_scan_panel(controller)
    controller._refreshSlowScanSources()
    assert sources == [etsnouty_module.SLOW_SCAN_ROUTINE, 'Scan']
    controller._widget.getSlowScanSource = lambda: etsnouty_module.SLOW_SCAN_ROUTINE
    assert controller._slowScanSourceKey() is None
    assert controller.runSlowScan() is True
    assert controller._commChannel.sigRunScanTriggerScopePLSRMulticolor.emitted == [()]


def test_run_slow_scan_does_not_trigger_scan_when_event_mode_fails():
    controller = _make_controller(
        enabled=True,
        results={
            'event': ApplyResult(
                applied=True,
                ok=False,
                modeName='Event mode',
                warnings=['FlipMirror: failed to move'],
                failedComponents=['FlipMirror'],
            )
        },
        endless=False,
    )

    assert controller.runSlowScan() is False

    assert controller._smartModeService.applyCalls == [('EtSnouty', 'event')]
    assert controller._commChannel.sigRunScanTriggerScopePLSRMulticolor.emitted == []
    assert controller._master.lasersManager.laser.enabled == [False, False]


def test_run_slow_scan_recovers_to_idle_when_event_mode_fails():
    controller = _make_controller(
        enabled=True,
        roles={
            'scouting': 'Scout mode',
            'event': 'Event mode',
            'idle': 'Idle mode',
        },
        results={
            'event': ApplyResult(
                applied=True,
                ok=False,
                modeName='Event mode',
                warnings=['FlipMirror: failed to move'],
                failedComponents=['FlipMirror'],
            )
        },
        endless=False,
    )

    assert controller.runSlowScan() is False

    assert controller._smartModeService.applyCalls == [
        ('EtSnouty', 'event'),
        ('EtSnouty', 'idle'),
    ]
    assert controller._commChannel.sigRunScanTriggerScopePLSRMulticolor.emitted == []
    assert controller._master.lasersManager.laser.enabled == [False, False]


def test_run_slow_scan_stops_instead_of_resuming_when_event_mode_fails_in_endless_mode():
    controller = _make_controller(
        enabled=True,
        roles={
            'scouting': 'Scout mode',
            'event': 'Event mode',
            'idle': 'Idle mode',
        },
        results={
            'event': ApplyResult(
                applied=True,
                ok=False,
                modeName='Event mode',
                warnings=['FlipMirror: failed to move'],
                failedComponents=['FlipMirror'],
            )
        },
        endless=True,
    )

    assert controller.runSlowScan() is False

    assert controller._smartModeService.applyCalls == [
        ('EtSnouty', 'event'),
        ('EtSnouty', 'idle'),
    ]
    assert controller._commChannel.sigRunScanTriggerScopePLSRMulticolor.emitted == []
    assert controller._commChannel.sigUpdateImage.connected == []
    assert controller._commChannel.sigToggleBlockScanWidget.emitted == [(True,)]
    assert controller._commChannel.sigInitiateEtSnouty.emitted == [(False,)]
    assert controller._widget.initiateButton.texts == ['Initiate']
    assert controller._EtSnoutyController__running is False
    assert controller._master.lasersManager.laser.enabled == [False, False]


def test_continue_fast_modality_prefers_resume_role_when_configured():
    controller = _make_controller(
        enabled=True,
        roles={
            'scouting': 'Scout mode',
            'event': 'Event mode',
            'resume': 'Resume mode',
        },
        endless=True,
    )

    controller.continueFastModality()

    assert controller._smartModeService.applyCalls == [('EtSnouty', 'resume')]
    assert controller._commChannel.sigUpdateImage.connected == [controller.runPipeline]
    assert controller._widget.initiateButton.texts == ['Stop']
    assert controller._EtSnoutyController__running is True


def test_idle_role_is_applied_only_when_configured():
    controller = _make_controller(
        enabled=True,
        roles={
            'scouting': 'Scout mode',
            'event': 'Event mode',
            'idle': 'Idle mode',
        },
    )

    assert controller._applySmartModeRoleIfConfigured('idle') is True

    assert controller._smartModeService.applyCalls == [('EtSnouty', 'idle')]
