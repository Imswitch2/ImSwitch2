from pathlib import Path
from types import SimpleNamespace

from imswitch.improcess.model.dataset_sources import (
    LOCATOR_DIRECTORY,
    LOCATOR_FILE,
    preferred_source_spec,
    resolve_dataset_source,
    specs_for_reconstructor,
)


def test_resolve_dataset_source_climbs_from_zarr_child_path(tmp_path):
    zarr_root = tmp_path / "recording.zarr"
    chunk = zarr_root / "APD" / "data" / "0.0.0"
    chunk.parent.mkdir(parents=True)
    chunk.write_bytes(b"chunk")

    source = resolve_dataset_source(chunk)

    assert source.path == zarr_root
    assert source.original_path == chunk
    assert source.format_id == "zarr"


def test_reconstructor_extensions_become_source_specs():
    reconstructor = SimpleNamespace(file_extensions=["hdf5", "zarr"])

    specs = specs_for_reconstructor(reconstructor)

    assert [(spec.id, spec.locator) for spec in specs] == [
        ("hdf5", LOCATOR_FILE),
        ("zarr", LOCATOR_DIRECTORY),
    ]


def test_preferred_source_spec_honors_selected_extension():
    reconstructor = SimpleNamespace(file_extensions=["hdf5", "zarr"])
    specs = specs_for_reconstructor(reconstructor)

    assert preferred_source_spec(specs, "zarr").id == "zarr"
    assert preferred_source_spec(specs, "hdf5").id == "hdf5"
    assert preferred_source_spec(specs, None).id == "hdf5"


def test_unsupported_child_without_zarr_ancestor_reports_original_suffix(tmp_path):
    unsupported = Path(tmp_path / "loose" / "0.0.0")
    unsupported.parent.mkdir()
    unsupported.write_text("not a zarr chunk")

    try:
        resolve_dataset_source(unsupported)
    except ValueError as exc:
        assert 'Unsupported file extension ".0"' in str(exc)
    else:
        raise AssertionError("expected unsupported child path to fail")
