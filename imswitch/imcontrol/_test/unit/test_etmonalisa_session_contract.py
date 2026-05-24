from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
CONTROLLER_PATH = ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'controllers' / 'EtMonalisaController.py'


def test_etmonalisa_controller_uses_shared_session_state():
    source = CONTROLLER_PATH.read_text()

    assert 'EventTriggeredSessionState' in source
    assert 'self.__state = EventTriggeredSessionState()' in source
    assert 'self.__state.reset_runtime_counters()' in source
    assert 'class RunMode' not in source
    assert 'class ScanInitiationMode' not in source
    assert 'self.__running =' not in source
    assert 'self.__busy =' not in source
    assert 'self.__detLog' not in source
