from __future__ import annotations

import h5py
import numpy as np
import pytest

from slmcore import (
    DEFAULT_REGISTRIES,
    SLM_CONFIG_SCHEMA_VERSION,
    SLMConfig,
    SLMGeometry,
    SLMIdentity,
    SLMRuntime,
)
from slmcore.core.cgh import CGHResult
from slmcore.core.engine.section import split_slm_geometry
from slmcore.workspace import SLMConfigStore


def _runtime_with_committed_cgh():
    geometry = SLMGeometry(width=64,height=64,pixel_size_um=1.0)
    runtime = SLMRuntime(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )
    section_key = "sec_0"
    runtime.apply_section_patch(
        section_key,
        {
            ("cgh","active"):True,
            ("cgh","selected_target"):"multi_foci_vector",
            ("cgh","multi_foci_vector","params","n_foci_x"):2,
            ("cgh","multi_foci_vector","params","n_foci_y"):2,
        },
    )
    job = runtime.prepare_section_cgh(section_key)
    runtime.commit_section_cgh(
        section_key,
        CGHResult(
            generation=job.generation,
            spec=job.spec,
            target_name=job.target_name,
            pattern=np.ones(job.spec.context.shape,dtype=np.complex128),
        ),
    )
    return runtime,section_key


def test_schema1_migration_discards_legacy_cgh_session_but_preserves_target_state():
    runtime,section_key = _runtime_with_committed_cgh()
    raw = runtime.create_config().to_dict()
    raw["schema_version"] = 1
    raw["sections"][section_key]["cgh_session"]["committed_target"][
        "target_signature"
    ] = "legacy-signature-that-is-invalid-under-v2"

    migrated,warnings = SLMConfig.from_dict(raw,DEFAULT_REGISTRIES)

    assert migrated.schema_version == SLM_CONFIG_SCHEMA_VERSION == 3
    assert migrated.sections[section_key].cgh_session is None
    assert migrated.sections[section_key].state.cgh.selected_target == "multi_foci_vector"
    cgh_state = migrated.sections[section_key].state.to_dict()["cgh"]
    params = cgh_state["items"]["multi_foci_vector"]["params"]
    assert params["n_foci_x"] == 2
    assert params["n_foci_y"] == 2
    assert any(
        warning.path == ("sections",section_key,"cgh_session")
        and "recompute the CGH" in warning.message
        for warning in warnings
    )

    restored = SLMRuntime.from_config(migrated,registries=DEFAULT_REGISTRIES)
    inspection = restored.get_section_cgh_session_inspection(section_key)
    assert inspection.committed_target is None
    assert inspection.rounds == ()
    assert restored.get_section_snapshot(section_key).state.cgh.selected_target == (
        "multi_foci_vector"
    )


def test_schema2_migrates_to_schema3_and_keeps_cgh_session_strictly_validated():
    runtime,section_key = _runtime_with_committed_cgh()
    raw = runtime.create_config().to_dict()
    raw["schema_version"] = 2
    raw["sections"][section_key]["cgh_session"]["committed_target"][
        "target_signature"
    ] = "corrupt-v2-signature"

    config,warnings = SLMConfig.from_dict(raw,DEFAULT_REGISTRIES)

    assert warnings == ()
    assert config.schema_version == SLM_CONFIG_SCHEMA_VERSION == 3
    assert config.sections[section_key].fov_correction is None
    assert config.sections[section_key].cgh_session is not None
    with pytest.raises(RuntimeError,match="target signature"):
        SLMRuntime.from_config(config,registries=DEFAULT_REGISTRIES)


def test_hdf5_schema1_load_migrates_to_current_schema_and_reports_original_metadata(tmp_path):
    runtime,section_key = _runtime_with_committed_cgh()
    store = SLMConfigStore(tmp_path,DEFAULT_REGISTRIES)
    path = store.save(
        "legacy.h5",runtime.create_config(),"legacy",overwrite=False,
    ).path

    with h5py.File(str(path),"r+") as handle:
        group = handle["config"]
        if "schema_version" in group:
            del group["schema_version"]
        group.attrs["schema_version"] = "1"

    metadata = store.read_metadata(path)
    config,warnings = store.load(path)

    assert metadata.schema_version == 1
    assert config.schema_version == SLM_CONFIG_SCHEMA_VERSION == 3
    assert config.sections[section_key].cgh_session is None
    assert any("schema 1 to 2" in warning.message for warning in warnings)
