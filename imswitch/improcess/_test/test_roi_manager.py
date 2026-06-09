import numpy as np
import pytest

from imswitch.improcess.analysis.roi_manager import (
    ROIManagerModel,
    ROIRecord,
    rectangle_roi_from_vertices,
)


def test_roi_manager_adds_unique_names_and_computes_stats():
    image = np.arange(25, dtype=float).reshape(5, 5)
    model = ROIManagerModel()

    first = model.add(ROIRecord("cell", "rectangle", (0, 2, 0, 2)))
    second = model.add(ROIRecord("cell", "rectangle", (2, 5, 2, 5)))
    stats = model.compute_stats(image)

    assert first.name == "cell"
    assert second.name == "cell_1"
    assert len(stats) == 2
    assert stats[0].stats.mean == pytest.approx(float(image[0:2, 0:2].mean()))
    assert stats[1].stats.total == pytest.approx(float(image[2:5, 2:5].sum()))


def test_roi_manager_rename_duplicate_visibility_and_serialization():
    model = ROIManagerModel(
        [ROIRecord("roi", "mask", (1, 4, 2, 5), pixels=((1, 2), (2, 3)))]
    )

    model.rename("roi", "nucleus")
    duplicate = model.duplicate("nucleus")
    model.set_visible("nucleus", False)
    restored = ROIManagerModel.from_dicts(model.to_dicts())

    assert duplicate.name == "nucleus_copy"
    assert restored.get("nucleus").visible is False
    assert restored.get("nucleus_copy").bounds == (1, 4, 2, 5)
    assert restored.get("nucleus_copy").pixels == ((1, 2), (2, 3))


def test_rectangle_roi_from_vertices_builds_bounds():
    vertices = np.array(
        [
            [1.2, 3.4],
            [1.2, 7.8],
            [5.6, 7.8],
            [5.6, 3.4],
        ]
    )

    roi = rectangle_roi_from_vertices(vertices, name="box")

    assert roi.name == "box"
    assert roi.roi_type == "rectangle"
    assert roi.bounds == (1, 6, 3, 8)


def test_roi_manager_visible_only_stats():
    image = np.ones((6, 6), dtype=float)
    model = ROIManagerModel(
        [
            ROIRecord("visible", "rectangle", (0, 2, 0, 2), visible=True),
            ROIRecord("hidden", "rectangle", (2, 4, 2, 4), visible=False),
        ]
    )

    assert len(model.compute_stats(image)) == 2
    assert len(model.compute_stats(image, visible_only=True)) == 1


def test_roi_manager_mask_roi_stats_use_exact_pixels():
    image = np.arange(9, dtype=float).reshape(3, 3)
    roi = ROIRecord(
        "mask",
        "mask",
        (0, 2, 0, 2),
        pixels=((0, 0), (0, 1), (1, 0)),
    )
    model = ROIManagerModel([roi])

    stats = model.compute_stats(image)[0].stats

    assert stats.area_pixels == 3
    assert stats.total == pytest.approx(float(image[0, 0] + image[0, 1] + image[1, 0]))
    assert stats.total != pytest.approx(float(image[0:2, 0:2].sum()))
