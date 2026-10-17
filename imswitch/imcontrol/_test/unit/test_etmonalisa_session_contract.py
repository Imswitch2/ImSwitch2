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
    suppressed to avoid double-actuation.
    """
    controller_source = CONTROLLER_PATH.read_text()

    # The direct stand switches are guarded by the smart-mode flag.
    assert 'not self._smartModeSwitchingEnabled()' in controller_source

    # The stand helpers route through the public StandManager API (WP3:
    # controllers no longer reach into standManager._subManager).
    assert 'self._master.standManager.setFLUO()' in controller_source
    assert 'self._master.standManager.setCS()' in controller_source
    assert 'self._master.standManager.setILshutter(1)' in controller_source

    # The unused sigInitiateEtMonalisa flag is gone from the channel and the
    # controller.
    assert 'sigInitiateEtMonalisa' not in controller_source
