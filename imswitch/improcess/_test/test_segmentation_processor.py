from pathlib import Path

import h5py
import numpy as np

from imswitch.improcess.analysis.segmentation import otsu_threshold, segment_image
from imswitch.improcess.model import PlotPayload, ProcessingResult
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.segmentation import (
    SegmentationProcessor,
    SegmentationResult,
)


class MinimalResult(ProcessingResult):
    def save(self, path: Path, fmt: str):
        pass


def test_segment_image_manual_threshold_filters_and_measures_regions():
    image = np.zeros((12, 14), dtype=np.float32)
    image[2:5, 3:7] = 10.0
    image[8:10, 9:12] = 6.0
    image[0, 0] = 100.0

    analysis = segment_image(
        image,
        threshold_method="manual",
        threshold_value=5.0,
        min_area=3,
    )

    assert analysis.threshold == 5.0
    assert analysis.labels.max() == 2
    assert analysis.binary_mask[0, 0]
    assert not analysis.mask[0, 0]
    assert [region.area_pixels for region in analysis.regions] == [12, 6]
    assert [region.bounds for region in analysis.regions] == [(2, 5, 3, 7), (8, 10, 9, 12)]
    assert [roi.name for roi in analysis.rois(name_prefix="Seg")] == ["Seg_1", "Seg_2"]
    assert analysis.rois(name_prefix="Seg")[0].source == "segmentation"
    assert analysis.rois(name_prefix="Seg")[0].roi_type == "mask"
    assert len(analysis.rois(name_prefix="Seg")[0].pixels) == 12
    assert analysis.region_rows()[0]["mean_intensity"] == 10.0


def test_segment_image_otsu_detects_bright_region():
    image = np.zeros((16, 16), dtype=np.float32)
    image[4:12, 5:13] = 20.0

    analysis = segment_image(image, threshold_method="otsu", min_area=10)

    assert len(analysis.regions) == 1
    assert analysis.regions[0].area_pixels == 64
    assert analysis.regions[0].bounds == (4, 12, 5, 13)
    assert analysis.metadata["threshold_method"] == "otsu"


def test_otsu_threshold_does_not_double_count_threshold_bin():
    image = np.array([0.0] * 5 + [1.0] + [2.0] * 5, dtype=np.float32)

    threshold = otsu_threshold(image, bins=3)

    assert np.isclose(threshold, 1.0 / 3.0)


def test_segment_image_triangle_and_yen_detect_bright_region():
    image = np.zeros((32, 32), dtype=np.float32)
    image[8:24, 10:22] = 12.0

    for method in ("triangle", "yen"):
        analysis = segment_image(image, threshold_method=method, min_area=20)

        assert len(analysis.regions) == 1
        assert analysis.regions[0].area_pixels == 16 * 12
        assert analysis.metadata["threshold_method"] == method


def test_segment_image_local_threshold_handles_uneven_background():
    rows, cols = 64, 64
    background = np.linspace(0, 8, cols, dtype=np.float32)[None, :]
    image = np.repeat(background, rows, axis=0)
    image[22:42, 24:44] += 15.0

    analysis = segment_image(
        image,
        threshold_method="local",
        min_area=100,
        local_block_size=21,
        local_offset=-2.0,
    )

    assert len(analysis.regions) == 1
    assert analysis.regions[0].area_pixels >= 250
    assert analysis.metadata["local_block_size"] == 21
    assert set(analysis.metadata["threshold_summary"]) == {"median", "min", "max"}


def test_segment_image_watershed_splits_touching_objects():
    yy, xx = np.ogrid[:64, :64]
    disk_a = (yy - 32) ** 2 + (xx - 25) ** 2 <= 12 ** 2
    disk_b = (yy - 32) ** 2 + (xx - 39) ** 2 <= 12 ** 2
    image = np.zeros((64, 64), dtype=np.float32)
    image[disk_a | disk_b] = 10.0

    analysis = segment_image(
        image,
        threshold_method="watershed",
        min_area=50,
        watershed_min_distance=8,
        fill_holes=True,
    )

    assert len(analysis.regions) == 2
    assert analysis.labels.max() == 2
    assert analysis.metadata["threshold_method"] == "watershed"


def test_segment_image_manual_threshold_can_use_watershed_label_method():
    yy, xx = np.ogrid[:64, :64]
    disk_a = (yy - 32) ** 2 + (xx - 25) ** 2 <= 12 ** 2
    disk_b = (yy - 32) ** 2 + (xx - 39) ** 2 <= 12 ** 2
    image = np.zeros((64, 64), dtype=np.float32)
    image[disk_a | disk_b] = 10.0

    analysis = segment_image(
        image,
        threshold_method="manual",
        threshold_value=1.0,
        label_method="watershed",
        min_area=50,
        watershed_min_distance=8,
        fill_holes=True,
    )

    assert len(analysis.regions) == 2
    assert analysis.metadata["threshold_method"] == "manual"
    assert analysis.metadata["label_method"] == "watershed"


def test_segment_image_bounded_hole_fill_and_physical_measurements():
    image = np.zeros((20, 20), dtype=np.float32)
    image[4:14, 4:14] = 10.0
    image[5, 5] = 0.0
    image[8:11, 8:11] = 0.0

    analysis = segment_image(
        image,
        threshold_method="manual",
        threshold_value=1.0,
        min_area=10,
        max_hole_area=4,
        pixel_size_um=(0.5, 2.0),
    )

    assert len(analysis.regions) == 1
    region = analysis.regions[0]
    assert region.area_pixels == 91
    assert region.bounds == (4, 14, 4, 14)
    assert region.bbox == (4, 4, 14, 14)
    assert region.area_um2 == 91.0
    assert region.height_um == 5.0
    assert region.width_um == 20.0
    assert "area_um2" not in analysis.region_rows()[0]
    assert analysis.region_rows(include_optional=True)[0]["area_um2"] == 91.0


def test_segment_image_cleanup_can_fill_holes_and_clear_border():
    image = np.zeros((32, 32), dtype=np.float32)
    image[0:8, 0:8] = 10.0
    image[10:24, 10:24] = 10.0
    image[14:18, 14:18] = 0.0

    analysis = segment_image(
        image,
        threshold_method="manual",
        threshold_value=1.0,
        min_area=10,
        fill_holes=True,
        clear_border=True,
    )

    assert len(analysis.regions) == 1
    assert analysis.regions[0].area_pixels == 14 * 14
    assert analysis.regions[0].bounds == (10, 24, 10, 24)


def test_segmentation_processor_registered_and_generates_payload():
    data = np.zeros((2, 10, 10), dtype=np.float32)
    data[0, 2:7, 3:8] = 4.0
    data[1] = 100.0
    result = MinimalResult(
        name="stack",
        data=data,
        axis_labels=["T", "Y", "X"],
        axis_scales=[1.0, 0.2, 0.3],
        scale_unit="um",
    )
    processor = SegmentationProcessor()

    segmented = processor.apply(
        result,
        {
            "threshold_method": "manual",
            "threshold_value": 1.0,
            "min_area": 5,
            "smooth_sigma": 0.0,
            "background_radius": 0.0,
            "morphology_radius": 0,
            "fill_holes": False,
            "clear_border": False,
            "local_block_size": 51,
            "local_offset": 0.0,
            "watershed_min_distance": 5,
        },
    )

    assert "segmentation" in available_processor_ids()
    assert isinstance(segmented, SegmentationResult)
    assert segmented.data.shape == (10, 10)
    assert segmented.axis_scales == [0.2, 0.3]
    assert segmented.scale_unit == "um"
    assert segmented.data.max() == 1
    assert segmented.analysis.regions[0].area_pixels == 25
    payloads = segmented.plot_payloads()
    assert len(payloads) == 1
    assert isinstance(payloads[0], PlotPayload)
    assert payloads[0].metadata["region_count"] == 1


def test_segmentation_processor_uses_requested_tzc_plane():
    data = np.zeros((2, 3, 2, 10, 10), dtype=np.float32)
    data[1, 2, 1, 2:7, 3:8] = 4.0
    result = MinimalResult(name="stack", data=data, axis_labels=["T", "Z", "C", "Y", "X"])
    processor = SegmentationProcessor()

    segmented = processor.apply(
        result,
        {
            "threshold_method": "manual",
            "threshold_value": 1.0,
            "min_area": 5,
            "t_index": 1,
            "z_index": 2,
            "c_index": 1,
        },
    )

    assert segmented.analysis.regions[0].area_pixels == 25
    assert segmented.analysis.metadata["source_plane_indices"] == {"T": 1, "Z": 2, "C": 1}


def test_segmentation_processor_uses_named_axis_index_mapping():
    data = np.zeros((1, 2, 10, 10), dtype=np.float32)
    data[0, 1, 1:6, 2:7] = 5.0
    result = MinimalResult(name="components", data=data, axis_labels=["Dataset", "Base", "Y", "X"])
    processor = SegmentationProcessor()

    segmented = processor.apply(
        result,
        {
            "threshold_method": "manual",
            "threshold_value": 1.0,
            "min_area": 5,
            "axis_indices": "Dataset=0, Base=1",
        },
    )

    assert segmented.analysis.regions[0].area_pixels == 25
    assert segmented.analysis.metadata["source_plane_indices"] == {"Dataset": 0, "Base": 1}


def test_segmentation_processor_rejects_out_of_range_plane_index():
    data = np.zeros((1, 10, 10), dtype=np.float32)
    result = MinimalResult(name="stack", data=data, axis_labels=["T", "Y", "X"])
    processor = SegmentationProcessor()

    try:
        processor.apply(
            result,
            {
                "threshold_method": "manual",
                "threshold_value": 1.0,
                "t_index": 1,
            },
        )
    except ValueError as exc:
        assert "out of range" in str(exc)
    else:
        raise AssertionError("Expected out-of-range plane index to fail")


def test_segmentation_processor_rejects_unknown_named_axis_index():
    data = np.zeros((1, 2, 10, 10), dtype=np.float32)
    result = MinimalResult(name="components", data=data, axis_labels=["Dataset", "Base", "Y", "X"])
    processor = SegmentationProcessor()

    try:
        processor.apply(
            result,
            {
                "threshold_method": "manual",
                "threshold_value": 1.0,
                "axis_indices": "Bas=1",
            },
        )
    except ValueError as exc:
        assert "unknown axis label" in str(exc)
    else:
        raise AssertionError("Expected unknown axis label to fail")


def test_segmentation_result_saves_hdf5(tmp_path):
    image = np.zeros((8, 8), dtype=np.float32)
    image[1:5, 2:6] = 5.0
    analysis = segment_image(
        image,
        threshold_method="manual",
        threshold_value=1.0,
        min_area=3,
    )
    result = SegmentationResult(
        "segmentation",
        analysis,
        params={"min_area": 3},
        axis_scales=[0.4, 0.5],
        scale_unit="um",
    )
    out_path = tmp_path / "segmentation.h5"

    result.save(out_path, "hdf5")

    with h5py.File(out_path, "r") as h5:
        assert "labels" in h5
        assert "mask" in h5
        assert "regions" in h5
        assert h5["regions"]["area_pixels"][0] == 16
        assert tuple(h5["regions"]["bounds"][0]) == (1, 5, 2, 6)
        assert h5.attrs["region_count"] == 1
        assert h5.attrs["threshold"] == 1.0
        np.testing.assert_allclose(h5.attrs["axis_scales"], [0.4, 0.5])
        assert h5.attrs["scale_unit"] == "um"


def test_segmentation_result_declares_source_context_and_labels_layers():
    """The result renders as one list entry with two owned display layers:
    a context source image behind the primary labels mask."""
    image = np.zeros((20, 20), dtype=np.float32)
    image[4:9, 4:9] = 50.0
    src = MinimalResult(name="raw", data=image, axis_labels=["Y", "X"])

    result = SegmentationProcessor().apply(
        src, {"threshold_method": "otsu", "min_area": 4}
    )

    layers = result.display_layers()
    assert len(layers) == 2

    context, primary = layers
    assert context.kind == "image" and context.role == "context"
    assert context.component == "source"
    np.testing.assert_array_equal(np.asarray(context.data), image)

    assert primary.kind == "labels" and primary.role == "primary"
    assert primary.component == "labels"
    np.testing.assert_array_equal(np.asarray(primary.data), result.data)


def test_segmentation_result_context_layer_not_offered_as_processor_input():
    image = np.zeros((16, 16), dtype=np.float32)
    image[3:8, 3:8] = 40.0
    src = MinimalResult(name="raw", data=image, axis_labels=["Y", "X"])
    result = SegmentationProcessor().apply(src, {"threshold_method": "otsu", "min_area": 4})

    ids = [choice.id for choice in result.processor_input_choices()]
    assert "result" in ids
    assert "component:labels" in ids
    assert "component:source" not in ids  # context is display-only


def test_segmentation_result_without_source_image_falls_back_to_labels_only():
    from imswitch.improcess.analysis.segmentation import segment_image

    analysis = segment_image(
        np.pad(np.ones((4, 4), dtype=np.float32) * 10.0, 2),
        threshold_method="manual",
        threshold_value=5.0,
        min_area=1,
    )
    result = SegmentationResult("seg", analysis)  # no source_image

    layers = result.display_layers()
    assert len(layers) == 1
    assert layers[0].kind == "labels" and layers[0].role == "primary"
