"""Layout-agnostic metadata reading for HDF5 / Zarr / TIFF measurement files."""

import json
import os

import h5py
import numpy as np
import pytest
import tifffile as tiff
import zarr

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.metadata_tree import (
    KIND_ARRAY,
    KIND_ATTRIBUTE,
    KIND_ERROR,
    KIND_GROUP,
    KIND_INFO,
    MetadataLimits,
    format_metadata_value,
    metadata_tree_from_container,
    metadata_tree_rows,
    metadata_tree_to_dict,
    read_metadata_tree,
)


def _paths(tree):
    return {node.path for node in tree.iter_nodes()}


def _node(tree, path):
    found = tree.find(path)
    assert found is not None, f"{path!r} not in {sorted(_paths(tree))}"
    return found


# -- HDF5 ---------------------------------------------------------------------


@pytest.fixture
def imswitch_h5(tmp_path):
    """A file laid out like an ImSwitch recording (root/detector/metadata)."""
    path = tmp_path / "rec.h5"
    with h5py.File(path, "w") as file:
        file.attrs["timestamp"] = "2026-08-02T10:00:00"
        file.attrs["rec_mode"] = "snap"
        detector = file.create_group("Camera")
        data = detector.create_dataset("data", data=np.zeros((3, 4, 5), dtype="uint16"))
        data.attrs["detector_name"] = "Camera"
        data.attrs["element_size_um"] = [1.0, 0.082, 0.082]
        metadata = detector.create_group("metadata")
        metadata.attrs["free_key"] = 3
        lasers = metadata.create_group("lasers")
        lasers.attrs["488 Laser"] = 12.5
    return path


def test_hdf5_walk_surfaces_groups_arrays_and_attributes(imswitch_h5):
    tree = read_metadata_tree(imswitch_h5)

    assert _node(tree, "rec_mode").kind == KIND_ATTRIBUTE
    assert _node(tree, "Camera").kind == KIND_GROUP
    assert _node(tree, "Camera/data").kind == KIND_ARRAY
    assert _node(tree, "Camera/data").detail == "(3, 4, 5) uint16"
    assert _node(tree, "Camera/data/detector_name").value == "Camera"
    assert _node(tree, "Camera/metadata/lasers/488 Laser").value == 12.5


def test_hdf5_walk_is_layout_agnostic(tmp_path):
    """A hierarchy nothing in ImSwitch writes still reads out in full."""
    path = tmp_path / "exotic.h5"
    with h5py.File(path, "w") as file:
        deep = file.create_group("a").create_group("b").create_group("c")
        deep.attrs["who"] = "made me"
        deep.create_dataset("payload", data=np.arange(4))

    tree = read_metadata_tree(path)

    assert _node(tree, "a/b/c/who").value == "made me"
    assert _node(tree, "a/b/c/payload").kind == KIND_ARRAY


def test_tree_is_usable_after_the_file_is_closed(imswitch_h5):
    """Values are materialized during the walk, so nothing lazily reopens."""
    tree = read_metadata_tree(imswitch_h5)
    imswitch_h5.unlink()

    assert _node(tree, "Camera/metadata/lasers/488 Laser").value == 12.5


def test_reading_an_open_container_does_not_close_it(imswitch_h5):
    with h5py.File(imswitch_h5, "r") as file:
        tree = metadata_tree_from_container(file, source_path=imswitch_h5)
        assert _node(tree, "Camera/data").kind == KIND_ARRAY
        # Still usable: metadata_tree_from_container never owns the handle.
        assert file["Camera/data"].shape == (3, 4, 5)


# -- Zarr ---------------------------------------------------------------------


def test_zarr_walk_surfaces_groups_arrays_and_attributes(tmp_path):
    # Reuse the production zarr helper so this works on zarr v2 and v3 alike.
    from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer

    path = tmp_path / "rec.zarr"
    root = zarr.open(str(path), mode="w")
    root.attrs["rec_mode"] = "recording"
    detector = root.create_group("Camera")
    detector.attrs["multiscales"] = [
        {"axes": [{"name": "t"}, {"name": "y"}], "datasets": [{"path": "data"}]}
    ]
    ZarrStorer._create_array(
        detector,
        "data",
        data=np.zeros((2, 3, 4), dtype="uint16"),
        chunks=(1, 3, 4),
    )

    tree = read_metadata_tree(path)

    assert _node(tree, "rec_mode").value == "recording"
    assert _node(tree, "Camera/data").kind == KIND_ARRAY
    assert "uint16" in _node(tree, "Camera/data").detail
    # NGFF metadata is a nested structure, and reads as a hierarchy.
    assert _node(tree, "Camera/multiscales/[0]/axes/[0]/name").value == "t"


# -- TIFF ---------------------------------------------------------------------


def test_ome_tiff_walk_surfaces_series_tags_and_ome_xml(tmp_path):
    path = tmp_path / "rec.ome.tif"
    tiff.imwrite(
        path,
        np.zeros((2, 4, 5), dtype="uint16"),
        metadata={"axes": "TYX", "PhysicalSizeX": 0.082},
    )

    tree = read_metadata_tree(path)
    paths = _paths(tree)

    assert any(path_.endswith("/axes") for path_ in paths)
    assert any("TIFF tags/ImageWidth" in path_ for path_ in paths)
    # The embedded OME-XML is parsed rather than shown as one unreadable line.
    assert any(path_.endswith("OME/Image/Pixels/SizeX") for path_ in paths)


# -- value expansion ----------------------------------------------------------


def test_json_string_attributes_expand_into_children(tmp_path):
    path = tmp_path / "json.h5"
    with h5py.File(path, "w") as file:
        file.attrs["scan"] = json.dumps({"axes": {"X": {"step_um": 0.1}}})

    tree = read_metadata_tree(path)

    assert _node(tree, "scan/axes/X/step_um").value == 0.1
    # The raw string is preserved on the parent node.
    assert "step_um" in _node(tree, "scan").value


def test_flat_scalar_sequences_stay_single_line(tmp_path):
    path = tmp_path / "flat.h5"
    with h5py.File(path, "w") as file:
        file.attrs["axes"] = ["T", "Y", "X"]
        file.attrs["element_size_um"] = [1.0, 0.082, 0.082]

    tree = read_metadata_tree(path)

    assert _node(tree, "axes").children == ()
    assert _node(tree, "element_size_um").children == ()
    assert _node(tree, "element_size_um").display_value() == "[1, 0.082, 0.082]"


def test_format_metadata_value_handles_common_attribute_types():
    assert format_metadata_value(b"bytes") == "bytes"
    assert format_metadata_value(np.float32(0.5)) == "0.5"
    assert format_metadata_value(np.array([1, 2, 3])) == "[1, 2, 3]"
    assert format_metadata_value(True) == "true"
    assert "shape=(100,)" in format_metadata_value(np.arange(100))
    assert format_metadata_value("x" * 50, max_chars=10).startswith("xxxxxxxxxx…")


# -- robustness ---------------------------------------------------------------


def test_unreadable_file_yields_an_error_node_not_an_exception(tmp_path):
    path = tmp_path / "broken.h5"
    path.write_bytes(b"this is not HDF5")

    tree = read_metadata_tree(path)

    assert any(node.kind == KIND_ERROR for node in tree.iter_nodes())


def test_unsupported_extension_yields_an_error_node(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello")

    tree = read_metadata_tree(path)

    assert any(node.kind == KIND_ERROR for node in tree.iter_nodes())
    assert _node(tree, "File/path").value == str(path)


def test_limits_truncate_instead_of_hanging(tmp_path):
    path = tmp_path / "wide.h5"
    with h5py.File(path, "w") as file:
        for index in range(40):
            file.attrs[f"key_{index:02d}"] = index

    tree = read_metadata_tree(path, limits=MetadataLimits(max_children=5))

    shown = [node for node in tree.iter_nodes() if node.path.startswith("key_")]
    assert len(shown) == 5
    assert any(node.kind == KIND_INFO and "more" in node.name for node in tree.iter_nodes())


def test_node_budget_stops_the_walk(tmp_path):
    path = tmp_path / "many.h5"
    with h5py.File(path, "w") as file:
        for index in range(50):
            file.create_group(f"g{index:02d}").attrs["i"] = index

    tree = read_metadata_tree(path, limits=MetadataLimits(max_nodes=10))

    assert any(node.kind == KIND_INFO and "stopped" in node.name for node in tree.iter_nodes())


# -- exports ------------------------------------------------------------------


def test_rows_flatten_every_attribute(imswitch_h5):
    rows = metadata_tree_rows(read_metadata_tree(imswitch_h5))
    by_path = {row["path"]: row["value"] for row in rows}

    assert by_path["Camera/metadata/lasers/488 Laser"] == "12.5"
    assert by_path["rec_mode"] == "snap"
    # Groups carry no value of their own and are not tabulated.
    assert "Camera/metadata" not in by_path


def test_tree_serializes_to_json(imswitch_h5):
    payload = metadata_tree_to_dict(read_metadata_tree(imswitch_h5))

    text = json.dumps(payload)  # must not raise on numpy attribute values
    assert "488 Laser" in text
