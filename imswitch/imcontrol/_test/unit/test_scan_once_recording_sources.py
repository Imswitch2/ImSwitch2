"""Scan-once recording on setups without a 'Scan' widget (e.g. Snouty /
TriggerScope), from the June 2026 Snouty bug set:
- the CommunicationChannel resolves the one recording scan source, and refuses
  a missing or ambiguous one;
- RecordingController must NOT broadcast run_scan (it would start several
  firmware scans at once); it arms the recording instead;
- a recording that cannot bind to the scan vetoes the TriggerScope start.

The last two pin the controller source text: they are the only guards on these
scan-safety properties until behavioural tests replace them.
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


def _method_body(source: str, methodName: str) -> str:
    body = source[source.index(f'def {methodName}'):]
    end = body.find('\n    def ')
    return body[:end] if end != -1 else body


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
