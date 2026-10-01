"""P-P: point and multipoint ROIs — geometry, measurements, drawing, interop."""

import numpy as np
import pytest

pytest.importorskip("qtpy")
from qtpy import QtWidgets  # noqa: E402

from imswitch.imcommon.algorithms.roi import ROIRecord  # noqa: E402
from imswitch.imcommon.algorithms.roi_geometry import (  # noqa: E402
    roi_capabilities,
    roi_from_points,
    roi_hit_test,
    roi_mask_local,
    roi_outline,
    roi_points,
)
from imswitch.imcommon.algorithms.roi_imagej import (  # noqa: E402
    available as imagej_available,
    read_imagej,
    write_imagej,
)
from imswitch.improcess.analysis.roi_manager import measure_roi  # noqa: E402
from imswitch.improcess.view.ROIManagerWidget import ROIManagerWidget  # noqa: E402

from .test_roi_manager_widget_p0 import _Viewer  # noqa: E402

needs_roifile = pytest.mark.skipif(
    not imagej_available(), reason="the optional 'roifile' package is not installed"
)


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def panel(qapp):
    widget = ROIManagerWidget(_Viewer(np.arange(256, dtype=float).reshape(16, 16)))
    yield widget
    widget.deleteLater()


# --------------------------------------------------------------------------
# the record
# --------------------------------------------------------------------------

def test_one_point_is_a_point_and_several_are_a_multipoint():
    assert roi_from_points([[3.0, 7.0]], name="p").roi_type == "point"
    assert roi_from_points([[1, 1], [5, 9]], name="m").roi_type == "multipoint"


def test_a_single_point_still_has_a_box():
    """A degenerate empty box would make it invisible to every bounds check."""
    roi = roi_from_points([[3.5, 7.25]], name="p")
    r0, r1, c0, c1 = roi.bounds
    assert r1 > r0 and c1 > c0


def test_points_are_stored_as_vertices_not_a_parallel_field():
    """One place coordinates live, so nothing has to know about a second."""
    roi = roi_from_points([[1.5, 2.5], [3.5, 4.5]], name="m")
    assert roi.vertices == ((1.5, 2.5), (3.5, 4.5))
    assert np.array_equal(roi_points(roi), [[1.5, 2.5], [3.5, 4.5]])


def test_an_empty_point_set_is_refused():
    with pytest.raises(ValueError, match="at least one point"):
        roi_from_points([], name="p")


def test_a_point_has_no_area_to_rasterise():
    """The same refusal a line gets, for the same reason."""
    from imswitch.imcommon.algorithms.roi_geometry import UnsupportedROIGeometry

    roi = roi_from_points([[3.0, 7.0]], name="p")
    assert roi_capabilities(roi.roi_type).is_point
    assert not roi_capabilities(roi.roi_type).is_area
    with pytest.raises(UnsupportedROIGeometry):
        roi_mask_local(roi, (16, 16))


def test_asking_a_non_point_for_its_points_is_empty_not_an_error():
    """So a caller can ask without branching on the type first."""
    assert len(roi_points(ROIRecord("r", "rectangle", (0, 4, 0, 4)))) == 0


# --------------------------------------------------------------------------
# drawing and selecting
# --------------------------------------------------------------------------

def test_a_point_draws_as_a_marker_not_its_one_pixel_box():
    """A 1-pixel outline is invisible at any realistic zoom."""
    roi = roi_from_points([[10.0, 10.0], [20.0, 20.0]], name="m")
    parts = roi_outline(roi)

    assert len(parts) == 2          # one marker per point
    assert parts[0].shape == (4, 2)
    assert np.ptp(parts[0][:, 0]) > 1.0


def test_clicking_near_a_point_selects_it():
    roi = roi_from_points([[10.0, 10.0]], name="p")
    assert roi_hit_test(roi, (10.0, 10.0))
    assert roi_hit_test(roi, (12.0, 10.0))
    assert not roi_hit_test(roi, (30.0, 30.0))


# --------------------------------------------------------------------------
# measurements
# --------------------------------------------------------------------------

def _image():
    return np.arange(256, dtype=float).reshape(16, 16)


def test_a_point_reports_the_intensity_at_it():
    """By nearest neighbour: interpolating would invent a number in no pixel."""
    roi = roi_from_points([[2.0, 3.0]], name="p")
    row = measure_roi(_image(), roi, selection=("mean", "max", "point_count"))

    assert row["mean"] == pytest.approx(_image()[2, 3])
    assert row["point_count"] == 1


def test_a_multipoint_averages_over_its_points():
    image = _image()
    roi = roi_from_points([[2.0, 3.0], [8.0, 9.0], [2.0, 10.0]], name="m")
    row = measure_roi(image, roi, selection=("mean", "point_count"))

    expected = np.mean([image[2, 3], image[8, 9], image[2, 10]])
    assert row["mean"] == pytest.approx(expected)
    assert row["point_count"] == 3


def test_nearest_neighbour_distances():
    roi = roi_from_points([[2.0, 3.0], [8.0, 9.0], [2.0, 10.0]], name="m")
    row = measure_roi(
        _image(), roi, selection=("nn_min_px", "nn_mean_px", "nn_max_px")
    )

    # Pairwise: (2,3)-(8,9) = 8.485, (2,3)-(2,10) = 7, (8,9)-(2,10) = 6.083.
    # Each point's nearest: 7.0, 6.083, 6.083.
    assert row["nn_min_px"] == pytest.approx(6.0827, abs=1e-3)
    assert row["nn_max_px"] == pytest.approx(7.0)
    assert row["nn_mean_px"] == pytest.approx((7.0 + 6.0827 + 6.0827) / 3, abs=1e-3)


def test_a_single_point_has_no_nearest_neighbour():
    """NaN, not zero: zero would read as "measured, and coincident"."""
    roi = roi_from_points([[2.0, 3.0]], name="p")
    row = measure_roi(_image(), roi, selection=("nn_min_px", "nn_mean_px"))
    assert np.isnan(row["nn_min_px"])
    assert np.isnan(row["nn_mean_px"])


def test_nearest_neighbour_distances_are_calibrated_from_scaled_points():
    roi = roi_from_points([[0.0, 0.0], [0.0, 10.0]], name="m")
    row = measure_roi(
        _image(), roi, selection=("nn_min_px", "nn_min_cal"),
        row_scale=1.0, col_scale=0.25, unit="um",
    )
    assert row["nn_min_px"] == pytest.approx(10.0)
    assert row["nn_min_cal"] == pytest.approx(2.5)


def test_a_point_has_no_area_perimeter_or_shape():
    """Zero for any of them would read as measured."""
    roi = roi_from_points([[2.0, 3.0]], name="p")
    row = measure_roi(
        _image(), roi,
        selection=("area_px", "perimeter_px", "circularity", "major_px"),
    )
    assert all(np.isnan(value) for value in row.values() if isinstance(value, float))


def test_line_measurements_are_nan_for_a_point_and_the_reverse():
    """`samples` is set for both, so the two must be told apart by more."""
    point = roi_from_points([[2.0, 3.0]], name="p")
    row = measure_roi(_image(), point, selection=("length_px", "point_count"))
    assert np.isnan(row["length_px"])
    assert row["point_count"] == 1

    line = ROIRecord(
        "l", "line", (2, 3, 2, 8), vertices=((2.0, 2.0), (2.0, 7.0))
    )
    row = measure_roi(_image(), line, selection=("length_px", "point_count"))
    assert row["length_px"] == pytest.approx(5.0)
    assert np.isnan(row["point_count"])


def test_a_point_outside_the_image_is_refused_not_clamped():
    roi = roi_from_points([[900.0, 900.0]], name="p")
    with pytest.raises(ValueError, match="no point inside"):
        measure_roi(_image(), roi, selection=("mean",))


def test_the_group_is_reported_so_fiducial_sets_are_distinguishable():
    roi = roi_from_points([[2.0, 3.0]], name="p", group=7)
    row = measure_roi(_image(), roi, selection=("point_group",))
    assert row["point_group"] == 7


# --------------------------------------------------------------------------
# the panel
# --------------------------------------------------------------------------

def _start_drawing(panel, mode):
    """Pick a shape in the chooser and take the tool, as the user would."""
    index = [m for _label, m in panel.DRAW_MODES].index(mode)
    panel.shapeCombo.setCurrentIndex(index)
    panel._startDrawing()


def test_the_point_mode_switches_the_viewer_tool(panel):
    _start_drawing(panel, "point")
    assert panel._toolService.get_mode() == "point"


def test_placed_points_are_captured_as_one_multipoint(panel):
    """A fiducial set is a thing; forty separate ROIs would not be one."""
    _start_drawing(panel, "point")
    manager = panel._toolService._manager
    manager._ensure_points_layer()
    manager._points_layer.data = [(2.0, 3.0), (8.0, 9.0), (11.0, 4.0)]

    panel.add_current_rectangle()   # the shared "Add Shape" capture

    assert len(panel._model.rois) == 1
    roi = panel._model.rois[0]
    assert roi.roi_type == "multipoint"
    assert len(roi_points(roi)) == 3


def test_capturing_with_nothing_drawn_says_what_to_do(panel):
    panel.add_current_rectangle()
    assert "point" in panel.summaryLabel.text()


def test_a_captured_point_set_measures_in_the_table(panel):
    _start_drawing(panel, "point")
    manager = panel._toolService._manager
    manager._ensure_points_layer()
    manager._points_layer.data = [(2.0, 3.0), (8.0, 9.0)]
    panel.add_current_rectangle()

    from imswitch.imcommon.algorithms.roi_set import MeasurementConfig

    panel.set_measurement_config(MeasurementConfig(selected=("point_count", "mean")))
    row = panel._stats_rows[0]
    assert row["point_count"] == 2


# --------------------------------------------------------------------------
# interop
# --------------------------------------------------------------------------

@needs_roifile
def test_a_multipoint_round_trips_through_imagej(tmp_path):
    original = roi_from_points([[1.0, 2.0], [5.0, 9.0], [3.0, 4.0]], name="fids")
    target = tmp_path / "fids.roi"
    write_imagej(target, [original])

    (restored,), _report = read_imagej(target)
    assert restored.roi_type == "multipoint"
    assert np.allclose(roi_points(restored), roi_points(original))


@needs_roifile
def test_a_single_point_comes_back_as_a_point_not_a_multipoint(tmp_path):
    target = tmp_path / "one.roi"
    write_imagej(target, [roi_from_points([[3.0, 7.0]], name="p")])

    (restored,), _report = read_imagej(target)
    assert restored.roi_type == "point"


@needs_roifile
def test_a_fiducial_group_survives_the_round_trip(tmp_path):
    target = tmp_path / "g.roi"
    write_imagej(target, [roi_from_points([[1.0, 2.0], [5.0, 9.0]], name="f", group=5)])

    (restored,), _report = read_imagej(target)
    assert restored.group == 5


def test_a_point_set_round_trips_through_the_native_format():
    from imswitch.imcommon.algorithms.roi_set import ROISet
    from imswitch.imcommon.algorithms.roi_set_io import dumps, loads

    original = roi_from_points([[1.5, 2.5], [3.5, 4.5]], name="m", group=2)
    restored = loads(dumps(ROISet(rois=(original,)))).rois[0]

    assert restored.roi_type == "multipoint"
    assert restored.group == 2
    assert np.array_equal(roi_points(restored), roi_points(original))


# --------------------------------------------------------------------------
# final review — the point layer must never be mistaken for an image
# --------------------------------------------------------------------------

def test_the_point_tool_layer_is_not_an_image_source():
    """A napari Points layer's data *is* an (n, 2) float ndarray, so the type
    check accepts it — and placing points makes it the active layer, so
    without the name exclusion the act of drawing would redirect measurement
    onto an array of coordinates."""
    from types import SimpleNamespace

    from imswitch.imcommon.view.guitools.naparitools import ViewerToolManager
    from imswitch.improcess.layer_selection import is_image_layer

    layer = SimpleNamespace(
        name=ViewerToolManager.POINTS_LAYER_NAME,
        data=np.zeros((5, 2)),
        visible=True,
    )
    assert not is_image_layer(layer)


def test_every_tool_layer_the_manager_creates_is_excluded_by_name():
    """The two lists are in different packages; a new tool layer added to one
    and not the other is the same bug, found later."""
    from imswitch.imcommon.view.guitools.naparitools import ViewerToolManager
    from imswitch.improcess.layer_selection import ANNOTATION_LAYER_NAMES

    for attribute in ("LAYER_NAME", "POINTS_LAYER_NAME"):
        name = getattr(ViewerToolManager, attribute)
        assert name in ANNOTATION_LAYER_NAMES, attribute


def test_drawing_points_does_not_redirect_the_measured_image(panel):
    before = panel._active_image_layer()
    _start_drawing(panel, "point")
    assert panel._active_image_layer() is before
