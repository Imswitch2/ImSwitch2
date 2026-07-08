from pathlib import Path
from types import SimpleNamespace

from imswitch.imcontrol.controller.CommunicationChannel import CommunicationChannel
from imswitch.imcontrol.controller.controllers._beadrec_scan_source import (
    BeadRecScanSource,
    BeadRecScanSourceMixin,
)


ROOT = Path(__file__).resolve().parents[4]
CHANNEL_PATH = ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'CommunicationChannel.py'


class _RasterLikeController(BeadRecScanSourceMixin):
    """Minimal stand-in for a TriggerScope-style scan controller."""

    def getBeadRecScanDims(self):
        return (10, 20)

    def getBeadRecStepSizes(self):
        return (0.05, 0.1)


def test_mixin_defaults():
    controller = _RasterLikeController()

    assert controller.getNumLineSteps() == 1
    assert controller.getFramesPerScanPixel() == 1
    assert controller.isBeadRecCompatible() is True


def test_mixin_implementer_satisfies_runtime_protocol():
    assert isinstance(_RasterLikeController(), BeadRecScanSource)


def test_communication_channel_prefers_active_scan_source():
    source = CHANNEL_PATH.read_text(encoding='utf-8')

    assert 'def _activeBeadRecScanSource(self)' in source

    method = source[
        source.index('def getBeadRecScanSource'):source.index('def getDimsScan')
    ]
    assert 'self._activeBeadRecScanSource()' in method

    for accessor in ('def getDimsScan', 'def getScanStepSizes'):
        body = source[source.index(accessor):]
        body = body[:body.index('\n    def ')]
        assert '_activeBeadRecScanSource()' in body
        # Idle fallback chain must remain for reads outside a running scan
        assert "_get_required_controller('Scan', 'scan')" in body
        assert 'self.getBeadRecScanSource()' in body


def test_communication_channel_uses_idle_beadrec_source_before_legacy_scan_methods():
    scan_source = _RasterLikeController()
    channel = CommunicationChannel.__new__(CommunicationChannel)
    channel._activeScanSource = None
    channel._CommunicationChannel__main = SimpleNamespace(
        controllers={'Scan': scan_source}
    )

    assert not hasattr(scan_source, 'getDimsScan')
    assert channel.getDimsScan() == [10, 20]
    assert channel.getScanStepSizes() == [0.05, 0.1]
