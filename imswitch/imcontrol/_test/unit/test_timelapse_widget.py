"""The Timelapse widget's contract with its controller.

The widget is a view: the controller decides what each kind of timelapse
allows and pushes it in. What these tests pin is that the widget reports the
operator's settings faithfully, never edits on the controller's behalf, and
restores the controller's last decisions when a run ends.
"""

import pytest

from imswitch.imcontrol.view.widgets.TimelapseWidget import (
    KIND_CAMERA,
    KIND_SCAN,
    KIND_TILING,
    MODE_END_TO_START,
    MODE_START_TO_START,
    TimelapseWidget,
)

_LASERS = [
    dict(name='L488', label='488 nm (L488)', binary=False, units='mW',
         minimum=0, maximum=100, step=0.5, decimals=1, value=12.5),
    dict(name='LED', label='LED', binary=True, units='', minimum=None,
         maximum=None, step=None, decimals=None, value=None),
    dict(name='L640', label='640 nm (L640)', binary=False, units='%',
         minimum=0, maximum=100, step=1, decimals=0, value=30),
]


@pytest.fixture
def widget(qtbot):
    w = TimelapseWidget(None)
    qtbot.addWidget(w)
    w.setAvailableKinds([KIND_CAMERA, KIND_SCAN, KIND_TILING])
    w.setLasers(_LASERS)
    w.setDetectors(['CamA', 'CamB', 'APD'], checked=['CamA'])
    return w


def _spin(widget, name):
    return widget._laserRows[name][1]


def _check(widget, name):
    return widget._laserRows[name][0]


def test_constants_follow_the_model_enums():
    assert (KIND_CAMERA, KIND_SCAN, KIND_TILING) == ('camera', 'scan', 'tiling')
    assert MODE_START_TO_START == 'start_to_start'
    assert MODE_END_TO_START == 'end_to_start'


def test_timing_round_trips(widget):
    widget.setTimepoints(120)
    widget.setIntervalS(2.5)
    widget.setIntervalMode(MODE_END_TO_START)
    widget.setFramesPerPoint(3)
    assert widget.getTimepoints() == 120
    assert widget.getIntervalS() == 2.5
    assert widget.getIntervalMode() == MODE_END_TO_START
    assert widget.getFramesPerPoint() == 3


def test_a_long_interval_is_not_clipped(widget):
    widget.setIntervalS(86_400.0 * 30)  # a month
    assert widget.getIntervalS() == 86_400.0 * 30


def test_kind_changes_carry_the_kind(widget, qtbot):
    with qtbot.waitSignal(widget.sigKindChanged) as blocker:
        widget.setKind(KIND_SCAN)
    assert blocker.args == [KIND_SCAN]
    assert widget.getKind() == KIND_SCAN


def test_available_kinds_keep_the_current_one_when_still_offered(widget):
    widget.setKind(KIND_SCAN)
    widget.setAvailableKinds([KIND_CAMERA, KIND_SCAN])
    assert widget.getKind() == KIND_SCAN
    widget.setAvailableKinds([KIND_CAMERA])
    assert widget.getKind() == KIND_CAMERA


def test_scan_sources(widget):
    widget.setScanSources(['Advanced', 'TriggerScope'])
    widget.setScanSource('TriggerScope')
    assert widget.getScanSource() == 'TriggerScope'
    widget.setScanSources([])
    assert widget.getScanSource() == ''


def test_only_ticked_lasers_are_returned_and_binary_has_no_power(widget):
    assert widget.getLasers() == []
    widget.setLaserChecked('L640', True)
    widget.setLaserChecked('LED', True)
    widget.setLaserPower('L640', 55)
    assert widget.getLasers() == [('LED', None), ('L640', 55.0)]


def test_ticking_a_laser_enables_its_power_box(widget):
    assert not _spin(widget, 'L488').isEnabled()
    widget.setLaserChecked('L488', True)
    assert _spin(widget, 'L488').isEnabled()
    assert _spin(widget, 'L488').value() == 12.5
    assert _spin(widget, 'L488').suffix() == ' mW'


def test_read_only_lasers_display_without_editing(widget, qtbot):
    widget.setLasersEditable(False, 'The scan program decides.')
    assert not _check(widget, 'L488').isEnabled()
    assert widget.laserNoteLabel.text() == 'The scan program decides.'
    assert not widget.laserNoteLabel.isHidden()
    with qtbot.assertNotEmitted(widget.sigParamsChanged):
        widget.showLaserStates({'L488': True, 'L640': False, 'Gone': True})
    assert _check(widget, 'L488').isChecked()
    assert not _spin(widget, 'L488').isEnabled()
    assert not widget.settleCheck.isEnabled()


def test_settle_none_round_trips(widget):
    assert widget.getIlluminationSettleMs() is None
    widget.setIlluminationSettleMs(40.0)
    assert widget.getIlluminationSettleMs() == 40.0
    assert widget.settleSpinbox.isEnabled()
    widget.setIlluminationSettleMs(None)
    assert widget.getIlluminationSettleMs() is None
    assert not widget.settleSpinbox.isEnabled()


def test_detectors(widget):
    assert widget.getDetectors() == ['CamA']
    widget._detectorBoxes['APD'].setChecked(True)
    assert widget.getDetectors() == ['CamA', 'APD']
    widget.setDetectorEnabled('APD', False, 'Scan-driven')
    assert not widget._detectorBoxes['APD'].isEnabled()
    assert widget._detectorBoxes['APD'].toolTip() == 'Scan-driven'
    widget.setDetectorsEnabled(False, 'Tiling decides.')
    assert not widget._detectorBoxes['CamA'].isEnabled()
    assert widget.detectorNoteLabel.text() == 'Tiling decides.'


def test_single_file_and_summary(widget):
    widget.setSingleFile(False)
    assert widget.getSingleFile() is False
    widget.setSingleFileEnabled(False, 'Each timepoint is a folder.')
    assert not widget.singleFileCheck.isEnabled()
    widget.setSaveSummary('Recording panel: /data, rec, HDF5')
    assert widget.saveSummaryLabel.text().endswith('HDF5')


def test_running_disables_settings_and_restores_the_controllers_flags(widget):
    widget.setFramesPerPointEnabled(False)
    widget.setDetectorEnabled('APD', False)
    widget.setLaserChecked('L488', True)

    widget.setRunning(True)
    assert not widget.startButton.isEnabled()
    assert widget.stopButton.isEnabled()
    for control in (widget.kindCombo, widget.timepointsSpinbox,
                    widget.intervalSpinbox, _check(widget, 'L488'),
                    _spin(widget, 'L488'), widget._detectorBoxes['CamA'],
                    widget.singleFileCheck):
        assert not control.isEnabled()

    widget.setRunning(False)
    assert widget.startButton.isEnabled()
    assert not widget.stopButton.isEnabled()
    assert widget.timepointsSpinbox.isEnabled()
    assert _spin(widget, 'L488').isEnabled()
    assert widget._detectorBoxes['CamA'].isEnabled()
    assert not widget.framesPerPointSpinbox.isEnabled(), (
        'the controller disabled it; ending a run must not re-enable it'
    )
    assert not widget._detectorBoxes['APD'].isEnabled()


def test_buttons_emit_start_and_stop(widget, qtbot):
    with qtbot.waitSignal(widget.sigStartTimelapse):
        widget.startButton.click()
    widget.setRunning(True)
    with qtbot.waitSignal(widget.sigStopTimelapse):
        widget.stopButton.click()


def test_progress_and_status(widget):
    widget.setProgress(3, 10)
    assert widget.progressBar.value() == 3
    assert widget.progressBar.format() == '3/10 timepoints'
    widget.setStatus('Timepoint 4/10 in 12 s')
    widget.setStatusIsError(True)
    assert 'color' in widget.statusLabel.styleSheet()
    widget.setStatusIsError(False)
    assert widget.statusLabel.styleSheet() == ''


def test_editing_emits_params_changed(widget, qtbot):
    with qtbot.waitSignal(widget.sigParamsChanged):
        widget.setTimepoints(7)
    with qtbot.waitSignal(widget.sigParamsChanged):
        widget.setLaserChecked('L488', True)
    with qtbot.waitSignal(widget.sigParamsChanged):
        widget._detectorBoxes['CamB'].setChecked(True)


def test_disabling_a_ticked_detector_unticks_it_and_enabling_restores_it(widget):
    widget._detectorBoxes['APD'].setChecked(True)
    widget.setDetectorEnabled('APD', False, 'Scan-driven')
    assert not widget._detectorBoxes['APD'].isChecked()
    assert 'APD' not in widget.getDetectors()
    widget.setDetectorEnabled('APD', True)
    assert widget._detectorBoxes['APD'].isChecked()


def test_a_laser_display_is_set_aside_when_the_list_is_editable_again(widget):
    widget.setLaserChecked('L640', True)
    widget.setLasersEditable(False, 'The scan decides.')
    widget.showLaserStates({'L488': True, 'L640': False})
    assert [n for n, _ in widget.getLasers()] == ['L488'], 'the display'
    widget.setLasersEditable(True)
    assert [n for n, _ in widget.getLasers()] == ['L640'], "the operator's"
