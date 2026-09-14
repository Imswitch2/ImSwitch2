"""P-3: the measurement set.

Fixture values are the contract for the definitions: shape descriptors are
checked against known analytic answers, and the calibrated columns against
hand-computed products.
"""

import numpy as np
import pytest

from imswitch.imcommon.algorithms.roi import ROIRecord, new_uid
from imswitch.imcommon.algorithms.roi_geometry import roi_from_mask, roi_from_vertices
from imswitch.improcess.analysis.roi_measurements import (
    MEASUREMENTS,
    MeasurementContext,
    column_label,
    default_selection,
    measure,
)


def _context(mask, image=None, **kwargs):
    mask = np.asarray(mask, dtype=bool)
    if image is None:
        image = np.ones(mask.shape, dtype=float)
    roi = kwargs.pop("roi", None)
    if roi is None:
        roi = ROIRecord("r", "rectangle", (0, mask.shape[0], 0, mask.shape[1]),
                        uid=new_uid())
    return MeasurementContext(
        local_mask=mask, local_image=np.asarray(image, dtype=float), roi=roi, **kwargs
    )


def _value(measurement_id, context):
    return MEASUREMENTS[measurement_id].compute(context)


def _disc(radius=50):
    size = radius * 2 + 3
    centre = size // 2
    rows, cols = np.ogrid[:size, :size]
    return (rows - centre) ** 2 + (cols - centre) ** 2 <= radius ** 2


# --------------------------------------------------------------------------
# basic statistics, and the ddof decision (P-3.8 / Q-12)
# --------------------------------------------------------------------------

def test_standard_deviation_is_the_sample_one_matching_imagej():
    values = np.array([[1.0, 2.0], [3.0, 4.0]])

    result = _value("std", _context(np.ones((2, 2), bool), values))

    assert result == pytest.approx(float(np.std([1, 2, 3, 4], ddof=1)))
    assert result != pytest.approx(float(np.std([1, 2, 3, 4], ddof=0)))


def test_standard_deviation_of_one_pixel_is_undefined_not_zero():
    """n-1 has no meaning for a single sample; 0 would claim a spread was
    measured and found to be none."""
    mask = np.zeros((4, 4), dtype=bool)
    mask[1, 1] = True

    assert np.isnan(_value("std", _context(mask, np.full((4, 4), 7.0))))


def test_basic_statistics_are_computed_over_the_mask_only():
    image = np.arange(16, dtype=float).reshape(4, 4)
    mask = np.zeros((4, 4), dtype=bool)
    mask[0, :2] = True  # values 0 and 1

    context = _context(mask, image)

    assert _value("mean", context) == pytest.approx(0.5)
    assert _value("sum", context) == pytest.approx(1.0)
    assert _value("area_px", context) == 2


def test_non_finite_pixels_are_excluded_from_intensity_but_counted_in_area():
    image = np.array([[1.0, np.nan], [3.0, 4.0]])

    context = _context(np.ones((2, 2), bool), image)

    assert _value("area_px", context) == 4
    assert _value("finite_px", context) == 3
    assert _value("mean", context) == pytest.approx((1 + 3 + 4) / 3)


# --------------------------------------------------------------------------
# shape descriptors (P-3.6 / Q-09a)
# --------------------------------------------------------------------------

def test_circularity_of_a_disc_and_a_square():
    """The published values: a disc is ~1, a square ~pi/4.

    The square is a *rectangle* ROI, whose perimeter is known exactly. A
    rasterised square measured with the Crofton estimator comes out at ~0.88
    instead — which is why an analytic perimeter is used wherever the shape is
    known rather than estimating one from pixels.
    """
    disc = _value("circularity", _context(_disc(50), roi=_mask_roi(_disc(50))))
    square_roi = ROIRecord("sq", "rectangle", (0, 100, 0, 100), uid=new_uid())
    square = _value("circularity",
                    _context(np.ones((100, 100), bool), roi=square_roi))

    assert disc == pytest.approx(1.0, abs=0.02)
    assert square == pytest.approx(np.pi / 4, abs=0.02)


def test_a_rectangles_perimeter_is_exact_not_estimated():
    roi = ROIRecord("r", "rectangle", (0, 10, 0, 30), uid=new_uid())

    perimeter = _value("perimeter_px", _context(np.ones((10, 30), bool), roi=roi))

    assert perimeter == pytest.approx(80.0)


def test_an_ellipses_perimeter_uses_a_closed_form():
    roi = ROIRecord("e", "ellipse", (0, 40, 0, 40), uid=new_uid())

    perimeter = _value("perimeter_px", _context(np.ones((40, 40), bool), roi=roi))

    # A circle of radius 20.
    assert perimeter == pytest.approx(2 * np.pi * 20, rel=0.01)


def _mask_roi(mask):
    roi = roi_from_mask(mask, name="m")
    return ROIRecord(roi.name, roi.roi_type, roi.bounds, mask=roi.mask, uid=new_uid())


def test_aspect_ratio_of_an_elongated_rectangle():
    mask = np.zeros((20, 60), dtype=bool)
    mask[5:15, 5:55] = True  # 10 x 50

    ratio = _value("aspect_ratio", _context(mask, roi=_mask_roi(mask)))

    assert ratio == pytest.approx(5.0, rel=0.05)


def test_solidity_of_a_convex_shape_is_one():
    mask = np.zeros((30, 30), dtype=bool)
    mask[5:25, 5:25] = True

    assert _value("solidity", _context(mask, roi=_mask_roi(mask))) == pytest.approx(1.0)


def test_solidity_of_a_concave_shape_is_less_than_one():
    mask = np.zeros((30, 30), dtype=bool)
    mask[5:25, 5:25] = True
    mask[5:15, 15:25] = False  # bite out a quarter

    assert _value("solidity", _context(mask, roi=_mask_roi(mask))) < 0.9


def test_feret_of_a_rotated_rectangle_follows_its_diagonal():
    diamond = roi_from_vertices(
        [[0, 20], [20, 40], [40, 20], [20, 0]], roi_type="polygon", name="d"
    )
    diamond = ROIRecord(diamond.name, diamond.roi_type, diamond.bounds,
                        vertices=diamond.vertices, uid=new_uid())
    from imswitch.imcommon.algorithms.roi_geometry import roi_mask_local

    mask, _ = roi_mask_local(diamond, (64, 64))

    feret = _value("feret_px", _context(mask, roi=diamond))

    assert feret == pytest.approx(40.0, rel=0.1)


def test_perimeter_of_a_polygon_is_its_exact_edge_length():
    triangle = roi_from_vertices(
        [[0, 0], [0, 30], [40, 0]], roi_type="polygon", name="t"
    )
    triangle = ROIRecord(triangle.name, triangle.roi_type, triangle.bounds,
                         vertices=triangle.vertices, uid=new_uid())
    from imswitch.imcommon.algorithms.roi_geometry import roi_mask_local

    mask, _ = roi_mask_local(triangle, (64, 64))

    # 30 + 40 + 50 for a 3-4-5 triangle.
    assert _value("perimeter_px", _context(mask, roi=triangle)) == pytest.approx(120.0)


# --------------------------------------------------------------------------
# calibration (P-3.3 / A-16)
# --------------------------------------------------------------------------

def test_calibrated_area_is_the_pixel_area_times_the_pixel_size():
    context = _context(np.ones((10, 10), bool), row_scale=0.1, col_scale=0.1, unit="um")

    assert _value("area_px", context) == 100
    assert _value("area_cal", context) == pytest.approx(100 * 0.01)


def test_column_ids_never_carry_a_unit_suffix():
    """A nm result and a um result must land in the same columns."""
    forbidden = ("_um", "_nm", "_mm", "_um2", "_nm2", "_micron")
    for entry in MEASUREMENTS.values():
        assert not entry.id.endswith(forbidden), entry.id


def test_the_unit_appears_only_in_the_header():
    assert column_label("area_cal", "um") == "Area (um²)"
    assert column_label("perimeter_cal", "um") == "Perim. (um)"
    # Uncalibrated data gets no invented unit.
    assert column_label("area_cal", "px") == "Area"
    assert column_label("area_px", "um") == "Area (px)"


def test_every_spatial_measurement_has_both_a_pixel_and_a_calibrated_form():
    """Half a pair is worse than neither: a reader cannot tell which they have."""
    pixel = {i[: -len("_px")] for i in MEASUREMENTS if i.endswith("_px")}
    calibrated = {i[: -len("_cal")] for i in MEASUREMENTS if i.endswith("_cal")}
    # `finite` is a pixel count, not a length. Everything spatial is paired,
    # including the Feret hull positions.
    unpaired = (pixel ^ calibrated) - {"finite"}

    assert not unpaired, sorted(unpaired)


def test_rows_always_say_what_unit_and_how_comparable_they_are():
    row = measure(_context(np.ones((4, 4), bool), unit="um",
                           geometry_match="pixel-compatible"))

    assert row["spatial_unit"] == "um"
    assert row["geometry_match"] == "pixel-compatible"


# --------------------------------------------------------------------------
# anisotropy (Q-09a)
# --------------------------------------------------------------------------

def test_anisotropic_pixels_are_flagged_on_the_row():
    row = measure(
        _context(np.ones((8, 8), bool), row_scale=0.2, col_scale=0.1),
        selection=("area_px", "pixel_aspect"),
    )

    assert row["pixel_aspect"] == pytest.approx(2.0)
    assert "shape_note" in row, "an anisotropic row must say so"


def test_square_pixels_are_not_flagged():
    row = measure(_context(np.ones((8, 8), bool), row_scale=0.1, col_scale=0.1))

    assert "shape_note" not in row


def test_calibrated_perimeter_respects_anisotropy():
    """The scaled outline, not a scaled scalar: a 10x50 box is not the same
    perimeter under 0.1/0.2 um pixels as under square ones."""
    square = roi_from_vertices(
        [[0, 0], [0, 10], [10, 10], [10, 0]], roi_type="polygon", name="s"
    )
    square = ROIRecord(square.name, square.roi_type, square.bounds,
                       vertices=square.vertices, uid=new_uid())
    from imswitch.imcommon.algorithms.roi_geometry import roi_mask_local

    mask, _ = roi_mask_local(square, (32, 32))
    isotropic = _context(mask, roi=square, row_scale=0.1, col_scale=0.1)
    anisotropic = _context(mask, roi=square, row_scale=0.1, col_scale=0.2)

    assert _value("perimeter_cal", isotropic) == pytest.approx(40 * 0.1)
    assert _value("perimeter_cal", anisotropic) == pytest.approx(2 * (10 * 0.1 + 10 * 0.2))


# --------------------------------------------------------------------------
# threshold (P-3.4)
# --------------------------------------------------------------------------

def test_limit_to_threshold_restricts_the_statistics():
    image = np.arange(16, dtype=float).reshape(4, 4)
    full = _context(np.ones((4, 4), bool), image)
    limited = _context(np.ones((4, 4), bool), image, threshold=(4.0, 7.0))

    assert _value("mean", full) == pytest.approx(7.5)
    assert _value("mean", limited) == pytest.approx(5.5)
    assert _value("finite_px", limited) == 4


def test_area_fraction_reports_the_thresholded_proportion():
    image = np.arange(16, dtype=float).reshape(4, 4)
    context = _context(np.ones((4, 4), bool), image, threshold=(8.0, 15.0))

    assert _value("area_fraction", context) == pytest.approx(50.0)


def test_area_fraction_is_a_hundred_percent_without_a_threshold():
    context = _context(np.ones((4, 4), bool), np.ones((4, 4)))

    assert _value("area_fraction", context) == pytest.approx(100.0)


# --------------------------------------------------------------------------
# line measurements (P-3.7)
# --------------------------------------------------------------------------

def test_a_line_reports_its_length_and_angle():
    line = roi_from_vertices([[0, 0], [30, 40]], roi_type="line", name="l")
    line = ROIRecord(line.name, line.roi_type, line.bounds,
                     vertices=line.vertices, uid=new_uid())
    context = _context(np.ones((4, 4), bool), roi=line)

    assert _value("length_px", context) == pytest.approx(50.0)
    assert _value("line_angle", context) == pytest.approx(
        np.degrees(np.arctan2(-30, 40)) % 180.0
    )


def test_a_calibrated_line_length_uses_both_axis_scales():
    line = roi_from_vertices([[0, 0], [10, 0]], roi_type="line", name="l")
    line = ROIRecord(line.name, line.roi_type, line.bounds,
                     vertices=line.vertices, uid=new_uid())

    context = _context(np.ones((4, 4), bool), roi=line, row_scale=0.5, col_scale=0.1)

    assert _value("length_cal", context) == pytest.approx(5.0)


def test_line_measurements_are_nan_for_an_area_roi():
    context = _context(np.ones((4, 4), bool))

    assert np.isnan(_value("length_px", context))


# --------------------------------------------------------------------------
# the registry is the single source
# --------------------------------------------------------------------------

def test_a_failing_measurement_takes_its_own_column_not_the_row():
    """One unfittable shape must not cost the mean beside it."""
    broken = ROIRecord("bad", "rectangle", (0, 0, 0, 0), uid=new_uid())
    row = measure(
        _context(np.zeros((0, 0), dtype=bool), np.zeros((0, 0)), roi=broken),
        selection=("area_px", "mean", "solidity"),
    )

    assert row["area_px"] == 0
    assert set(row) >= {"area_px", "mean", "solidity"}


def test_the_default_selection_covers_the_columns_the_panel_showed():
    defaults = set(default_selection())

    assert {"area_px", "mean", "median", "std", "min", "max", "sum"} <= defaults


def test_roi_stats_shim_uses_the_registry():
    """The legacy panel and the ROI manager cannot disagree about a mean."""
    from imswitch.improcess.analysis.roi_stats import compute_roi_stats

    image = np.arange(16, dtype=float).reshape(4, 4)
    legacy = compute_roi_stats(image)
    context = _context(np.ones((4, 4), bool), image)

    assert legacy.mean == pytest.approx(_value("mean", context))
    assert legacy.std == pytest.approx(_value("std", context))
    assert legacy.total == pytest.approx(_value("sum", context))


# --------------------------------------------------------------------------
# P-3.7 — line ROIs are measurable through the same entry point
# --------------------------------------------------------------------------

def _line_image():
    image = np.zeros((16, 16), dtype=float)
    image[8, 2:12] = np.arange(10, dtype=float)
    return image


def _line_roi(**kwargs):
    return ROIRecord(
        "l", "line", (8, 9, 2, 12), vertices=((8.0, 2.0), (8.0, 11.0)), **kwargs
    )


def test_a_line_roi_can_be_measured_at_all():
    """It could not: measure_roi always asked the area rasteriser."""
    from imswitch.improcess.analysis.roi_manager import measure_roi

    row = measure_roi(
        _line_image(), _line_roi(),
        selection=("length_px", "line_mean", "line_min", "line_max", "line_std"),
    )
    assert row["length_px"] == pytest.approx(9.0)
    assert row["line_mean"] == pytest.approx(4.5)
    assert row["line_min"] == pytest.approx(0.0)
    assert row["line_max"] == pytest.approx(9.0)
    assert row["line_std"] == pytest.approx(np.std(np.arange(10), ddof=1))


def test_the_standard_statistics_of_a_line_come_from_the_line():
    from imswitch.improcess.analysis.roi_manager import measure_roi

    row = measure_roi(_line_image(), _line_roi(), selection=("mean", "max", "std"))
    assert row["mean"] == pytest.approx(4.5)
    assert row["max"] == pytest.approx(9.0)


def test_a_line_has_no_area():
    """Zero would read as "measured, and empty"; NaN says "not applicable"."""
    from imswitch.improcess.analysis.roi_manager import measure_roi

    row = measure_roi(
        _line_image(), _line_roi(), selection=("area_px", "area_cal", "circularity")
    )
    assert np.isnan(row["area_px"])
    assert np.isnan(row["area_cal"])
    assert np.isnan(row["circularity"])


def test_line_width_averages_perpendicular_to_the_line():
    from imswitch.improcess.analysis.roi_manager import measure_roi

    image = np.zeros((16, 16), dtype=float)
    image[8, 2:12] = 10.0   # the line itself
    image[7, 2:12] = 0.0    # its neighbours
    image[9, 2:12] = 0.0

    narrow = measure_roi(image, _line_roi(), selection=("line_mean",), line_width=1)
    wide = measure_roi(image, _line_roi(), selection=("line_mean",), line_width=3)
    # A uniform mean over three rows, as ImageJ does it — not a weighted one.
    assert narrow["line_mean"] == pytest.approx(10.0)
    assert wide["line_mean"] == pytest.approx(10.0 / 3.0)


def test_line_statistics_are_nan_for_an_area_roi():
    from imswitch.improcess.analysis.roi_manager import measure_roi

    roi = ROIRecord("box", "rectangle", (0, 4, 0, 4))
    row = measure_roi(np.ones((8, 8)), roi, selection=("line_mean", "length_px"))
    assert np.isnan(row["line_mean"])
    assert np.isnan(row["length_px"])


def test_the_profile_panel_and_the_measurement_share_one_sampler():
    """Two samplers would mean a plotted profile and a line mean that disagree."""
    import ast
    import importlib
    import inspect

    # By module path: `imswitch.improcess.view` re-exports the *class* under
    # the same name, so a plain attribute lookup gets the class instead.
    module = importlib.import_module("imswitch.improcess.view.ProfileWidget")

    source = inspect.getsource(module)
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "line_samples" in imported
    assert "map_coordinates" not in imported


def test_the_shared_sampler_reads_off_image_pixels_as_zero():
    from imswitch.imcommon.algorithms.line_sampling import line_samples

    image = np.ones((4, 4))
    values = line_samples(image, 2.0, 2.0, 2.0, 8.0)
    assert values is not None
    assert values[0] == pytest.approx(1.0)
    assert values[-1] == pytest.approx(0.0)


def test_a_degenerate_line_is_refused_rather_than_divided_by_zero():
    from imswitch.imcommon.algorithms.line_sampling import line_samples

    assert line_samples(np.ones((4, 4)), 1.0, 1.0, 1.0, 1.0) is None


def test_a_polyline_does_not_count_its_corner_twice():
    from imswitch.imcommon.algorithms.line_sampling import polyline_samples

    image = np.zeros((8, 8))
    image[4, 4] = 1.0
    values = polyline_samples(image, [(4.0, 0.0), (4.0, 4.0), (0.0, 4.0)])
    assert values is not None
    assert np.count_nonzero(values == 1.0) == 1


# --------------------------------------------------------------------------
# P-3 calibration: geometry is scaled before it is measured, not after
# --------------------------------------------------------------------------

def _bar_context(row_scale, col_scale):
    """A 10-row by 20-column bar, so anisotropy has a direction."""
    return MeasurementContext(
        local_mask=np.ones((10, 20), dtype=bool),
        local_image=np.ones((10, 20)),
        roi=ROIRecord("bar", "mask", (0, 10, 0, 20)),
        row_scale=row_scale,
        col_scale=col_scale,
        unit="um",
    )


def test_the_fitted_axes_match_scikit_image_on_the_pixel_grid():
    """The pixel form must stay exactly what ImageJ users compare against."""
    from skimage.measure import regionprops

    from imswitch.improcess.analysis.roi_measurements import _axis_lengths

    mask = np.ones((10, 20), dtype=bool)
    props = regionprops(mask.astype(np.uint8))[0]
    major, minor = _axis_lengths(_bar_context(1.0, 1.0))

    assert major == pytest.approx(props.axis_major_length)
    assert minor == pytest.approx(props.axis_minor_length)


def test_anisotropic_axes_are_recomputed_not_rescaled():
    """A mean pixel size cannot convert a pixel axis into a world one."""
    context = _bar_context(1.0, 0.1)
    major, minor = _value("major_cal", context), _value("minor_cal", context)
    pixel_major = _value("major_px", context)

    # In world units the bar is 10 tall and 2 wide, so the long axis now runs
    # along the rows: the axes have swapped, which no scalar factor can do.
    assert major < pixel_major
    assert major == pytest.approx(_value("minor_px", context))
    assert minor == pytest.approx(pixel_major * 0.1)


def test_anisotropic_feret_is_measured_on_the_scaled_hull():
    context = _bar_context(1.0, 0.1)
    # The diagonal of the 9 x 1.9 world-unit hull.
    assert _value("feret_cal", context) == pytest.approx(np.hypot(9.0, 1.9), rel=1e-3)
    assert _value("feret_cal", context) != pytest.approx(
        _value("feret_px", context) * 0.55
    )


def test_the_feret_positions_have_a_calibrated_form_too():
    context = MeasurementContext(
        local_mask=np.ones((4, 4), dtype=bool),
        local_image=np.ones((4, 4)),
        roi=ROIRecord("m", "mask", (10, 14, 20, 24)),
        row_scale=0.5,
        col_scale=0.25,
        unit="um",
    )
    assert _value("feret_y_cal", context) == pytest.approx(
        _value("feret_y_px", context) * 0.5
    )
    assert _value("feret_x_cal", context) == pytest.approx(
        _value("feret_x_px", context) * 0.25
    )


def test_an_anisotropic_mask_perimeter_is_not_scaled_by_an_average():
    """There is no scalar that converts a pixel perimeter into a world one."""
    context = _bar_context(1.0, 0.1)
    pixel = _value("perimeter_px", context)
    calibrated = _value("perimeter_cal", context)
    assert calibrated != pytest.approx(pixel * 0.55)
    # 2*(10*1.0 + 20*0.1) = 24 for the traced rectangle boundary.
    assert calibrated == pytest.approx(24.0, rel=0.1)


def test_calibrated_measurements_drop_out_on_an_uncalibrated_frame():
    """`area_cal` beside an identical `area_px` claims a calibration there is not."""
    from imswitch.improcess.analysis.roi_measurements import applicable_selection

    asked = ("area_px", "area_cal", "mean", "perimeter_cal")
    assert applicable_selection(asked, unit="px") == ("area_px", "mean")
    assert applicable_selection(asked, unit="um") == asked


def test_the_default_selection_is_pixel_only_without_a_calibration():
    from imswitch.improcess.analysis.roi_measurements import applicable_selection

    ids = applicable_selection(None, unit="px")
    assert "area_px" in ids
    assert not [key for key in ids if key.endswith("_cal")]
