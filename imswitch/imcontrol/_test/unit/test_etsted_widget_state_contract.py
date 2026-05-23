from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
WIDGET_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'widgets' / 'EtSTEDWidget.py'
CONTROLLER_PATH = ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'controllers' / 'EtSTEDController.py'


def test_etsted_widget_exposes_status_and_armed_control_contract():
    source = WIDGET_PATH.read_text()

    assert 'def setEtSTEDStatus' in source
    assert 'def setEtSTEDControlsArmed' in source
    assert 'statusLabel' in source
    assert 'statusMessageLabel' in source
    assert "self.setBusyFalseButton.setVisible(status == 'error')" in source
    assert 'control.setEnabled(not armed)' in source


def test_etsted_controller_updates_status_contract():
    source = CONTROLLER_PATH.read_text()

    assert "self._setEtSTEDStatus('arming')" in source
    assert "self._setEtSTEDStatus('detecting')" in source
    assert "self._setEtSTEDStatus('triggered')" in source
    assert "self._setEtSTEDStatus('scanning')" in source
    assert "self._setEtSTEDStatus('error'" in source
    assert 'self._widget.setEtSTEDControlsArmed(True)' in source
    assert 'self._widget.setEtSTEDControlsArmed(False)' in source
