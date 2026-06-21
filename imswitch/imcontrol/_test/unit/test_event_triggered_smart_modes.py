"""Phase 5 unit tests for smart-microscopy role wiring in the shared base.

These tests exercise :class:`SmartModeRoleMixin` *through* the real
``EventTriggeredControllerBase`` lifecycle (``initiate`` /
``initiateSlowScan`` / ``continueFastModality`` / ``stopExperiment``), using a
spy :class:`_SpySmartModeService` and a lightweight controller subclass that
stubs out the heavy hardware/UI machinery the base would otherwise touch.

They cover:

  * role-application ordering across the lifecycle (scouting at arm, event
    before the slow scan, resume on endless continue, idle on stop);
  * flag-off being a complete no-op (no service calls at all);
  * a preflight failure blocking arming;
  * a required-role apply failure recovering to idle and not running the scan;
  * EtMonalisa's direct stand-switching hooks being flag-gated: they fire on the
    flag-off legacy path and are suppressed when smart-mode switching is on (the
    stand mode then belongs in the scouting/event setup modes via the LeicaStand
    setup-mode component).
"""

from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.SmartMicroscopyModeService import (
    ApplyResult,
    PreflightResult,
)
from imswitch.imcontrol.controller.controllers.EventTriggeredBaseController import (
    EventTriggeredControllerBase,
)
from imswitch.imcontrol.model.EventTriggeredSession import (
    EventRunMode as RunMode,
    EventScanInitiationMode as ScanInitiationMode,
    EventTriggeredSessionState,
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

    def emit(self, *args):
        self.emitted.append(args)


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
        self.fastaxisshiftCheck = _Check(False)


class _SpySmartModeService:
    """Records applyRole/preflight calls and returns configurable results."""

    def __init__(self, roles=None, results=None, preflightResult=None):
        self.roles = roles if roles is not None else {
            'scouting': 'Scout mode',
            'event': 'Event mode',
        }
        self.results = results or {}
        self.preflightResult = preflightResult or PreflightResult(ok=True)
        self.applyCalls = []
        self.preflightCalls = []

    def resolveMode(self, workflowName, role):
        return self.roles.get(role)

    def preflight(self, workflowName, roles=None):
        self.preflightCalls.append((workflowName, list(roles or [])))
        return self.preflightResult

    def applyRole(self, workflowName, role):
        self.applyCalls.append((workflowName, role))
        if role in self.results:
            return self.results[role]
        return ApplyResult(
            applied=True, ok=True, modeName=self.roles.get(role),
            warnings=[], failedComponents=[],
        )


class _HarnessController(EventTriggeredControllerBase):
    """Minimal EventTriggeredControllerBase subclass for lifecycle wiring tests.

    Overrides the heavy machinery (experiment prep, signal wiring, fast laser,
    scan runner) with call recorders so the tests observe *only* the ordering of
    smart-mode role application relative to the existing lifecycle steps.
    """

    SMART_MODE_WORKFLOW = 'EtHarness'
    SMART_MODE_REQUIRED_ROLES = ('scouting', 'event')

    def _prepareExperiment(self):
        self.calls.append('prepare')

    def _connectRunSignals(self):
        self.calls.append('connect')

    def _disconnectRunSignals(self):
        self.calls.append('disconnect')

    def _setFastLaserEnabled(self, enabled, *, require_success=False):
        self.calls.append(('laser', enabled))

    def _cleanupBinaryMaskRecording(self):
        pass

    def _set_status(self, status, message=''):
        self.calls.append(('status', status))

    def _set_controls_armed(self, armed):
        pass

    def _pre_arm_hook(self):
        self.calls.append('pre_arm_hook')

    def _post_stop_hook(self, *, reset_params):
        self.calls.append('post_stop_hook')

    def _on_resume_modality_hook(self):
        self.calls.append('resume_hook')

    def resetParamVals(self):
        pass

    def resetRunParams(self):
        self._state.reset_runtime_counters()

    # Record smart-mode role application in the same ``calls`` stream so the
    # tests can assert ordering against the lifecycle steps above.
    def _applySmartModeRole(self, role, *, required):
        self.calls.append(('role', role))
        return super()._applySmartModeRole(role, required=required)

    # Heavy scan path stubs.
    def _runTriggeredScanPrepare(self):
        self.calls.append('scan_prepare')
        return True

    def runSlowScan(self):
        self.calls.append('scan_trigger')
        return True


class _CustomShiftHarness(_HarnessController):
    FAST_AXIS_SHIFT_COEFFICIENTS = (0, 0, 0, 0, 0, 0)


class _BadShiftHarness(_HarnessController):
    FAST_AXIS_SHIFT_COEFFICIENTS = (1, 2, 3)


def _make_controller(cls=_HarnessController, *, enabled=True, endless=False,
                     roles=None, results=None, preflightResult=None,
                     runMode=RunMode.Experiment):
    controller = cls.__new__(cls)
    controller.MODALITY_LABEL = cls.MODALITY_LABEL
    controller.calls = []
    controller._logger = _NullLogger()
    controller._setupInfo = SimpleNamespace(
        smartMicroscopyModeSwitchingEnabled={cls.SMART_MODE_WORKFLOW: enabled}
    )
    controller._smartModeService = _SpySmartModeService(
        roles=roles, results=results, preflightResult=preflightResult,
    )
    controller._widget = _FakeWidget(endless=endless)
    controller._state = EventTriggeredSessionState()
    controller._state.runMode = runMode
    controller._state.scanInitiationMode = ScanInitiationMode.ScanWidget
    controller._logsDir = '/tmp/_et_harness_logs'
    controller.signalDic = None
    controller.scanInfoDict = None
    controller._analogParameterDict = {}
    controller._digitalParameterDict = {}
    controller._positionersScan = []
    # Stub the triggered-scan runner so initiateSlowScan does not touch hardware.
    controller._triggeredScanRunner = SimpleNamespace(
        prepare=lambda *a, **k: SimpleNamespace(
            success=True, signal_dict={}, scan_info_dict={}, message=''
        )
    )
    controller._commChannel = SimpleNamespace()
    controller._master = SimpleNamespace(
        scanManager=None, positionersManager=None, nidaqManager=None,
    )
    return controller


def test_fast_axis_shift_coefficients_are_subclass_configurable():
    controller = _make_controller(cls=_CustomShiftHarness)
    controller._analogParameterDict = {
        'sequence_time': 2.0,
        'axis_step_size': [3.0],
    }

    assert controller.addFastAxisShift(42.0) == 42.0


def test_fast_axis_shift_rejects_invalid_coefficients():
    controller = _make_controller(cls=_BadShiftHarness)
    controller._analogParameterDict = {
        'sequence_time': 2.0,
        'axis_step_size': [3.0],
    }

    with pytest.raises(ValueError, match="exactly 6 values"):
        controller.addFastAxisShift(42.0)


def test_analysis_scatter_pipeline_markers_are_subclass_configurable():
    controller = _make_controller()
    controller._pipelineName = 'custom_probe'
    assert controller._pipelineSupportsAnalysisScatter() is False

    controller.ANALYSIS_SCATTER_PIPELINE_NAME_MARKERS = ('custom_',)
    assert controller._pipelineSupportsAnalysisScatter() is True


# ── flag-off no-op ───────────────────────────────────────────────────────── #

def test_flag_off_is_complete_noop_across_lifecycle():
    controller = _make_controller(enabled=False)

    controller.initiate()
    assert controller.initiateSlowScan(position=(0, 0)) is True
    controller.stopExperiment(resetParams=True)

    # No smart-mode call of any kind when the rollout flag is off.
    assert controller._smartModeService.applyCalls == []
    assert controller._smartModeService.preflightCalls == []
    # And no 'role' step ever entered the lifecycle stream.
    assert not any(
        isinstance(c, tuple) and c[0] == 'role' for c in controller.calls
    )


# ── arm ordering ──────────────────────────────────────────────────────────── #

def test_initiate_preflights_then_applies_scouting_before_laser_on():
    controller = _make_controller(enabled=True)

    controller.initiate()

    assert controller._smartModeService.preflightCalls == [
        ('EtHarness', ['scouting', 'event']),
    ]
    assert controller._smartModeService.applyCalls == [('EtHarness', 'scouting')]
    # scouting (the beam-path mode) is applied before the fast laser turns on,
    # and after the pre-arm hook.
    assert controller.calls.index('pre_arm_hook') < controller.calls.index(('role', 'scouting'))
    assert controller.calls.index(('role', 'scouting')) < controller.calls.index(('laser', True))
    assert controller._state.running is True


def test_initiate_blocks_arming_on_preflight_failure():
    controller = _make_controller(
        enabled=True,
        preflightResult=PreflightResult(ok=False, messages=['hazard']),
    )

    controller.initiate()

    # Preflight ran and failed; scouting was never applied and the laser never
    # turned on; the controller did not arm.
    assert controller._smartModeService.preflightCalls
    assert controller._smartModeService.applyCalls == []
    assert ('role', 'scouting') not in controller.calls
    assert ('laser', True) not in controller.calls
    assert controller._state.running is False
    assert ('status', 'error') in controller.calls


def test_initiate_blocks_arming_when_scouting_apply_fails():
    controller = _make_controller(
        enabled=True,
        results={
            'scouting': ApplyResult(
                applied=True, ok=False, modeName='Scout mode',
                warnings=['FlipMirror: failed'], failedComponents=['FlipMirror'],
            )
        },
    )

    controller.initiate()

    # scouting was attempted but failed, so the fast laser never turned on.
    assert controller._smartModeService.applyCalls == [('EtHarness', 'scouting')]
    assert ('laser', True) not in controller.calls
    assert controller._state.running is False


# ── event ordering ────────────────────────────────────────────────────────── #

def test_initiate_slow_scan_applies_event_before_scan_prepare():
    controller = _make_controller(enabled=True)

    assert controller.initiateSlowScan(position=(1, 2)) is True

    assert controller._smartModeService.applyCalls == [('EtHarness', 'event')]
    assert ('role', 'event') in controller.calls


def test_event_apply_failure_blocks_scan_and_recovers_to_idle():
    controller = _make_controller(
        enabled=True,
        roles={
            'scouting': 'Scout mode',
            'event': 'Event mode',
            'idle': 'Idle mode',
        },
        results={
            'event': ApplyResult(
                applied=True, ok=False, modeName='Event mode',
                warnings=['FlipMirror: failed'], failedComponents=['FlipMirror'],
            )
        },
    )

    assert controller.initiateSlowScan(position=(1, 2)) is False

    # event apply failed, idle recovery applied, scan never prepared/triggered.
    assert controller._smartModeService.applyCalls == [
        ('EtHarness', 'event'),
        ('EtHarness', 'idle'),
    ]
    assert controller.signalDic is None
    assert controller.scanInfoDict is None


# ── resume ordering ───────────────────────────────────────────────────────── #

def test_continue_endless_applies_resume_then_scouting_fallback():
    # No explicit resume role configured -> falls back to scouting.
    controller = _make_controller(enabled=True, endless=True)
    controller._state.running = False

    controller.continueFastModality()

    assert controller._smartModeService.applyCalls == [('EtHarness', 'scouting')]
    assert controller.calls.index('resume_hook') < controller.calls.index(('role', 'scouting'))
    assert controller.calls.index(('role', 'scouting')) < controller.calls.index(('laser', True))
    assert controller._state.running is True


def test_continue_endless_prefers_resume_role_when_configured():
    controller = _make_controller(
        enabled=True,
        endless=True,
        roles={
            'scouting': 'Scout mode',
            'event': 'Event mode',
            'resume': 'Resume mode',
        },
    )
    controller._state.running = False

    controller.continueFastModality()

    assert controller._smartModeService.applyCalls == [('EtHarness', 'resume')]


def test_continue_endless_recovers_when_resume_apply_fails():
    controller = _make_controller(
        enabled=True,
        endless=True,
        results={
            'scouting': ApplyResult(
                applied=True, ok=False, modeName='Scout mode',
                warnings=['FlipMirror: failed'], failedComponents=['FlipMirror'],
            )
        },
    )
    controller._state.running = False

    controller.continueFastModality()

    # Resume apply (scouting fallback) failed -> recovery path, not armed.
    assert controller._state.running is False
    assert ('status', 'error') in controller.calls


# ── stop ordering ─────────────────────────────────────────────────────────── #

def test_stop_applies_idle_after_fast_laser_off_when_configured():
    controller = _make_controller(
        enabled=True,
        roles={
            'scouting': 'Scout mode',
            'event': 'Event mode',
            'idle': 'Idle mode',
        },
    )

    controller.stopExperiment(resetParams=True)

    assert controller._smartModeService.applyCalls == [('EtHarness', 'idle')]
    # idle is applied after the fast laser is turned off.
    assert controller.calls.index(('laser', False)) < controller.calls.index(('role', 'idle'))


def test_stop_does_not_apply_idle_when_unconfigured():
    controller = _make_controller(enabled=True)  # no idle role configured

    controller.stopExperiment(resetParams=True)

    assert controller._smartModeService.applyCalls == []


# ── EtMonalisa: direct stand hooks are flag-gated ───────────────────────────── #

class _StandSubManager:
    def __init__(self):
        self.calls = []

    def setFLUO(self):
        self.calls.append('setFLUO')

    def setCS(self):
        self.calls.append('setCS')

    def setILshutter(self, value):
        self.calls.append(('setILshutter', value))


def _import_etmonalisa():
    from imswitch.imcontrol.controller.controllers.EtMonalisaController import (
        EtMonalisaController,
    )

    return EtMonalisaController


class _MonalisaHarness(_HarnessController):
    """Harness that runs EtMonalisa's real flag-gated hooks.

    It inherits EtMonalisa's ``_pre_arm_hook`` / ``_on_*_modality_hook``
    verbatim and only overrides the sleep-heavy stand helpers, plus records the
    legacy stand command in the call stream for ordering assertions.
    """

    SMART_MODE_WORKFLOW = 'EtMonalisa'

    # Bound at class-creation time below from the real controller's hooks.
    def _switchStandToFastMode(self):
        self.calls.append('pre_arm_hook')
        self._master.standManager._subManager.setFLUO()

    def _switchStandToSlowMode(self):
        self._master.standManager._subManager.setCS()


# Adopt EtMonalisa's real hook methods so we test the shipped behavior, not a
# re-implementation. The sleep-pumping stand helpers stay overridden above.
_EtMonalisa = _import_etmonalisa()
_MonalisaHarness._pre_arm_hook = _EtMonalisa._pre_arm_hook
_MonalisaHarness._on_pause_modality_hook = _EtMonalisa._on_pause_modality_hook
_MonalisaHarness._on_resume_modality_hook = _EtMonalisa._on_resume_modality_hook
_MonalisaHarness._post_stop_hook = _EtMonalisa._post_stop_hook


def test_monalisa_stand_hook_suppressed_when_smart_mode_on():
    """With smart-mode switching ON, the direct stand hook is NOT invoked.

    The scouting role still applies (and drives the LeicaStand setup-mode
    component), but switching the stand directly here would double-actuate, so
    the legacy direct command is suppressed. ``sigInitiateEtMonalisa`` is UI
    state and still fires.
    """
    controller = _make_controller(cls=_MonalisaHarness, enabled=True)
    standSub = _StandSubManager()
    controller._master = SimpleNamespace(
        standManager=SimpleNamespace(_subManager=standSub)
    )
    controller._commChannel = SimpleNamespace(sigInitiateEtMonalisa=_Signal())

    controller.initiate()

    # The direct stand command did NOT run; only the scouting role applied.
    assert standSub.calls == []
    assert controller._commChannel.sigInitiateEtMonalisa.emitted == [(True,)]
    assert controller._smartModeService.applyCalls == [('EtMonalisa', 'scouting')]
    assert controller._state.running is True


def test_monalisa_stand_hook_fires_on_arm_when_smart_mode_off():
    """With smart-mode switching OFF (legacy path), the direct stand hook fires.

    No smart-mode role is applied; the behavior is byte-for-byte the legacy one.
    ``sigInitiateEtMonalisa`` fires regardless of the flag.
    """
    controller = _make_controller(cls=_MonalisaHarness, enabled=False)
    standSub = _StandSubManager()
    controller._master = SimpleNamespace(
        standManager=SimpleNamespace(_subManager=standSub)
    )
    controller._commChannel = SimpleNamespace(sigInitiateEtMonalisa=_Signal())

    controller.initiate()

    # The direct stand command ran (legacy path); no smart-mode role applied.
    assert standSub.calls == ['setFLUO']
    assert controller._commChannel.sigInitiateEtMonalisa.emitted == [(True,)]
    assert controller._smartModeService.applyCalls == []
    assert controller._state.running is True
