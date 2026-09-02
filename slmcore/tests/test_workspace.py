from slmcore import (
    DEFAULT_REGISTRIES,
    SLMGeometry,
    SLMIdentity,
    SLMSectionsDefinition,
    SLMDefinition,
    SLMWorkspace,
    SectionSplitLayout,
)


def _setup(*,key="slm",serial="SER123"):
    return SLMDefinition(
        identity=SLMIdentity(key,serial),
        geometry=SLMGeometry(width=16,height=8,pixel_size_um=1.0),
        sections=SLMSectionsDefinition(
            layout=SectionSplitLayout(n_sections=1),
        ),
    )


def test_workspace_namespaces_persistent_resources_by_serial(tmp_path):
    workspace = SLMWorkspace(tmp_path)
    setup = _setup(key="renamable_key",serial="PHYSICAL-001")
    store = workspace.config_store(setup.identity,DEFAULT_REGISTRIES)

    assert store.directory == tmp_path / "configs" / "PHYSICAL-001"
    assert store.directory.is_dir()
    assert "renamable_key" not in str(store.directory)


def test_workspace_owns_standard_resource_layout(tmp_path):
    workspace = SLMWorkspace(tmp_path)
    setup = _setup(serial="SER123")

    assert workspace.config_directory(setup.identity) == tmp_path / "configs" / "SER123"
    assert workspace.correction_directory(setup.identity) == tmp_path / "corrections" / "SER123"
    assert workspace.calibrations_root == tmp_path / "calibrations"

    store = workspace.correction_store(setup.identity)
    assert store.directory == tmp_path / "corrections" / "SER123"
    assert store.directory.is_dir()
    assert store.wavelength_table_file == "wavelength.json"


def test_workspace_supports_explicit_directory_overrides(tmp_path):
    external = tmp_path / "external-corrections"
    workspace = SLMWorkspace(
        tmp_path / "workspace",
        configs_dir="custom-configs",
        corrections_dir=external,
        calibrations_dir="custom-calibrations",
    )
    setup = _setup(serial="SER123")

    assert workspace.config_directory(setup.identity) == (
        tmp_path / "workspace" / "custom-configs" / "SER123"
    )
    assert workspace.correction_directory(setup.identity) == external / "SER123"
    assert workspace.calibrations_root == (
        tmp_path / "workspace" / "custom-calibrations"
    )


def test_workspace_position_references_are_workspace_wide_not_serial_scoped(tmp_path):
    from slmcore import PositionReference
    import numpy as np

    workspace = SLMWorkspace(tmp_path)
    first = _setup(key="first",serial="SER-A")
    second = _setup(key="second",serial="SER-B")

    store = workspace.position_reference_store
    reference = PositionReference(
        name="aligned OFF",
        plane_name="sample",
        lattice_indices=np.array([[0,1,0,1],[0,0,1,1]],dtype=np.int64),
        positions_px=np.array(
            [[10.0,20.0,10.0,20.0],[10.0,10.0,20.0,20.0]],
            dtype=np.float64,
        ),
        image_shape=(64,64),
        metadata={"source_slm_serial":first.identity.serial_number},
    )

    path = store.save(reference)
    assert path.is_relative_to(tmp_path / "position_references")
    assert first.identity.serial_number not in str(path)
    assert second.identity.serial_number not in str(path)
    assert store.list("sample") == ("aligned OFF",)

    restored = store.load("sample","aligned OFF")
    assert restored.name == reference.name
    assert restored.plane_name == "sample"
    np.testing.assert_array_equal(restored.lattice_indices,reference.lattice_indices)
    np.testing.assert_allclose(restored.positions_px,reference.positions_px)

    store.delete("sample","aligned OFF")
    assert store.list("sample") == ()
