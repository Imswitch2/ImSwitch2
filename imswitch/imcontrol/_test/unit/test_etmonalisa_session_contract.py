from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
CONTROLLER_PATH = ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'controllers' / 'EtMonalisaController.py'
BASE_CONTROLLER_PATH = (
    ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'controllers'
    / 'EventTriggeredBaseController.py'
)


def test_etmonalisa_controller_uses_shared_session_state():
    controller_source = CONTROLLER_PATH.read_text()
    base_source = BASE_CONTROLLER_PATH.read_text()

    assert 'class EtMonalisaController(EventTriggeredControllerBase):' in controller_source
    assert 'EventTriggeredSessionState' not in controller_source
    assert 'self._state.runMode == RunMode.Experiment' in controller_source
    assert 'EventTriggeredSessionState' in base_source
    assert 'self._state = EventTriggeredSessionState()' in base_source
    assert 'self._state.reset_runtime_counters()' in base_source
    assert 'class RunMode' not in controller_source
    assert 'class ScanInitiationMode' not in controller_source
    assert 'self.__running =' not in controller_source
    assert 'self.__busy =' not in controller_source
    assert 'self.__detLog' not in controller_source


def test_etmonalisa_direct_stand_hooks_are_flag_gated():
    """Phase 5b: the Experiment-mode hooks gate the direct stand switch on the flag.

    With smart-mode switching off (legacy/default), the hooks still call
    ``_switchStandToFastMode``/``_switchStandToSlowMode``; with it on, the
    LeicaStand setup-mode component carries FLUO/CS, so the direct switch is
    suppressed to avoid double-actuation. The ``sigInitiateEtMonalisa`` emissions
    are UI state and stay unconditional.
    """
    controller_source = CONTROLLER_PATH.read_text()

    # The direct stand switches are guarded by the smart-mode flag.
    assert 'not self._smartModeSwitchingEnabled()' in controller_source

    # The raw stand helpers themselves are unchanged (flag-off path identical).
    assert 'self._master.standManager._subManager.setFLUO()' in controller_source
    assert 'self._master.standManager._subManager.setCS()' in controller_source
    assert 'self._master.standManager._subManager.setILshutter(1)' in controller_source

    # sigInitiateEtMonalisa stays unconditional (not inside a flag/runMode guard).
    pre_arm = controller_source.split('def _pre_arm_hook', 1)[1].split('def ', 1)[0]
    emit_line = 'self._commChannel.sigInitiateEtMonalisa.emit(True)'
    assert emit_line in pre_arm
    assert pre_arm.index(emit_line) < pre_arm.index('if')
