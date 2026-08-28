import pytest

from slmcore import (
    DEFAULT_REGISTRIES,SLMGeometry,SLMIdentity,SLMSectionsSetup,SLMSetup,
)
from slmcore.application import SLMConfigurationService,SLMRuntimeFactory
from slmcore.core.calibration import SLMSectionCalibration,attach_calibration_geometry
from slmcore.workspace import SLMConfigStore
from slmcore.core.engine.section import SectionSplitLayout,create_split_section_geometries


def _factory(tmp_path=None):
    geometry = SLMGeometry(width=64,height=32,pixel_size_um=1.0)
    setup_layout = SectionSplitLayout(n_sections=2,axis="x")
    setup = SLMSetup(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        sections=SLMSectionsSetup(
            layout=setup_layout,
            customizable=True,
        ),
    )
    store = (
        None if tmp_path is None
        else SLMConfigStore(tmp_path,DEFAULT_REGISTRIES)
    )
    factory = SLMRuntimeFactory(
        setup=setup,
        registries=DEFAULT_REGISTRIES,
    )
    service = (
        None if store is None
        else SLMConfigurationService(store=store,runtime_factory=factory)
    )
    return factory,service


def _calibration(geometry):
    return attach_calibration_geometry(
        SLMSectionCalibration(
            kx_per_um=0.01,ky_per_um=0.02,plane="Sample plane",
        ),
        geometry,
    )


def test_layout_replacement_keeps_or_clears_calibration_by_explicit_choice():
    factory,_repo = _factory()
    runtime = factory.create_default()
    original_geometry = runtime.get_section_geometry("sec_0")
    runtime.set_section_calibration("sec_0",_calibration(original_geometry))

    requested = create_split_section_geometries(
        runtime.geometry,
        SectionSplitLayout(
            n_sections=2,axis="x",mode="manual",sizes=(30,34),
        ),
    )

    kept = factory.create_layout_replacement(runtime,requested)
    kept_calibration = kept.get_section_calibration_copy("sec_0")
    assert kept_calibration.is_valid()
    # Keeping intentionally preserves where the calibration was measured; it
    # must not be relabelled as measured under the new geometry.
    assert kept_calibration.section_geometry == _calibration(
        original_geometry
    ).section_geometry
    assert kept.get_section_geometry("sec_0") == requested["sec_0"]

    cleared = factory.create_layout_replacement(
        runtime,requested,clear_calibration_sections=("sec_0",),
    )
    calibration = cleared.get_section_calibration_copy("sec_0")
    assert calibration is None or not calibration.is_valid()


def test_runtime_factory_keeps_section_count_fixed():
    factory,_repo = _factory()
    runtime = factory.create_default()
    one_section = create_split_section_geometries(
        runtime.geometry,SectionSplitLayout(n_sections=1,axis="x"),
    )
    with pytest.raises(ValueError,match="section count"):
        factory.create_layout_replacement(runtime,one_section)


def test_startup_rejects_calibration_geometry_mismatch_without_prompt(tmp_path):
    factory,service = _factory(tmp_path)
    runtime = factory.create_default()
    geometry = runtime.get_section_geometry("sec_0")
    runtime.set_section_calibration("sec_0",_calibration(geometry))
    config = runtime.create_config()

    # Persist an internally inconsistent config: its calibration explicitly
    # belongs to another section geometry.
    clone = config.sections["sec_0"].clone(DEFAULT_REGISTRIES)
    clone.calibration.section_geometry = {
        "key":"sec_0","x":0,"y":0,"width":30,"height":32,
    }
    config.sections["sec_0"] = clone
    service.store.save("bad.h5",config,"mismatch",overwrite=False)

    startup = service.create_startup("bad.h5")
    assert startup.config_path is None
    assert startup.warnings
    assert "calibration geometry" in startup.warnings[0].lower()
    assert startup.runtime.get_section_calibration_copy("sec_0") is None


def test_startup_loads_matching_calibration_config(tmp_path):
    factory,service = _factory(tmp_path)
    runtime = factory.create_default()
    geometry = runtime.get_section_geometry("sec_0")
    runtime.set_section_calibration("sec_0",_calibration(geometry))
    service.store.save("good.h5",runtime.create_config(),"matching",overwrite=False)

    startup = service.create_startup("good.h5")
    assert startup.config_path == str(service.store.resolve("good.h5"))
    loaded = startup.runtime.get_section_calibration_copy("sec_0")
    assert loaded is not None and loaded.is_valid()
    assert loaded.section_geometry == _calibration(geometry).section_geometry
