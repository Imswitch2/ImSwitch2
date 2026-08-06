"""Phase 2f compatibility contracts for saved tiling datasets."""

import json

import numpy as np
import tifffile

from imswitch.imcommon.algorithms.tile_mosaic import (
    MANIFEST_NAME,
    assemble,
    assemble_dataset,
    detectors_in,
    LayoutOptions,
    PayloadSelection,
    assemble_payload,
    inspect_dataset,
    load_dataset,
    solve_layout,
)


def _write_tiff(path, data):
    tifffile.imwrite(path, np.asarray(data), photometric="minisblack")


def test_original_v1_wrapper_and_assemble_dataset_are_unchanged(tmp_path):
    folder = tmp_path / "legacy"
    folder.mkdir()
    first = np.arange(20, dtype=np.uint16).reshape(4, 5)
    second = first + 100
    _write_tiff(folder / "first.tiff", first)
    _write_tiff(folder / "second.tiff", second)
    manifest = folder / MANIFEST_NAME
    manifest.write_text(json.dumps({
        "format": "imswitch-tiling/1",
        "pixel_size_um": {"y": 0.5, "x": 0.5},
        "tile_step_um": 2.5,
        "tiles": [
            {
                "filename": "first.tiff",
                "grid": [0, 0],
                "stage_um": [0.0, 0.0],
                "pixel_xy": [0.0, 0.0],
            },
            {
                "filename": "second.tiff",
                "grid": [1, 0],
                "stage_um": [2.5, 0.0],
                "pixel_xy": [5.0, 0.0],
            },
        ],
    }), encoding="utf-8")

    loaded = load_dataset(manifest, progress=lambda _message: None)
    mosaic, assembled_dataset, moved = assemble_dataset(manifest, refine=False)
    expected = np.concatenate((first, second), axis=1).astype(np.float32)

    assert loaded.axes == assembled_dataset.axes == "YX"
    assert [tile.tile_id for tile in loaded.tiles] == [0, 1]
    np.testing.assert_array_equal(assemble(loaded), expected)
    np.testing.assert_array_equal(mosaic, expected)
    assert moved == 0


def test_interim_v2_files_map_stays_on_the_legacy_reader(tmp_path):
    folder = tmp_path / "interim"
    folder.mkdir()
    entries = []
    for tile_id, column in enumerate((0, 4)):
        alignment = np.full((4, 4), tile_id + 1, np.uint16)
        payload = np.stack((alignment + 10, alignment + 20))
        alignment_name = f"tile-{tile_id}-Alignment.tiff"
        payload_name = f"tile-{tile_id}-Camera.tiff"
        _write_tiff(folder / alignment_name, alignment)
        _write_tiff(folder / payload_name, payload)
        entries.append({
            "filename": alignment_name,
            "grid": [tile_id, 0],
            "stage_um": [float(column), 0.0],
            "pixel_xy": [float(column), 0.0],
            "files": {
                "Alignment": {
                    "filename": alignment_name,
                    "axes": "YX",
                    "stored_axes": "YX",
                    "shape": [4, 4],
                    "transform_to_alignment": "reference",
                },
                "Camera": {
                    "filename": payload_name,
                    "axes": "CYX",
                    "stored_axes": "CYX",
                    "shape": [2, 4, 4],
                    "transform_to_alignment": "identity",
                },
            },
        })
    manifest = folder / MANIFEST_NAME
    raw = {
        "format": "imswitch-tiling/2",
        "pixel_size_um": {"y": 1.0, "x": 1.0},
        "tile_step_um": 4.0,
        "tiles": entries,
    }
    manifest.write_text(json.dumps(raw), encoding="utf-8")

    index, completeness = inspect_dataset(manifest)
    alignment = load_dataset(manifest, progress=lambda _message: None)
    camera = load_dataset(
        manifest, detector="Camera", progress=lambda _message: None
    )
    camera_mosaic = assemble(camera)

    assert index.format == "imswitch-tiling/2-interim"
    assert detectors_in(raw) == ["Alignment", "Camera"]
    assert completeness.alignment_files_present == 2
    assert completeness.payloads_complete == {"Alignment": 2, "Camera": 2}
    assert alignment.axes == "YX"
    assert camera.axes == "CYX"
    assert camera_mosaic.shape == (2, 4, 8)
    np.testing.assert_array_equal(camera_mosaic[:, :, :4], camera.tiles[0].data)


def _write_undeclared_run(folder):
    """A v2 run whose second detector nobody declared a transform for."""
    folder.mkdir(parents=True, exist_ok=True)
    tiles = []
    for tile_id, column in enumerate((0, 4)):
        alignment = np.full((4, 4), tile_id + 1, np.uint16)
        alignment_name = f"tile-{tile_id}-APDgreen.tiff"
        _write_tiff(folder / alignment_name, alignment)
        payloads = {
            "APDgreen": {
                "path": alignment_name,
                "group": None,
                "detector": "APDgreen",
                "axes": "YX", "stored_axes": "YX",
                "shape": [4, 4], "stored_shape": [4, 4],
                "generation": tile_id, "complete": True,
                "transform_to_alignment": "reference",
            },
        }
        # One payload omits the key entirely, one says so explicitly. Both
        # describe the same situation and must behave identically.
        for name, declaration in (("APDred", ...), ("APDblue", "unknown")):
            payload = alignment + (30 if name == "APDred" else 60)
            payload_name = f"tile-{tile_id}-{name}.tiff"
            _write_tiff(folder / payload_name, payload)
            ref = {
                "path": payload_name,
                "group": None,
                "detector": name,
                "axes": "YX", "stored_axes": "YX",
                "shape": [4, 4], "stored_shape": [4, 4],
                "generation": tile_id, "complete": True,
            }
            if declaration is not ...:
                ref["transform_to_alignment"] = declaration
            payloads[name] = ref
        tiles.append({
            "grid": [0, tile_id],
            "stage_um": [float(column), 0.0],
            "pixel_xy": [float(column), 0.0],
            "alignment": {
                "detector": "APDgreen",
                "filename": alignment_name,
                "axes": "YX", "stored_axes": "YX",
                "shape": [4, 4], "stored_shape": [4, 4],
            },
            "payloads": payloads,
        })
    manifest = folder / MANIFEST_NAME
    manifest.write_text(json.dumps({
        "format": "imswitch-tiling/2",
        "pixel_size_um": {"y": 1.0, "x": 1.0},
        "tile_step_um": 4.0,
        "tiles": tiles,
    }), encoding="utf-8")
    return manifest


def test_a_payload_with_no_declared_transform_is_still_readable(tmp_path):
    manifest = _write_undeclared_run(tmp_path / "undeclared")

    index, completeness = inspect_dataset(manifest)

    assert sorted(completeness.payloads_complete) == [
        "APDblue", "APDgreen", "APDred"
    ]
    for name in ("APDred", "APDblue"):
        transform = index.tiles[0].payloads[name].transform_to_alignment
        assert transform.kind == "unknown"
        assert not transform.is_declared
        assert transform.is_identity   # the default it resolves to


def test_an_assumed_transform_is_not_reported_as_a_declared_one(tmp_path):
    manifest = _write_undeclared_run(tmp_path / "undeclared")
    index = inspect_dataset(manifest)[0]
    layout = solve_layout(index, LayoutOptions(refine=False))

    declared = assemble_payload(
        index, layout, PayloadSelection(detector="APDgreen")
    )
    for name in ("APDred", "APDblue"):
        assumed = assemble_payload(index, layout, PayloadSelection(detector=name))
        # The pixels come out either way — what differs is the honesty of
        # the label on how they were placed.
        assert assumed.data.shape == declared.data.shape
        assert assumed.provenance.transform_source == "assumed"
        assert assumed.provenance.transform["kind"] == "unknown"
    assert declared.provenance.transform_source == "manifest"
