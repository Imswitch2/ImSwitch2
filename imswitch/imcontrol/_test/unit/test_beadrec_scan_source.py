from types import SimpleNamespace

from imswitch.imcontrol.controller.CommunicationChannel import CommunicationChannel
from imswitch.imcontrol.controller.controllers._beadrec_scan_source import (
    BeadRecScanSource,
    BeadRecScanSourceMixin,
)



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
