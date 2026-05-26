from imswitch.imcontrol.model.EventTriggeredSession import (
    EventRunMode,
    EventScanInitiationMode,
    EventTriggeredSessionState,
)


def test_event_triggered_session_resets_runtime_counters_only():
    state = EventTriggeredSessionState(
        runMode=EventRunMode.Validate,
        scanInitiationMode=EventScanInitiationMode.RecordingWidget,
        detectorFast='widefield',
        laserFast='laser488',
        running=True,
        validating=True,
        busy=True,
        imageSignalConnected=True,
        scanEndSignalConnected=True,
        binaryMaskSignalConnected=True,
        frame=8,
        validationFrames=3,
        tCallMs=12.5,
        maxAnaImgVal=42,
        detLog={'pipeline': 'demo'},
    )

    state.reset_runtime_counters()

    assert state.runMode == EventRunMode.Validate
    assert state.scanInitiationMode == EventScanInitiationMode.RecordingWidget
    assert state.detectorFast == 'widefield'
    assert state.laserFast == 'laser488'
    assert state.imageSignalConnected is True
    assert state.scanEndSignalConnected is True
    assert state.binaryMaskSignalConnected is True
    assert state.detLog == {'pipeline': 'demo'}
    assert state.running is False
    assert state.validating is False
    assert state.busy is False
    assert state.frame == 0
    assert state.validationFrames == 0
    assert state.tCallMs == 0
    assert state.maxAnaImgVal == 0
