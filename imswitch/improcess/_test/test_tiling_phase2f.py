"""Phase 2f compatibility contracts at the ImProcess boundary."""

import json
from types import SimpleNamespace

import numpy as np
import tifffile

from imswitch.imcommon.algorithms.tile_mosaic import (
    MANIFEST_NAME,
    assemble_dataset,
)
from imswitch.improcess.model import DataObj
from imswitch.improcess.reconstructors import _AVAILABLE_RECONSTRUCTOR_CLASSES
from imswitch.improcess.reconstructors.tiling import TilingReconstructor
from imswitch.improcess.reconstructors.view_only import ViewOnlyReconstructor


def test_non_tiling_reconstructors_keep_image_only_inline_defaults():
    for plugin_id, plugin_class in _AVAILABLE_RECONSTRUCTOR_CLASSES.items():
        if plugin_id == "tiling-mosaic":
            continue
        assert plugin_class.accepted_source_kinds == ("image",)
        assert plugin_class.execution_policy == "inline"


def test_ordinary_image_data_obj_and_view_only_result_are_unchanged(tmp_path):
    image = np.arange(30, dtype=np.uint16).reshape(5, 6)
    path = tmp_path / "ordinary.ome.tiff"
    tifffile.imwrite(path, image, photometric="minisblack")

    data_obj = DataObj(path.name, "Camera", path=str(path))
    result = ViewOnlyReconstructor().process(data_obj, {})

    assert data_obj.sourceKind == "image"
    assert result.axis_labels == ["Y", "X"]
    np.testing.assert_array_equal(result.data, image)


def test_legacy_alignment_only_reconstructor_matches_compatibility_wrapper(
    tmp_path,
):
    folder = tmp_path / "legacy-run"
    folder.mkdir()
    first = np.arange(20, dtype=np.uint16).reshape(4, 5)
    second = first + 100
    tifffile.imwrite(folder / "first.tiff", first, photometric="minisblack")
    tifffile.imwrite(folder / "second.tiff", second, photometric="minisblack")
    manifest = folder / MANIFEST_NAME
    manifest.write_text(json.dumps({
        "format": "imswitch-tiling/1",
        "pixel_size_um": {"y": 0.75, "x": 0.5},
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
    expected, dataset, _moved = assemble_dataset(manifest, refine=False)

    result = TilingReconstructor().process(
        SimpleNamespace(name="legacy", dataPath=str(manifest)),
        {
            "refine": False,
            "blend": True,
            "stage_positions": True,
            "detector": None,
            "project": False,
            "max_shift_px": None,
        },
    )

    assert result.axis_labels == list(dataset.axes) == ["Y", "X"]
    np.testing.assert_array_equal(result.data, expected)

