"""Phase 2f compatibility contracts at the ImProcess boundary."""

import numpy as np
import tifffile

from imswitch.improcess.model import DataObj
from imswitch.improcess.reconstructors import _AVAILABLE_RECONSTRUCTOR_CLASSES
from imswitch.improcess.reconstructors.view_only import ViewOnlyReconstructor


def test_non_tiling_reconstructors_keep_image_only_inline_defaults():
    """No reconstructor but the tiling one may opt into the manifest path.

    Asserted as "does not accept tiling-manifest" rather than "accepts exactly
    ('image',)", which was the same thing only while tiling was the sole
    non-image source kind. The SMLM localizer also declares "localizations" —
    a table that opens straight to a result and never reaches a reconstructor —
    and that must not weaken this contract. The time-lapse reconstructor runs
    on the worker too (it opens every file of a lapse for its header), but
    takes lapses, never manifests. So does the MoNaLISA lattice reconstructor,
    whose reconstruction of one scan takes half a minute. The polarisation map
    takes measurement runs only — a table of instrument samples, never an image.
    """
    worker_plugins = {"tiling-mosaic", "time-lapse", "monalisa-lattice", "monalisa"}
    image_less = {"polarisation-map"}
    for plugin_id, plugin_class in _AVAILABLE_RECONSTRUCTOR_CLASSES.items():
        if plugin_id == "tiling-mosaic":
            continue
        if plugin_id in image_less:
            assert "image" not in plugin_class.accepted_source_kinds
        else:
            assert "image" in plugin_class.accepted_source_kinds
        assert "tiling-manifest" not in plugin_class.accepted_source_kinds
        expected = "worker" if plugin_id in worker_plugins else "inline"
        assert plugin_class.execution_policy == expected


def test_ordinary_image_data_obj_and_view_only_result_are_unchanged(tmp_path):
    image = np.arange(30, dtype=np.uint16).reshape(5, 6)
    path = tmp_path / "ordinary.ome.tiff"
    tifffile.imwrite(path, image, photometric="minisblack")

    data_obj = DataObj(path.name, "Camera", path=str(path))
    result = ViewOnlyReconstructor().process(data_obj, {})

    assert data_obj.sourceKind == "image"
    assert result.axis_labels == ["Y", "X"]
    np.testing.assert_array_equal(result.data, image)

