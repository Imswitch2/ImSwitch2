"""Architecture regression: hardware and acquisition do not own MoNaLISA defaults."""

from pathlib import Path

from imswitch.imcontrol.model.configeditor import resources


_ROOT = Path(__file__).resolve().parents[2] / "model"


def test_detector_manager_has_no_processing_calibration_properties():
    from imswitch.imcontrol.model.managers.detectors.DetectorManager import DetectorManager

    assert not hasattr(DetectorManager, "monalisaSpotFwhmNm")
    assert not hasattr(DetectorManager, "monalisaScanOrientation")
    for name in ("APDManager", "HamamatsuManager", "TISManager", "ThorCamTSIManager"):
        props = resources.generated_schema_for(name)["properties"]
        assert "monalisaSpotFwhmNm" not in props
        assert "monalisaScanOrientation" not in props


def test_acquisition_metadata_does_not_inject_reconstruction_defaults():
    text = (_ROOT / "managers" / "RecordingManager.py").read_text(encoding="utf-8")
    assert "new_attrs['monalisa:spot_fwhm_nm']" not in text
    assert "new_attrs['monalisa:scan_orientation']" not in text
