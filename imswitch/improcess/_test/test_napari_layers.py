"""Results become napari layers by kind, and only when that is honest."""

import numpy as np
import pytest

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.labels_result import LabelsResult
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import localizations_from_columns
from imswitch.improcess.model.napari_layers import (
    LAYERABLE_KINDS,
    NotLayerable,
    localization_transform,
    result_to_layer_data,
)
from imswitch.improcess.model.points_table_result import PointsTableResult


def _image(shape=(4, 8, 8), unit="um"):
    return ArrayProcessingResult(
        name="recon",
        data=np.random.default_rng(1).random(shape).astype(np.float32),
        axis_labels=["Z", "Y", "X"][-len(shape):],
        axis_scales=[0.5] * len(shape),
        scale_unit=unit,
    )


def _locs(three_d=False, x0=0.0, y0=0.0):
    columns = {
        "frame": np.array([0, 0, 1], dtype=np.int32),
        "x_nm": np.array([x0 + 100.0, x0 + 250.0, x0 + 400.0]),
        "y_nm": np.array([y0 + 50.0, y0 + 75.0, y0 + 300.0]),
        "photons": np.array([1000.0, 2000.0, 1500.0]),
    }
    if three_d:
        columns["z_nm"] = np.array([0.0, 300.0, -150.0])
    return localizations_from_columns(columns)


def test_an_image_result_is_one_image_layer_with_scale_units_and_provenance_slot():
    layers = result_to_layer_data(_image(), session_uid="s1")
    assert len(layers) == 1
    data, kwargs, layer_type = layers[0]
    assert layer_type == "image"
    assert kwargs["scale"] == (0.5, 0.5, 0.5)
    assert kwargs["units"] == ("micrometer",) * 3
    assert kwargs["metadata"]["endpoint_session_uid"] == "s1"
    assert kwargs["metadata"]["result_uid"]
    assert kwargs["metadata"]["axis_labels"] == ["Z", "Y", "X"]
    assert kwargs["colormap"] == "gray"          # never ImProcess's private "grayclip"


def test_labels_become_a_labels_layer():
    result = LabelsResult("mask", np.arange(16, dtype=np.int32).reshape(4, 4))
    data, kwargs, layer_type = result_to_layer_data(result)[0]
    assert layer_type == "labels"
    assert data.dtype == np.int32
    assert "colormap" not in kwargs


def test_a_2d_localization_result_becomes_points_from_the_table_not_the_preview():
    result = LocalizationResult("locs", _locs(), pixel_size_nm=100.0)
    layers = result_to_layer_data(result)
    assert len(layers) == 1
    coords, kwargs, layer_type = layers[0]
    assert layer_type == "points"
    # (y, x) in pixel index units; scale restores nanometres
    np.testing.assert_allclose(coords, [[0.5, 1.0], [0.75, 2.5], [3.0, 4.0]])
    assert kwargs["scale"] == (100.0, 100.0)
    assert kwargs["units"] == ("nanometer", "nanometer")
    assert set(kwargs["properties"]) >= {"frame", "photons"}
    assert "x_nm" not in kwargs["properties"]
    assert kwargs["metadata"]["coordinate_transform"]["axes"] == ["y", "x"]


def test_a_3d_localization_uses_zyx_order_and_the_axial_step():
    result = LocalizationResult("locs", _locs(three_d=True), pixel_size_nm=100.0, z_step_nm=50.0, dims="3D")
    coords, kwargs, _ = result_to_layer_data(result)[0]
    assert coords.shape == (3, 3)
    np.testing.assert_allclose(coords[:, 0], [0.0, 6.0, -3.0])      # z / 50
    np.testing.assert_allclose(coords[:, 1], [0.5, 0.75, 3.0])      # y / 100
    assert kwargs["scale"] == (50.0, 100.0, 100.0)
    assert localization_transform(result)["z_scale_assumed"] is False


def test_a_3d_localization_without_an_axial_step_is_flagged():
    result = LocalizationResult("locs", _locs(three_d=True), pixel_size_nm=100.0, dims="3D")
    coords, kwargs, _ = result_to_layer_data(result)[0]
    assert kwargs["scale"] == (100.0, 100.0, 100.0)
    assert kwargs["metadata"]["z_scale_assumed"] is True
    np.testing.assert_allclose(coords[:, 0], [0.0, 3.0, -1.5])


def test_the_preview_is_translated_to_where_the_points_are():
    """The histogram is binned from the table minimum, not the origin."""
    result = LocalizationResult("locs", _locs(x0=5000.0, y0=8000.0), pixel_size_nm=100.0)
    preview, points = result_to_layer_data(result, include_preview=True)
    _data, kwargs, layer_type = preview
    assert layer_type == "image"
    assert kwargs["metadata"]["role"] == "context"
    y_min, x_min = kwargs["translate"]
    assert y_min == pytest.approx(8050.0)
    assert x_min == pytest.approx(5100.0)
    assert kwargs["units"] == ("nanometer", "nanometer")
    assert points[2] == "points"


@pytest.mark.parametrize("kind_result", ["table", "curve"])
def test_tables_and_curves_are_not_layerable(kind_result):
    if kind_result == "table":
        result = PointsTableResult("pts", np.zeros((2, 2)))
    else:
        result = _image()
        result.kind = "curve"
    assert kind_result not in LAYERABLE_KINDS
    with pytest.raises(NotLayerable):
        result_to_layer_data(result)


def test_pixel_units_add_no_units_kwarg():
    _data, kwargs, _ = result_to_layer_data(_image(unit="px"))[0]
    assert "units" not in kwargs
