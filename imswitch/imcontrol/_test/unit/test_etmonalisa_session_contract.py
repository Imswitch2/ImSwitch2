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
