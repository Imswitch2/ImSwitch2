"""Detector-scoped MoNaLISA reconstruction defaults recorded with the data."""

from types import SimpleNamespace

from imswitch.imcontrol.model.managers.RecordingManager import RecordingWorker, SaveFormat
from imswitch.imcontrol.model.managers.detectors.DetectorManager import configuredMonalisaSpotFwhm, configuredMonalisaScanOrientation


def _worker(detector_properties):
    detector = SimpleNamespace(
        monalisaSpotFwhmNm=configuredMonalisaSpotFwhm(detector_properties),
        monalisaScanOrientation=configuredMonalisaScanOrientation(detector_properties),
    )
    manager = SimpleNamespace(detectorsManager={"Camera": detector})
    worker = object.__new__(RecordingWorker)
    worker._RecordingWorker__recordingManager = manager
    worker.saveFormat = SaveFormat.HDF5
    worker.recLapseTotal = 1
    worker.recLapseIndex = 0
    worker.singleLapseFile = False
    worker.acquisitionLayouts = {}
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


def test_detector_orientation_default_is_snapshotted_when_valid():
    attrs = _worker({"monalisaScanOrientation": "-y+x"})._augment_attrs_with_recording_metadata(
        {"Camera": {}}
    )["Camera"]
    assert attrs["monalisa:scan_orientation"] == "-y+x"


def test_invalid_or_missing_scan_orientation_is_not_snapshotted():
    for props in ({}, {"monalisaScanOrientation": "invalid"}, {"monalisaScanOrientation": None}):
        attrs = _worker(props)._augment_attrs_with_recording_metadata({"Camera": {}})["Camera"]
        assert "monalisa:scan_orientation" not in attrs
