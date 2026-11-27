"""Focus Lock panel: hold/relock controls, live confidence and failed test."""
import dataclasses

from imswitch.imcontrol.model import focus_lock_quality as flq
from imswitch.imcontrol.view.widgets.FocusLockWidget import FocusLockWidget


def test_panel_shows_state_reason_and_edits_settings(qtbot):
    widget = FocusLockWidget(options=None)
    qtbot.addWidget(widget)
    emitted = []
    widget.sigRobustnessSettingsChanged.connect(emitted.append)
    widget.setRobustnessSettings(dataclasses.asdict(flq.SupervisorSettings()),
                                 dataclasses.asdict(flq.RelockLimits()), storedAvailable=False)
    assert emitted == [], 'showing the controller values does not echo them back'
    assert not widget.reengageButton.isEnabled()
    widget.relockStepEdit.setValue(0.25)
    assert emitted and emitted[-1]['maxStepUm'] == 0.25
    widget.setLockState('holding')
    widget.setLockQuality({'state': 'holding', 'confidence': 0.91, 'reasons': ['agreement'],
                           'heldFor': ['jump']})
    assert 'Holding' in widget.lockStateLabel.text()
    assert '0.910' in widget.confidenceLabel.text()
    assert 'jump' in widget.reasonLabel.text()
    assert widget.relockButton.isEnabled()
    widget.setLockState('lost')
    widget.setLockQuality({'state': 'lost', 'confidence': None, 'reasons': [],
                           'lostReason': 'relock failed: excursion limit reached'})
    assert 'excursion' in widget.reasonLabel.text()
    widget.setLockQuality({'state': 'locked', 'confidence': 0.999, 'reasons': []})
    assert not widget.relockButton.isEnabled()
