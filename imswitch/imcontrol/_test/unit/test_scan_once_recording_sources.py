"""Scan-once recording on setups without a 'Scan' widget (e.g. Snouty /
TriggerScope), from the June 2026 Snouty bug set: the CommunicationChannel
resolves the one recording scan source and refuses a missing or ambiguous
one; the recording never broadcasts a scan start (it would start several
firmware scans at once) but arms and binds to the scan the operator starts;
and a recording that cannot bind vetoes that scan.
"""

from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.basecontrollers import SuperScanController
from imswitch.imcontrol.controller.CommunicationChannel import (
    CommunicationChannel,
)
from imswitch.imcontrol.controller.controllers.RecordingController import (
    RecordingController,
)
from imswitch.imcontrol.controller.WorkflowServices import ScanWorkflowService
from imswitch.imcontrol.model import RecMode, SaveFormat, SaveMode


def _recording_source():
    return SimpleNamespace(
        runScanExternal=lambda *_args: None,
        abortScan=lambda: None,
        getNumScanPositions=lambda: 1,
        getNumCamTTL=lambda: {},
    )


def test_channel_resolves_exact_recording_scan_source():
    canonical = _recording_source()
    standalone = _recording_source()
    shell = SimpleNamespace(
        _CommunicationChannel__main=SimpleNamespace(
            controllers={
                'Scan': canonical,
                'TriggerScopeRaster': standalone,
            }
        )
    )

    assert (
        CommunicationChannel.getRecordingScanSource(shell) is canonical
    )

    shell._CommunicationChannel__main.controllers = {
        'TriggerScopeRaster': standalone,
        'Other': SimpleNamespace(runScanExternal=lambda *_args: None),
    }
    assert (
        CommunicationChannel.getRecordingScanSource(shell) is standalone
    )


@pytest.mark.parametrize("controllers", [{}, {
    'RasterA': _recording_source(),
    'RasterB': _recording_source(),
}])
def test_channel_refuses_missing_or_ambiguous_recording_scan_source(
        controllers):
    shell = SimpleNamespace(
        _CommunicationChannel__main=SimpleNamespace(
            controllers=controllers
        )
    )

    with pytest.raises(RuntimeError, match='Cannot automate scan-lapse'):
        CommunicationChannel.getRecordingScanSource(shell)


def test_camera_ttl_count_includes_sample_zero_high():
    assert SuperScanController._countRisingEdges([1, 1, 0, 1, 0, 1]) == 3
    assert SuperScanController._countRisingEdges([0, 1, 1, 0, 1, 0]) == 2


class _Log:
    def __getattr__(self, level):
        return lambda *args, **kwargs: None


def _scanOnceRecording(tmp_path, *, hasScanWidget, refuse=None):
    """RecordingController's own scan-once code on a shell. ``refuse`` names
    the arming step that fails."""
    calls = []
    source = SimpleNamespace(name='TriggerScopeRaster')

    def step(name):
        return lambda: calls.append(name) or name != refuse

    shell = SimpleNamespace(
        _RecordingController__logger=_Log(),
        _finalizingRecCycle=False,
        recording=False,
        recMode=RecMode.ScanOnce,
        _recordingAcquisitionStartAllowed=lambda: True,
        _master=SimpleNamespace(
            recordingManager=SimpleNamespace(record=False, recordingGeneration=1)
        ),
        updateRecAttrs=lambda isSnapping: None,
        _widget=SimpleNamespace(
            getRecFolder=lambda: str(tmp_path),
            getRecSaveMode=lambda: SaveMode.Disk.value,
            getSaveFormat=lambda: SaveFormat.HDF5.value,
            getMultiDetectorSingleFile=lambda: False,
        ),
        getFileName=lambda: 'rec',
        getDetectorNamesToCapture=lambda: ['APD'],
        _preflightNewScanRequest=lambda: True,
        _freezeRecordingFilenameSuffix=lambda source: None,
        _scanRunTokenFor=lambda source: 'run',
        _commChannel=SimpleNamespace(
            hasScanWidget=lambda: hasScanWidget,
            getRecordingScanSource=lambda: source,
            sharedAttrs=SimpleNamespace(getHDF5Attributes=lambda: {}),
            # The broadcast: it starts every scan controller listening.
            scanWorkflow=SimpleNamespace(
                run_scan=lambda *args, **kwargs: calls.append('broadcast')
            ),
        ),
        _notifyScanStarting=lambda: calls.append('scan-starting'),
        _applyScanGeometryToRecordingArgs=step('geometry'),
        _startManagerRecording=step('manager-armed'),
        _waitForManagerArm=step('manager-ready'),
        _requestScanStart=lambda recalculate, nonFinal: (
            calls.append(('scan-request', recalculate, nonFinal)) or True
        ),
        _handleRecordingFailure=lambda message, **kwargs: calls.append(
            ('failure', message)
        ),
    )
    return shell, source, calls


def test_scan_once_without_a_scan_widget_arms_and_starts_no_scan(tmp_path):
    """Several standalone scan controllers listen for a start; requesting one
    without a source would start all of their scans at once. The recording
    waits instead, and binds to the scan the operator starts."""
    shell, source, calls = _scanOnceRecording(tmp_path, hasScanWidget=False)

    RecordingController.toggleREC(shell, True)

    assert calls == []
    assert shell._awaitingScanSourceArm is True

    assert RecordingController.prepareForScanSource(shell, source) is True
    assert calls == ['geometry', 'manager-armed', 'manager-ready']
    assert shell._recordingScanSource is source
    assert shell._scanStartOwnedByScanSource is True


def test_scan_once_with_a_scan_widget_requests_that_one_scan(tmp_path):
    shell, source, calls = _scanOnceRecording(tmp_path, hasScanWidget=True)

    RecordingController.toggleREC(shell, True)

    assert calls == [
        'scan-starting', 'geometry', 'manager-armed', 'manager-ready',
        ('scan-request', True, False),
    ]
    assert shell._recordingScanSource is source


@pytest.mark.parametrize('refuse', ['geometry', 'manager-armed', 'manager-ready'])
def test_a_recording_that_cannot_bind_vetoes_the_scan(tmp_path, refuse):
    """False stops the scan: one that ran anyway would bleach the sample with
    nothing recording it."""
    shell, source, calls = _scanOnceRecording(
        tmp_path, hasScanWidget=False, refuse=refuse
    )
    RecordingController.toggleREC(shell, True)

    assert RecordingController.prepareForScanSource(shell, source) is False
    assert calls[-1] == refuse
    assert shell._scanStartOwnedByScanSource is False


def _workflowWithRecording(recording):
    channel = SimpleNamespace(controllerRegistry=lambda: {'Recording': recording})
    return ScanWorkflowService(channel)


def test_the_scan_workflow_passes_the_recordings_veto_to_the_scan():
    source = object()
    asked = []

    def refuse(scan):
        asked.append(scan)
        return False

    def fail(scan):
        raise RuntimeError('writer gone')

    assert _workflowWithRecording(
        SimpleNamespace(prepareForScanSource=refuse)
    ).prepare_recording_for_scan(source) is False
    assert asked == [source]
    # A recording that could not even be asked was not armed for this scan.
    assert _workflowWithRecording(
        SimpleNamespace(prepareForScanSource=fail)
    ).prepare_recording_for_scan(source) is False
    assert _workflowWithRecording(None).prepare_recording_for_scan(source) is True
