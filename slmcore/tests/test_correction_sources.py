import json

import numpy as np
import pytest
from PIL import Image

from slmcore import (
    DEFAULT_REGISTRIES,
    CorrectionMismatchPolicy,
    CorrectionSourceInvalidatedError,
    ResolvedCorrections,
    SLMConfigurationService,
    SLMControlMode,
    SLMGeometry,
    SLMIdentity,
    SLMRuntime,
    SLMRuntimeFactory,
    SLMSectionConfig,
    SLMSectionsSetup,
    SLMSetup,
    SLMSession,
)
from slmcore.core.engine.section import SectionSplitLayout,split_slm_geometry
from slmcore.workspace import SLMConfigStore,SLMCorrectionStore


class FakeCorrectionProvider:
    def __init__(self,pattern_value,two_pi_value,*,directory="/current",name="current.bmp"):
        self.pattern_value = float(pattern_value)
        self.two_pi_value = int(two_pi_value)
        self.directory = directory
        self.name = name
        self.calls = []

    def resolve(self,wavelength_nm,geometry):
        self.calls.append((int(wavelength_nm),geometry))
        return ResolvedCorrections(
            wavelength_nm=int(wavelength_nm),
            geometry=geometry,
            correction_pattern=np.full(geometry.shape,self.pattern_value),
            two_pi_value=self.two_pi_value,
            source_directory=self.directory,
            pattern_filename=self.name,
            pattern_wavelength_nm=int(wavelength_nm),
            twopi_filename="wavelength.json",
            twopi_wavelength_nm=int(wavelength_nm),
            twopi_source="measurement",
        )


def _setup():
    return SLMSetup(
        identity=SLMIdentity("slm","SER123"),
        geometry=SLMGeometry(width=12,height=8,pixel_size_um=1.0),
        sections=SLMSectionsSetup(
            layout=SectionSplitLayout(n_sections=1,axis="x"),
            customizable=True,
        ),
    )


def _runtime(provider):
    setup = _setup()
    return SLMRuntime(
        identity=setup.identity,
        geometry=setup.geometry,
        section_geometries=setup.section_geometries,
        registries=DEFAULT_REGISTRIES,
        correction_provider=provider,
    )


def _service(tmp_path,provider):
    setup = _setup()
    factory = SLMRuntimeFactory(
        setup=setup,
        registries=DEFAULT_REGISTRIES,
        correction_provider=provider,
    )
    store = SLMConfigStore(tmp_path,DEFAULT_REGISTRIES)
    return SLMConfigurationService(store=store,runtime_factory=factory),factory


def test_correction_store_resolves_complete_resources_and_provenance(tmp_path):
    identity = SLMIdentity("slm","SER123")
    image = np.arange(12 * 8,dtype=np.uint8).reshape(8,12)
    Image.fromarray(image).save(tmp_path / "CAL_SER123_490nm.bmp")
    (tmp_path / "wavelength.json").write_text(json.dumps({
        "manufacturer":{"490nm":201},
        "measurement":{"490nm":199},
    }),encoding="utf-8")
    geometry = split_slm_geometry(
        SLMGeometry(width=12,height=8,pixel_size_um=1.0),2,
    )["sec_1"]

    resolved = SLMCorrectionStore(
        identity=identity,directory=tmp_path,wavelength_table_file="wavelength.json",
    ).resolve(488,geometry)

    np.testing.assert_array_equal(resolved.correction_pattern,image[:,6:].astype(float))
    assert resolved.two_pi_value == 199
    assert resolved.source_directory == str(tmp_path.resolve())
    assert resolved.pattern_filename == "CAL_SER123_490nm.bmp"
    assert resolved.pattern_wavelength_nm == 490
    assert resolved.twopi_filename == "wavelength.json"
    assert resolved.twopi_wavelength_nm == 490
    assert resolved.twopi_source == "measurement"


def test_config_snapshot_is_exact_computed_resolution_and_save_does_not_requery(tmp_path):
    provider = FakeCorrectionProvider(7,203,directory="/historical",name="hist.bmp")
    runtime = _runtime(provider)
    section_key = runtime.section_keys[0]
    computed = runtime.get_section_artifacts(section_key).resolved_corrections
    calls_after_compute = len(provider.calls)

    config = runtime.create_config()
    assert len(provider.calls) == calls_after_compute
    saved = config.sections[section_key].correction_snapshot
    assert saved.numerically_equal(computed)
    assert saved.to_dict()["source_directory"] == computed.source_directory
    assert saved.source_directory == "/historical"
    assert saved.pattern_filename == "hist.bmp"

    store = SLMConfigStore(tmp_path,DEFAULT_REGISTRIES)
    store.save("snapshot.h5",config,overwrite=False)
    assert len(provider.calls) == calls_after_compute

    loaded,_warnings = store.load("snapshot.h5")
    restored = loaded.sections[section_key].correction_snapshot
    assert restored.numerically_equal(saved)
    assert restored.source_directory == saved.source_directory
    assert restored.pattern_filename == saved.pattern_filename


def test_config_mismatch_compares_effective_values_not_provenance(tmp_path):
    saved_provider = FakeCorrectionProvider(5,200,directory="/old",name="old.bmp")
    saved_runtime = _runtime(saved_provider)
    saved_config = saved_runtime.create_config()

    same_values = FakeCorrectionProvider(5,200,directory="/new",name="new.bmp")
    service,_factory = _service(tmp_path,same_values)
    service.store.save("same.h5",saved_config,overwrite=False)
    prepared = service.prepare_load(_runtime(same_values),"same.h5")

    assert prepared.correction_mismatches == ()


def test_correction_mismatch_can_pin_saved_or_use_current_and_wavelength_invalidates_pin(tmp_path):
    saved_provider = FakeCorrectionProvider(5,200,directory="/old",name="old.bmp")
    saved_runtime = _runtime(saved_provider)
    saved_runtime.apply_section_patch(saved_runtime.section_keys[0],{("corrections","active"):True})
    saved_config = saved_runtime.create_config()

    current_provider = FakeCorrectionProvider(9,220,directory="/new",name="new.bmp")
    service,factory = _service(tmp_path,current_provider)
    service.store.save("mismatch.h5",saved_config,overwrite=False)

    runtime = factory.create_default()
    prepared = service.prepare_load(runtime,"mismatch.h5")
    assert len(prepared.correction_mismatches) == 1
    mismatch = prepared.correction_mismatches[0]
    assert mismatch.pattern_changed
    assert mismatch.two_pi_changed

    with pytest.raises(ValueError,match="Saved corrections differ"):
        service.commit_load(runtime,prepared,require_complete=True)

    runtime = factory.create_default()
    prepared = service.prepare_load(runtime,"mismatch.h5")
    commit = service.commit_load(
        runtime,prepared,
        correction_mismatch_policy=CorrectionMismatchPolicy.USE_SAVED,
        require_complete=True,
    )
    runtime = commit.runtime
    section_key = runtime.section_keys[0]
    assert runtime.section_uses_saved_corrections(section_key)
    assert runtime.get_section_artifacts(section_key).resolved_corrections.two_pi_value == 200

    runtime.apply_section_patch(section_key,{("optics","pupil_radius_px"):2})
    assert runtime.section_uses_saved_corrections(section_key)
    assert runtime.get_section_artifacts(section_key).resolved_corrections.two_pi_value == 200

    with pytest.raises(CorrectionSourceInvalidatedError):
        runtime.apply_section_patch(section_key,{("optics","wavelength_nm"):561})
    assert runtime.get_section_parameter(section_key,("optics","wavelength_nm")) == 488

    runtime.apply_section_patch(
        section_key,{("optics","wavelength_nm"):561},
        use_workspace_corrections=True,
    )
    assert not runtime.section_uses_saved_corrections(section_key)
    assert runtime.get_section_artifacts(section_key).resolved_corrections.two_pi_value == 220
    assert runtime.get_section_artifacts(section_key).resolved_corrections.wavelength_nm == 561

    current_runtime = factory.create_default()
    prepared = service.prepare_load(current_runtime,"mismatch.h5")
    service.commit_load(
        current_runtime,prepared,
        correction_mismatch_policy=CorrectionMismatchPolicy.USE_CURRENT,
        require_complete=True,
    )
    assert not current_runtime.section_uses_saved_corrections(section_key)
    assert current_runtime.get_section_artifacts(section_key).resolved_corrections.two_pi_value == 220


def test_disabled_saved_corrections_do_not_trigger_load_mismatch(tmp_path):
    saved_provider = FakeCorrectionProvider(5,200)
    runtime = _runtime(saved_provider)
    section_key = runtime.section_keys[0]
    runtime.apply_section_patch(section_key,{("corrections","active"):False})
    config = runtime.create_config()

    current_provider = FakeCorrectionProvider(99,230)
    service,_factory = _service(tmp_path,current_provider)
    service.store.save("disabled.h5",config,overwrite=False)
    prepared = service.prepare_load(_runtime(current_provider),"disabled.h5")

    assert prepared.correction_mismatches == ()


def test_fast_exit_is_strict_correction_decision_even_for_same_config_path(tmp_path):
    saved_provider = FakeCorrectionProvider(5,200)
    saved_runtime = _runtime(saved_provider)
    saved_runtime.apply_section_patch(saved_runtime.section_keys[0],{("corrections","active"):True})
    saved_config = saved_runtime.create_config()
    current_provider = FakeCorrectionProvider(9,220)
    service,factory = _service(tmp_path,current_provider)
    service.store.save("fast.h5",saved_config,overwrite=False)

    session = SLMSession(
        runtime=factory.create_default(),
        runtime_factory=factory,
        configuration_service=service,
    )
    session.load_config(
        "fast.h5",
        correction_mismatch_policy=CorrectionMismatchPolicy.USE_SAVED,
        require_complete=True,
    )
    assert session.set_control_mode(SLMControlMode.FAST_CONFIG)
    assert session.fast_config_path == session.current_config_path

    with pytest.raises(ValueError,match="Saved corrections differ"):
        session.set_control_mode(SLMControlMode.EDITOR)
    assert session.control_mode is SLMControlMode.FAST_CONFIG

    assert session.set_control_mode(
        SLMControlMode.EDITOR,
        correction_mismatch_policy=CorrectionMismatchPolicy.USE_CURRENT,
    )
    assert session.control_mode is SLMControlMode.EDITOR
    assert not session.runtime.section_uses_saved_corrections(session.runtime.section_keys[0])


def test_correction_store_warnings_are_deduplicated_until_cache_invalidation(tmp_path,caplog):
    identity = SLMIdentity("slm","SER123")
    geometry = split_slm_geometry(
        SLMGeometry(width=12,height=8,pixel_size_um=1.0),1,
    )["sec_0"]
    store = SLMCorrectionStore(
        identity=identity,directory=tmp_path,wavelength_table_file="wavelength.json",
    )

    with caplog.at_level("WARNING",logger="slmcore.workspace.correction_store"):
        store.resolve(488,geometry)
        store.resolve(488,geometry)
        store.resolve(561,geometry)

    messages = [record.getMessage() for record in caplog.records]
    assert sum("No correction patterns found" in message for message in messages) == 1
    assert sum("2pi wavelength table not found" in message for message in messages) == 1
    assert sum("No 2pi values found" in message for message in messages) == 1

    caplog.clear()
    store.invalidate_cache()
    with caplog.at_level("WARNING",logger="slmcore.workspace.correction_store"):
        store.resolve(488,geometry)

    messages = [record.getMessage() for record in caplog.records]
    assert sum("No correction patterns found" in message for message in messages) == 1
    assert sum("2pi wavelength table not found" in message for message in messages) == 1
    assert sum("No 2pi values found" in message for message in messages) == 1
