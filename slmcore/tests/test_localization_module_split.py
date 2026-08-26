"""Checks for the responsibility-based localization module layout."""

from slmcore.cgh.localization import register_lattice
from slmcore.cgh.localization.registration import register_lattice as package_register
from slmcore.cgh.localization.registration.pipeline import (
    register_lattice as pipeline_register,
)
from slmcore.cgh.localization.guidance import (
    LocalizationGuidance,
    resolve_localization_guidance,
)
from slmcore.cgh.localization.workflow import localize_measurement


def test_registration_public_api_is_owned_by_pipeline():
    assert register_lattice is package_register
    assert package_register is pipeline_register


def test_guidance_and_workflow_have_direct_final_apis():
    assert callable(resolve_localization_guidance)
    assert callable(localize_measurement)
    assert LocalizationGuidance.__name__ == "LocalizationGuidance"
