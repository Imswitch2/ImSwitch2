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


def test_raster_controller_provides_recording_accessors():
    source = (CONTROLLERS / 'TriggerScopeRasterController.py').read_text(encoding='utf-8')

    assert 'def getNumScanPositions(self) -> int:' in source
    assert 'def getNumCamTTL(self) -> dict:' in source
    # Frame count derives from the same dims BeadRec uses
    assert 'self.getBeadRecScanDims()' in _method_body(source, 'getNumScanPositions')
    # Camera TTL map only contains TTL-included detectors
    assert 'self._setupInfo.detectors' in _method_body(source, 'getNumCamTTL')


def test_recording_controller_does_not_broadcast_run_scan_without_scan_widget():
    source = (CONTROLLERS / 'RecordingController.py').read_text(encoding='utf-8')

    scanOnce = source[source.index('elif self.recMode == RecMode.ScanOnce:'):]
    scanOnce = scanOnce[:scanOnce.index('elif self.recMode == RecMode.ScanLapse:')]

    assert 'if self._commChannel.hasScanWidget():' in scanOnce
    assert 'self._commChannel.scanWorkflow.run_scan(True, False)' in scanOnce
    assert 'Recording armed (scan-once)' in scanOnce


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
