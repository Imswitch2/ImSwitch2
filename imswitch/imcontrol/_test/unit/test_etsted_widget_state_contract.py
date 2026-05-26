from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
WIDGET_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'widgets' / 'EtSTEDWidget.py'
# Status / controls-armed calls were lifted into the shared base controller
# during the EtSTED+EtMonalisa unification.  The contract still applies — we
# just check the source-of-truth file instead.
BASE_CONTROLLER_PATH = (
    ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'controllers'
    / 'EventTriggeredBaseController.py'
)


def test_etsted_widget_exposes_status_and_armed_control_contract():
    source = WIDGET_PATH.read_text()

    assert 'def setEtSTEDStatus' in source
    assert 'def setEtSTEDControlsArmed' in source
    assert 'statusLabel' in source
    assert 'statusMessageLabel' in source
    assert "self.setBusyFalseButton.setVisible(status == 'error')" in source
    assert 'control.setEnabled(not armed)' in source


def test_etsted_controller_updates_status_contract():
    source = BASE_CONTROLLER_PATH.read_text()

    # The base class calls _set_status (which forwards to setEtSTEDStatus on
    # widgets that expose it) at every milestone of the run.
    assert "self._set_status('arming')" in source
    assert "self._set_status('detecting')" in source
    assert "self._set_status('triggered')" in source
    assert "self._set_status('scanning')" in source
    assert "self._set_status('error'" in source
    # Controls-armed lockout still applied at arm and release.
    assert 'self._set_controls_armed(True)' in source
    assert 'self._set_controls_armed(False)' in source
