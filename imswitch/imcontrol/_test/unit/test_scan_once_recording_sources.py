"""Contract tests for scan-once recording on setups without a 'Scan' widget
(e.g. Snouty / TriggerScope) and the related zero-frame diagnostics.

Pins the fixes for the June 2026 Snouty bug set:
- RecordingController scan-once no longer requires the 'Scan' widget for
  getNumScanPositions / getNumCamTTL (resolved via _resolveScanAccessor).
- RecordingController must NOT broadcast run_scan on setups whose scan
  controllers all listen to sigRunScan (it would start several firmware
  scans at once); it arms the recording instead.
- TriggerScopeRasterController provides the recording accessors and warns
  when an included camera TTL device will not be pulsed (the raster
  firmware drives only the earliest-start TTL line, p1).
- BeadRecController reports loudly when a scan ends with zero frames.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from imswitch.imcontrol.controller.basecontrollers import SuperScanController
from imswitch.imcontrol.controller.CommunicationChannel import (
    CommunicationChannel,
)


ROOT = Path(__file__).resolve().parents[4]
CONTROLLERS = ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'controllers'
CHANNEL_PATH = ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'CommunicationChannel.py'


def _method_body(source: str, methodName: str) -> str:
    body = source[source.index(f'def {methodName}'):]
    end = body.find('\n    def ')
    return body[:end] if end != -1 else body


def test_channel_resolves_recording_accessors_without_scan_widget():
    source = CHANNEL_PATH.read_text(encoding='utf-8')

    assert 'def hasScanWidget(self)' in source
    assert 'def _resolveScanAccessor(self, methodName)' in source

    for accessor in ('getNumScanPositions', 'getNumCamTTL'):
        body = _method_body(source, accessor)
        assert f"_resolveScanAccessor('{accessor}')" in body

    # Resolution priority: active scan source first, then 'Scan' widget,
    # then a unique idle provider — never first-match among several.
    resolver = _method_body(source, '_resolveScanAccessor')
    assert 'self._activeScanSource' in resolver
    assert "_get_required_controller('Scan', 'scan')" in resolver
    assert 'len(matches) == 1' in resolver
    assert 'len(matches) > 1' in resolver


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


def test_raster_controller_provides_recording_accessors():
    source = (CONTROLLERS / 'TriggerScopeRasterController.py').read_text(encoding='utf-8')

    assert 'def getNumScanPositions(self) -> int:' in source
    assert 'def getNumCamTTL(self) -> dict:' in source
    # Frame count derives from the same dims BeadRec uses
    assert 'self.getBeadRecScanDims()' in _method_body(source, 'getNumScanPositions')
    # Camera TTL map only contains TTL-included detectors
    assert 'self._setupInfo.detectors' in _method_body(source, 'getNumCamTTL')


def test_camera_ttl_count_includes_sample_zero_high():
    assert SuperScanController._countRisingEdges([1, 1, 0, 1, 0, 1]) == 3
    assert SuperScanController._countRisingEdges([0, 1, 1, 0, 1, 0]) == 2


def test_recording_controller_does_not_broadcast_run_scan_without_scan_widget():
    source = (CONTROLLERS / 'RecordingController.py').read_text(encoding='utf-8')

    scanOnce = source[source.index('elif self.recMode == RecMode.ScanOnce:'):]
    scanOnce = scanOnce[:scanOnce.index('elif self.recMode == RecMode.ScanLapse:')]

    # Whether the setup has a canonical 'Scan' widget is decided while the
    # request is being validated; the arm block below only acts on the result.
    toggleREC = _method_body(source, 'toggleREC')
    assert 'if self._commChannel.hasScanWidget():' in toggleREC
    assert 'self._commChannel.getRecordingScanSource()' in toggleREC
    assert 'self._awaitingScanSourceArm = True' in toggleREC

    assert 'self._requestScanStart(True, False)' in scanOnce
    assert 'Recording armed (scan-once)' in scanOnce
    # A broadcast would start every standalone scan controller at once.
    assert 'run_scan(' not in scanOnce


def test_armed_scan_once_binds_geometry_to_the_scan_that_starts():
    """A setup with several scan widgets must not freeze the frame count at
    arm time: the controller that asks to start is the one whose geometry the
    recording is armed with."""
    source = (CONTROLLERS / 'RecordingController.py').read_text(encoding='utf-8')

    body = _method_body(source, 'prepareForScanSource')
    # Bind only while armed and waiting, and only to a real source.
    assert 'if source is None or not self._awaitingScanSourceArm:' in body
    assert 'self._recordingScanSource = source' in body
    # Geometry first, then arm: the manager must never be armed with the
    # previous guess.
    assert body.index('_applyScanGeometryToRecordingArgs') < body.index(
        '_startManagerRecording'
    )
    assert '_waitForManagerArm' in body

    geometry = _method_body(source, '_applyScanGeometryToRecordingArgs')
    for accessor in ('getNumScanPositions', 'getNumCamTTL'):
        assert f"'{accessor}'" in geometry
    assert 'self._scanAccessor(methodName)' in geometry


def test_scan_binding_can_veto_the_scan_rather_than_only_reporting():
    """A recording that cannot be armed must stop the scan. Learning about the
    scan from an announcement signal is too late: the slot's return value goes
    nowhere and the firmware is already being commanded."""
    recording = _method_body(
        (CONTROLLERS / 'RecordingController.py').read_text(encoding='utf-8'),
        'prepareForScanSource',
    )
    # Every step that can fail answers False; only the end answers True.
    for step in (
        '_applyScanGeometryToRecordingArgs',
        '_startManagerRecording',
        '_waitForManagerArm',
    ):
        assert f'if not self.{step}():\n            return False' in recording
    assert recording.rstrip().endswith('return True')

    lifecycle = (
        CONTROLLERS / '_triggerscope_scan_lifecycle.py'
    ).read_text(encoding='utf-8')
    startBody = _method_body(lifecycle, '_startTriggerScopeScan')
    assert 'if not self._prepareRecordingForTriggerScopeScan():' in startBody
    assert '_abandonTriggerScopeRun(runToken)' in startBody
    # The veto must precede the active-source announcement, the start boundary
    # and every hardware write.
    vetoAt = startBody.index('_prepareRecordingForTriggerScopeScan')
    for later in (
        'self.isRunning = True',
        'self._commChannel.sigScanStarting',
        'self._commChannel.sigScanDevicesResolved',
        'armWithStarter',
    ):
        assert vetoAt < startBody.index(later), later


def test_scan_lapse_reads_geometry_from_its_own_pinned_source():
    """ScanLapse picks a source and then reads geometry before anything runs,
    so the global accessors would still be ambiguous — with every controller
    now implementing them, the channel lookup raises instead of answering."""
    source = (CONTROLLERS / 'RecordingController.py').read_text(encoding='utf-8')

    body = _method_body(source, '_scanAccessor')
    assert "self.__dict__.get('_recordingScanSource')" in body
    assert 'getattr(source, methodName, None)' in body

    nextLapse = _method_body(source, 'nextLapse')
    assert 'self._applyScanGeometryToRecordingArgs()' in nextLapse
    for accessor in ('getNumScanPositions', 'getNumCamTTL'):
        assert f'self._commChannel.{accessor}()' not in nextLapse

    for helper in ('_scanDimsForRecording', '_scanStepSizesForRecording'):
        helperBody = _method_body(source, helper)
        assert '_scanAccessor(' in helperBody
        assert 'self._commChannel.get' not in helperBody


def test_a_pinned_source_without_an_optional_accessor_yields_no_metadata():
    """Falling back to the channel would label a pinned RESOLFT recording with
    the raster controller's dimensions and step sizes, since only the raster
    implements the BeadRec accessors. No metadata beats wrong metadata."""
    source = (CONTROLLERS / 'RecordingController.py').read_text(encoding='utf-8')

    body = _method_body(source, '_scanAccessor')
    # Pinned and missing the accessor -> None, never the channel.
    assert 'return accessor if callable(accessor) else None' in body

    for helper in ('_scanDimsForRecording', '_scanStepSizesForRecording'):
        helperBody = _method_body(source, helper)
        assert 'if accessor is None:\n            return None' in helperBody

    # The required accessors are a hard failure instead, since the frame count
    # cannot be guessed.
    geometry = _method_body(source, '_applyScanGeometryToRecordingArgs')
    assert 'if accessor is None:' in geometry
    assert 'raise RuntimeError(' in geometry


def test_an_unmade_scan_source_choice_is_refused_not_defaulted():
    """Repopulating a combo box selects index zero, so without a placeholder a
    vanished or never-made choice would silently drive the first registered
    scanner."""
    widget = (
        ROOT / 'imswitch' / 'imcontrol' / 'view' / 'widgets'
        / 'RecordingWidget.py'
    ).read_text(encoding='utf-8')

    assert 'SCAN_SOURCE_PLACEHOLDER' in widget
    populate = _method_body(widget, 'setScanSourceOptions')
    # The placeholder is added first and carries no controller key.
    assert (
        'self.scanSourceList.addItem(self.SCAN_SOURCE_PLACEHOLDER, \'\')'
        in populate
    )
    assert populate.index('SCAN_SOURCE_PLACEHOLDER') < populate.index(
        'for key in sourceKeys'
    )

    source = (CONTROLLERS / 'RecordingController.py').read_text(encoding='utf-8')
    nextLapse = _method_body(source, 'nextLapse')
    assert 'if not selectedKey and self._scanSourceChoiceRequired():' in nextLapse
    assert 'Select a scan source' in nextLapse
    # One capable scanner is not a choice; those rigs are never asked.
    assert 'len(list(listSources())) > 1' in _method_body(
        source, '_scanSourceChoiceRequired'
    )
    # A saved choice for a scanner this setup no longer has must not persist.
    assert 'if self._scanSourcePreference not in sources:' in _method_body(
        source, '_refreshScanSourceOptions'
    )


def test_a_scanner_failing_after_the_arm_fails_the_recording():
    """The scan source publishes sigScanStarting for a late-bound recording, so
    _scanStartPublished stays False. Without a second marker the terminal check
    is skipped and the writer waits for frames until its stall watchdog."""
    source = (CONTROLLERS / 'RecordingController.py').read_text(encoding='utf-8')

    bind = _method_body(source, 'prepareForScanSource')
    assert 'self._scanStartOwnedByScanSource = True' in bind

    ended = _method_body(source, '_scanLifecycleEnded')
    assert (
        'self._scanStartPublished or self._scanStartOwnedByScanSource' in ended
    )
    assert 'self._scanStartOwnedByScanSource = False' in ended
    # Cleared on both operation start and teardown, so it cannot leak.
    assert source.count('self._scanStartOwnedByScanSource = False') >= 4


def test_cancelling_an_unclaimed_arm_closes_the_operation_locally():
    """No manager session exists, so endRecording() yields no terminal. Without
    local cleanup the arm stays pending and a later scan would start recording
    with REC switched off."""
    source = (CONTROLLERS / 'RecordingController.py').read_text(encoding='utf-8')

    toggleREC = _method_body(source, 'toggleREC')
    assert 'if self._awaitingScanSourceArm:' in toggleREC
    assert 'self._cancelPendingScanSourceArm()' in toggleREC

    body = _method_body(source, '_cancelPendingScanSourceArm')
    # Clear the pending arm before teardown so a scan started meanwhile
    # cannot bind to a recording that is going away.
    assert body.index('_awaitingScanSourceArm = False') < body.index(
        'recordingCycleEnded()'
    )


def test_raster_controller_logs_ttl_diagnostics_and_camera_keepup():
    """The deployed TriggerSwitch 0.1 firmware pulses TTL lines 0-3 together
    in one window (earliest row's start/end); individual rows are ignored.
    The controller must log that reality at scan start and warn when a
    triggered camera's exposure+readout exceeds the dwell time."""
    source = (CONTROLLERS / 'TriggerScopeRasterController.py').read_text(encoding='utf-8')

    assert 'def _logRasterTTLDiagnostics(self):' in source
    assert 'self._logRasterTTLDiagnostics()' in _method_body(source, 'runScanAdvanced')
    body = _method_body(source, '_logRasterTTLDiagnostics')
    assert 'starts.index(min(starts))' in body
    assert 'TTL lines 0-3 together' in body
    assert 'cannot keep up' in body
    assert 'Real exposure time' in body


def test_bead_rec_reports_zero_frames_after_scan():
    source = (CONTROLLERS / 'BeadRecController.py').read_text(encoding='utf-8')

    assert 'self.framesReceivedThisScan = 0' in source
    assert 'self.framesReceivedThisScan += recIm.frames_written' in source
    body = _method_body(source, 'onEndedScan')
    assert 'self.framesReceivedThisScan == 0' in body
    assert '0 detector frames received' in body
