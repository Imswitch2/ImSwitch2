import pytest

from slmcore import (
    CalibrationMismatchPolicy,
    DEFAULT_REGISTRIES,
    SectionSplitLayout,
    SLMGeometry,
    SLMIdentity,
    SLMSectionsSetup,
    SLMSetup,
    SLMSession,
)
from slmcore.application import SLMRuntimeFactory
from slmcore.calibration import SLMSectionCalibration,attach_calibration_geometry


def _factory():
    geometry = SLMGeometry(width=64,height=32,pixel_size_um=1.0)
    setup = SLMSetup(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        sections=SLMSectionsSetup(
            layout=SectionSplitLayout(n_sections=2,axis="x"),
            customizable=True,
        ),
    )
    return SLMRuntimeFactory(setup=setup,registries=DEFAULT_REGISTRIES)


def _calibration(geometry):
    return attach_calibration_geometry(
        SLMSectionCalibration(
            kx_per_um=0.01,ky_per_um=0.02,plane="Sample plane",
        ),
        geometry,
    )


def _changed_layout():
    return SectionSplitLayout(
        n_sections=2,axis="x",mode="manual",sizes=(30,34),
    )


def test_prepare_section_layout_is_side_effect_free_and_reports_mismatch():
    factory = _factory()
    runtime = factory.create_default()
    original = runtime.get_section_geometry("sec_0")
    runtime.set_section_calibration("sec_0",_calibration(original))
    session = SLMSession(runtime=runtime,runtime_factory=factory)

    prepared = session.prepare_section_layout_change(_changed_layout())

    assert prepared.changed
    assert prepared.calibration_mismatches
    assert session.runtime is runtime
    assert runtime.get_section_geometry("sec_0") == original


def test_section_layout_rejects_mismatch_by_default_and_supports_keep_clear():
    factory = _factory()
    runtime = factory.create_default()
    original = runtime.get_section_geometry("sec_0")
    runtime.set_section_calibration("sec_0",_calibration(original))
    session = SLMSession(runtime=runtime,runtime_factory=factory)
    prepared = session.prepare_section_layout_change(_changed_layout())

    with pytest.raises(ValueError,match="calibration geometry"):
        session.apply_section_layout_change(prepared)

    assert session.apply_section_layout_change(
        prepared,calibration_mismatch_policy=CalibrationMismatchPolicy.KEEP,
    )
    kept = session.runtime.get_section_calibration_copy("sec_0")
    assert kept is not None and kept.is_valid()
    assert kept.section_geometry == _calibration(original).section_geometry

    # Rebuild the original state to exercise CLEAR independently.
    runtime = factory.create_default()
    original = runtime.get_section_geometry("sec_0")
    runtime.set_section_calibration("sec_0",_calibration(original))
    session = SLMSession(runtime=runtime,runtime_factory=factory)
    prepared = session.prepare_section_layout_change(_changed_layout())
    assert session.apply_section_layout_change(
        prepared,calibration_mismatch_policy=CalibrationMismatchPolicy.CLEAR,
    )
    cleared = session.runtime.get_section_calibration_copy("sec_0")
    assert cleared is None or not cleared.is_valid()


def test_prepared_section_layout_rejects_stale_runtime_layout():
    factory = _factory()
    runtime = factory.create_default()
    session = SLMSession(runtime=runtime,runtime_factory=factory)
    prepared = session.prepare_section_layout_change(_changed_layout())

    first = session.prepare_section_layout_change(
        SectionSplitLayout(
            n_sections=2,axis="x",mode="manual",sizes=(28,36),
        )
    )
    assert session.apply_section_layout_change(
        first,calibration_mismatch_policy=CalibrationMismatchPolicy.KEEP,
    )

    with pytest.raises(RuntimeError,match="prepare it again"):
        session.apply_section_layout_change(
            prepared,calibration_mismatch_policy=CalibrationMismatchPolicy.KEEP,
        )
