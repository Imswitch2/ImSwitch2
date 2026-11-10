"""Detector-scoped MoNaLISA reconstruction defaults recorded with the data."""

from types import SimpleNamespace

from imswitch.imcontrol.model.managers.RecordingManager import RecordingWorker, SaveFormat
from imswitch.imcontrol.model.managers.detectors.DetectorManager import configuredMonalisaSpotFwhm


def _worker(detector_properties):
    detector = SimpleNamespace(
        monalisaSpotFwhmNm=configuredMonalisaSpotFwhm(detector_properties)
    )
    manager = SimpleNamespace(detectorsManager={"Camera": detector})
    worker = RecordingWorker.__new__(RecordingWorker)
    worker._RecordingWorker__recordingManager = manager
    worker.saveFormat = SaveFormat.HDF5
    worker.recLapseTotal = 1
    worker.recLapseIndex = 0
    worker.singleLapseFile = False
    worker.acquisitionLayouts = {}
    # An uninitialised QObject raises RuntimeError, not AttributeError, for a
    # missing attribute, so getattr() defaults cannot stand in for these.
    worker.recLapseIntervalS = None
    worker.recLapseScheduledTime = None
    worker.recordingGeneration = 0
    return worker


def test_detector_monalisa_fwhm_default_is_snapshotted_into_recording_attrs():
    attrs = _worker({"monalisaSpotFwhmNm": 215.0})._augment_attrs_with_recording_metadata(
        {"Camera": {}}
    )["Camera"]
    assert attrs["recording:detector_name"] == "Camera"
    assert attrs["monalisa:spot_fwhm_nm"] == 215.0


def test_missing_or_invalid_detector_fwhm_is_not_invented():
    for properties in ({}, {"monalisaSpotFwhmNm": None}, {"monalisaSpotFwhmNm": "bad"}):
        attrs = _worker(properties)._augment_attrs_with_recording_metadata(
            {"Camera": {}}
        )["Camera"]
        assert "monalisa:spot_fwhm_nm" not in attrs
