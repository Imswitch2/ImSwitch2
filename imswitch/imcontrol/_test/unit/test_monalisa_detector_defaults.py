"""Architecture regression: hardware and acquisition do not own MoNaLISA defaults."""

from imswitch.imcontrol.model.configeditor import resources



def test_detector_manager_has_no_processing_calibration_properties():
    from imswitch.imcontrol.model.managers.detectors.DetectorManager import DetectorManager

    assert not hasattr(DetectorManager, "monalisaSpotFwhmNm")
    assert not hasattr(DetectorManager, "monalisaScanOrientation")
    for name in ("APDManager", "HamamatsuManager", "TISManager", "ThorCamTSIManager"):
        props = resources.generated_schema_for(name)["properties"]
        assert "monalisaSpotFwhmNm" not in props
        assert "monalisaScanOrientation" not in props


