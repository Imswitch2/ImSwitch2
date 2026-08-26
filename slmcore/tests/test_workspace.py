from slmcore import (
    DEFAULT_REGISTRIES,
    SLMGeometry,
    SLMIdentity,
    SLMSectionsSetup,
    SLMSetup,
    SLMWorkspace,
    SectionSplitLayout,
)


def _setup(*,key="slm",serial="SER123"):
    return SLMSetup(
        identity=SLMIdentity(key,serial),
        geometry=SLMGeometry(width=16,height=8,pixel_size_um=1.0),
        sections=SLMSectionsSetup(
            layout=SectionSplitLayout(n_sections=1),
        ),
    )


def test_workspace_namespaces_persistent_resources_by_serial(tmp_path):
    workspace = SLMWorkspace(tmp_path)
    setup = _setup(key="renamable_key",serial="PHYSICAL-001")
    repository = workspace.config_repository(setup,DEFAULT_REGISTRIES)

    assert repository.directory == tmp_path / "configs" / "PHYSICAL-001"
    assert repository.directory.is_dir()
    assert "renamable_key" not in str(repository.directory)


def test_workspace_owns_standard_resource_layout(tmp_path):
    workspace = SLMWorkspace(tmp_path)
    setup = _setup(serial="SER123")

    assert workspace.config_directory(setup) == tmp_path / "configs" / "SER123"
    assert workspace.correction_directory(setup) == tmp_path / "corrections" / "SER123"
    assert workspace.calibrations_root == tmp_path / "calibrations"

    store = workspace.correction_store(setup)
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

    assert workspace.config_directory(setup) == (
        tmp_path / "workspace" / "custom-configs" / "SER123"
    )
    assert workspace.correction_directory(setup) == external / "SER123"
    assert workspace.calibrations_root == (
        tmp_path / "workspace" / "custom-calibrations"
    )
