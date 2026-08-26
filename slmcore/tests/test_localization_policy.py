import numpy as np

from slmcore.cgh.localization import suggest_localization_sources
from slmcore.measurement import ImageMeasurement


def _measurement(source):
    return ImageMeasurement(image=np.zeros((8,8)),source=source)


def test_loaded_measurements_always_default_to_auto_sources():
    context = {
        "target_expected_period_px":(10.0,11.0),
        "target_stagger":0.5,
        "target_lattice_count":(31,31),
    }
    assert suggest_localization_sources(
        _measurement("file"),context,allow_target_hints=True,
    ) == {
        "period_prior_mode":"auto",
        "stagger_prior_mode":"auto",
        "lattice_size_prior_mode":"auto",
    }


def test_detector_measurement_uses_only_available_current_target_hints():
    context = {
        "target_expected_period_px":None,
        "target_stagger":0.5,
        "target_lattice_count":(31,29),
    }
    assert suggest_localization_sources(
        _measurement("detector"),context,allow_target_hints=True,
    ) == {
        "period_prior_mode":"auto",
        "stagger_prior_mode":"target",
        "lattice_size_prior_mode":"target",
    }


def test_detector_measurement_defaults_to_auto_when_target_hints_are_not_allowed():
    context = {
        "target_expected_period_px":(10.0,11.0),
        "target_stagger":0.5,
        "target_lattice_count":(31,31),
    }
    assert suggest_localization_sources(
        _measurement("detector"),context,allow_target_hints=False,
    ) == {
        "period_prior_mode":"auto",
        "stagger_prior_mode":"auto",
        "lattice_size_prior_mode":"auto",
    }
