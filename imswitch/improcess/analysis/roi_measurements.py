"""What a measurement is, and every measurement there is.

One registry, read by the *Set Measurements* dialog, the table, the CSV export
and the rows pushed to the Results dock — so a measurement is added in one
place and cannot appear in three of the four.

Two things here are easy to get subtly wrong and are therefore pinned rather
than left to each measurement:

**Units.** A measurement id never contains a unit. ``area_px`` and ``area_cal``
are separate ids and every row carries ``spatial_unit``; the header renders
"Area (µm²)". Encoding the unit in the id (``area_um2``) would fragment the
accumulating Results table the moment a nm result and a µm result are both
measured, because the two would land in different columns.

**Anisotropy.** Scan-derived data routinely has different row and column pixel
sizes, which ImageJ's shape descriptors do not contemplate. Descriptors are
computed on the pixel grid — unitless, isotropic, matching ImageJ for square
pixels — and ``pixel_aspect`` is reported alongside so a reader can see when
that assumption does not hold. Calibrated lengths come from the scaled
outline, which is correct under anisotropy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

import numpy as np

UnitKind = Literal["none", "length", "area", "intensity", "index"]
Domain = Literal["pixel", "calibrated", "both", "meta"]

#: Relative difference in row/column pixel size above which shape descriptors
#: are flagged: they assume square pixels, and beyond this they are indicative
#: rather than comparable with ImageJ's.
ANISOTROPY_TOLERANCE = 0.01


@dataclass(frozen=True)
class MeasurementContext:
    """Everything one measurement is allowed to look at."""

    #: The ROI's mask over its clipped bounding box.
    local_mask: np.ndarray
    #: The image window under that mask.
    local_image: np.ndarray
    roi: Any
    row_scale: float = 1.0
    col_scale: float = 1.0
    unit: str = "px"
    plane: tuple[tuple[str, int], ...] = ()
    #: Inclusive intensity window; None counts every finite pixel.
    threshold: tuple[float, float] | None = None
    geometry_match: str = "exact"
    #: Intensities sampled along a line ROI. Present instead of a mask, not
    #: beside one: a line has no interior, so there is nothing for the area
    #: rasteriser to produce and the intensity statistics read from here.
    samples: np.ndarray | None = None
    #: Perpendicular samples averaged for a line, ImageJ's line width.
    line_width: int = 1

    @property
    def is_line(self) -> bool:
        return self.samples is not None

    @property
    def values(self) -> np.ndarray:
        """Finite intensities inside the ROI, after any threshold.

        For a line, "inside" means along it: the same statistics — mean, min,
        max, standard deviation — are reported for a line as ImageJ reports
        them, from the sampled profile rather than from an interior it has not
        got.
        """
        inside = (
            np.asarray(self.samples, dtype=np.float64)
            if self.is_line
            else self.local_image[self.local_mask]
        )
        finite = inside[np.isfinite(inside)]
        if self.threshold is None:
            return finite
        low, high = self.threshold
        return finite[(finite >= low) & (finite <= high)]

    @property
    def area_pixels(self) -> int:
        return int(self.local_mask.sum())

    @property
    def pixel_aspect(self) -> float:
        if not self.col_scale:
            return float("nan")
        return float(self.row_scale) / float(self.col_scale)

    @property
    def anisotropic(self) -> bool:
        aspect = self.pixel_aspect
        return bool(np.isfinite(aspect) and abs(aspect - 1.0) > ANISOTROPY_TOLERANCE)

    @property
    def pixel_area(self) -> float:
        return float(self.row_scale) * float(self.col_scale)


@dataclass(frozen=True)
class Measurement:
    """One column."""

    id: str
    label: str
    group: str
    compute: Callable[[MeasurementContext], Any]
    domain: Domain = "pixel"
    unit_kind: UnitKind = "none"
    default_on: bool = False
    #: Whether this measurement means anything for a line ROI. False by
    #: default and by design: a line has no interior, so an area or shape
    #: descriptor computed from its (empty) mask returns a number that looks
    #: measured and is not — circularity 0.0 for a straight line, say.
    line_safe: bool = False
    #: Short note shown in the dialog, for anything with a definition worth
    #: stating (perimeter, the standard-deviation convention, and so on).
    note: str = ""


MEASUREMENTS: dict[str, Measurement] = {}


def measurement(**kwargs) -> Callable:
    """Register a measurement; the decorated function is its ``compute``."""

    def _register(func):
        entry = Measurement(compute=func, **kwargs)
        MEASUREMENTS[entry.id] = entry
        return func

    return _register


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _nan() -> float:
    return float("nan")


def _regionprops(context: MeasurementContext):
    """scikit-image region properties for the ROI's mask.

    Computed on the pixel grid: shape descriptors assume square pixels, which
    is what makes them comparable with ImageJ's.
    """
    from skimage.measure import regionprops

    mask = np.asarray(context.local_mask, dtype=np.uint8)
    if not mask.any():
        return None
    props = regionprops(mask)
    return props[0] if props else None


def _outline_lengths(context: MeasurementContext) -> tuple[float, float]:
    """``(pixel_perimeter, calibrated_perimeter)`` for the ROI.

    Vector ROIs use the exact polygon length; masks use the Crofton estimator
    (``perimeter_crofton``, *not* ``perimeter``), which is the closer analogue
    of ImageJ's traced boundary. The calibrated value scales the outline before
    measuring it, so it stays correct when pixels are not square.
    """
    from imswitch.imcommon.algorithms.roi_geometry import (
        roi_bounds,
        roi_capabilities,
        roi_outline,
    )

    roi = context.roi
    kind = str(getattr(roi, "roi_type", "") or "").lower()
    caps = roi_capabilities(kind)

    # Analytic first, wherever the shape is known exactly. Estimating a
    # rectangle's perimeter from its rasterisation is measurably wrong — the
    # Crofton estimator returns ~378 for a 100x100 square, so its circularity
    # comes out 0.88 instead of pi/4 — and there is no reason to estimate a
    # quantity that is simply 2*(w+h).
    if getattr(roi, "vertices", None) is None and kind in ("rectangle", "ellipse", "oval"):
        r0, r1, c0, c1 = roi_bounds(roi)
        height, width = float(r1 - r0), float(c1 - c0)
        if kind == "rectangle":
            return (
                2.0 * (height + width),
                2.0 * (height * context.row_scale + width * context.col_scale),
            )
        # Ramanujan's approximation, good to ~1e-5 for any realistic ellipse.
        def _ellipse_perimeter(a: float, b: float) -> float:
            if a <= 0 or b <= 0:
                return 0.0
            h = ((a - b) ** 2) / ((a + b) ** 2)
            return float(np.pi * (a + b) * (1.0 + 3.0 * h / (10.0 + np.sqrt(4.0 - 3.0 * h))))

        return (
            _ellipse_perimeter(height / 2.0, width / 2.0),
            _ellipse_perimeter(
                height * context.row_scale / 2.0, width * context.col_scale / 2.0
            ),
        )

    if getattr(roi, "vertices", None) is not None:
        parts = [np.asarray(roi.vertices, dtype=float)]
        closed = caps.is_area
    else:
        try:
            parts = roi_outline(roi)
        except Exception:
            return _nan(), _nan()
        closed = True

    if not parts:
        return _nan(), _nan()

    def _length(points: np.ndarray, scale_rows: float, scale_cols: float) -> float:
        scaled = points * np.array([scale_rows, scale_cols], dtype=float)
        if closed:
            scaled = np.vstack([scaled, scaled[:1]])
        return float(np.sum(np.linalg.norm(np.diff(scaled, axis=0), axis=1)))

    if getattr(roi, "vertices", None) is None:
        # A rasterised region: the Crofton estimator on the mask, which is the
        # closer analogue of ImageJ's traced boundary.
        from skimage.measure import perimeter_crofton

        pixel = float(perimeter_crofton(np.asarray(context.local_mask, dtype=np.uint8)))
        if not context.anisotropic:
            return pixel, pixel * float(context.row_scale)
        # Under anisotropy there is no scalar that converts a pixel perimeter
        # into a world one — the answer depends on how much of the boundary
        # runs along each axis — so it is measured again on the scaled
        # boundary rather than multiplied by an average.
        try:
            parts = roi_outline(roi)
        except Exception:
            return pixel, _nan()
        calibrated = sum(
            _length(part, context.row_scale, context.col_scale) for part in parts
        )
        return pixel, calibrated

    pixel = sum(_length(part, 1.0, 1.0) for part in parts)
    calibrated = sum(
        _length(part, context.row_scale, context.col_scale) for part in parts
    )
    return pixel, calibrated


def _centroid(context: MeasurementContext) -> tuple[float, float]:
    rows, cols = np.nonzero(context.local_mask)
    if rows.size == 0:
        return _nan(), _nan()
    return float(rows.mean()), float(cols.mean())


def _center_of_mass(context: MeasurementContext) -> tuple[float, float]:
    rows, cols = np.nonzero(context.local_mask)
    if rows.size == 0:
        return _nan(), _nan()
    weights = context.local_image[rows, cols].astype(float)
    weights = np.where(np.isfinite(weights), weights, 0.0)
    weights = np.clip(weights - np.nanmin(weights), 0.0, None)
    total = float(weights.sum())
    if total <= 0:
        return _centroid(context)
    return float((rows * weights).sum() / total), float((cols * weights).sum() / total)


def _feret(context: MeasurementContext, *, scaled: bool = False) -> dict[str, float]:
    """Max/min calipers over the convex hull.

    ``scaled`` scales the hull **before** the calipers are taken, which is the
    only way to get a calibrated Feret under anisotropic pixels: the longest
    chord of a shape is not in general the longest chord of the same shape
    stretched along one axis, so multiplying a pixel Feret by a mean scale
    gives a number that is neither of the two lengths involved.
    """
    rows, cols = np.nonzero(context.local_mask)
    if rows.size < 2:
        return {"max": _nan(), "min": _nan(), "angle": _nan(), "x": _nan(), "y": _nan()}
    points = np.column_stack([rows, cols]).astype(float)
    if scaled:
        points = points * np.array([context.row_scale, context.col_scale], dtype=float)
    try:
        from scipy.spatial import ConvexHull

        hull = points[ConvexHull(points).vertices]
    except Exception:
        hull = points

    diffs = hull[:, None, :] - hull[None, :, :]
    distances = np.linalg.norm(diffs, axis=2)
    i, j = np.unravel_index(int(np.argmax(distances)), distances.shape)
    longest = float(distances[i, j])
    vector = hull[j] - hull[i]
    angle = float(np.degrees(np.arctan2(-vector[0], vector[1])) % 180.0)

    # Minimum caliper: the smallest width over all hull-edge directions.
    minimum = longest
    count = len(hull)
    for index in range(count):
        edge = hull[(index + 1) % count] - hull[index]
        norm = np.linalg.norm(edge)
        if norm == 0:
            continue
        normal = np.array([-edge[1], edge[0]]) / norm
        widths = hull @ normal
        minimum = min(minimum, float(widths.max() - widths.min()))

    return {
        "max": longest,
        "min": minimum,
        "angle": angle,
        "y": float(hull[i][0]),
        "x": float(hull[i][1]),
    }


# --------------------------------------------------------------------------
# basic intensity and area
# --------------------------------------------------------------------------

@measurement(id="area_px", label="Area (px)", group="Basic", domain="pixel",
             default_on=True)
def _area_px(context):
    if context.is_line:
        # A line has no area. Reporting 0 would read as "measured, and empty".
        return _nan()
    return context.area_pixels


@measurement(id="area_cal", label="Area", group="Basic", domain="calibrated",
             unit_kind="area", default_on=True)
def _area_cal(context):
    if context.is_line:
        return _nan()
    return context.area_pixels * context.pixel_area


@measurement(id="finite_px", line_safe=True, label="Finite px", group="Basic", default_on=True,
             note="Pixels with a finite value; the rest are excluded from "
                  "intensity statistics but still counted in Area.")
def _finite_px(context):
    return int(context.values.size)


@measurement(id="mean", line_safe=True, label="Mean", group="Basic", unit_kind="intensity",
             default_on=True)
def _mean(context):
    values = context.values
    return float(values.mean()) if values.size else _nan()


@measurement(id="std", line_safe=True, label="StdDev", group="Basic", unit_kind="intensity",
             default_on=True,
             note="Sample standard deviation (n-1), matching ImageJ. NaN for "
                  "fewer than two pixels, where it is undefined rather than 0.")
def _std(context):
    values = context.values
    if values.size < 2:
        # n-1 is undefined for a single pixel; reporting 0 would claim a
        # spread was measured and found to be none.
        return _nan()
    return float(values.std(ddof=1))


@measurement(id="median", line_safe=True, label="Median", group="Basic", unit_kind="intensity",
             default_on=True)
def _median(context):
    values = context.values
    return float(np.median(values)) if values.size else _nan()


@measurement(id="min", line_safe=True, label="Min", group="Basic", unit_kind="intensity",
             default_on=True)
def _min(context):
    values = context.values
    return float(values.min()) if values.size else _nan()


@measurement(id="max", line_safe=True, label="Max", group="Basic", unit_kind="intensity",
             default_on=True)
def _max(context):
    values = context.values
    return float(values.max()) if values.size else _nan()


@measurement(id="sum", line_safe=True, label="Sum", group="Basic", unit_kind="intensity",
             default_on=True, note="Equals RawIntDen.")
def _sum(context):
    values = context.values
    return float(values.sum()) if values.size else _nan()


@measurement(id="mode", line_safe=True, label="Mode", group="Basic", unit_kind="intensity",
             note="Most frequent value; float data is binned into 256 bins.")
def _mode(context):
    values = context.values
    if values.size == 0:
        return _nan()
    if np.issubdtype(values.dtype, np.integer):
        counts = np.bincount(values.astype(np.int64) - int(values.min()))
        return float(int(values.min()) + int(np.argmax(counts)))
    counts, edges = np.histogram(values, bins=256)
    index = int(np.argmax(counts))
    return float((edges[index] + edges[index + 1]) / 2.0)


@measurement(id="skewness", line_safe=True, label="Skew", group="Intensity")
def _skewness(context):
    values = context.values
    if values.size < 3:
        return _nan()
    centred = values - values.mean()
    spread = centred.std(ddof=1)
    return float((centred ** 3).mean() / spread ** 3) if spread else _nan()


@measurement(id="kurtosis", line_safe=True, label="Kurt", group="Intensity",
             note="Excess kurtosis: 0 for a normal distribution.")
def _kurtosis(context):
    values = context.values
    if values.size < 4:
        return _nan()
    centred = values - values.mean()
    spread = centred.std(ddof=1)
    return float((centred ** 4).mean() / spread ** 4 - 3.0) if spread else _nan()


@measurement(id="int_den_px", label="IntDen (px)", group="Intensity",
             domain="pixel")
def _int_den_px(context):
    values = context.values
    return float(context.area_pixels * values.mean()) if values.size else _nan()


@measurement(id="int_den_cal", label="IntDen", group="Intensity",
             domain="calibrated", unit_kind="area")
def _int_den_cal(context):
    values = context.values
    if not values.size:
        return _nan()
    return float(context.area_pixels * context.pixel_area * values.mean())


@measurement(id="raw_int_den", label="RawIntDen", group="Intensity",
             unit_kind="intensity",
             note="Sum of the pixel values; area-independent, so it has no "
                  "calibrated twin.")
def _raw_int_den(context):
    values = context.values
    return float(values.sum()) if values.size else _nan()


@measurement(id="area_fraction", label="%Area", group="Intensity",
             note="Percentage of the ROI's pixels inside the threshold; 100 "
                  "when no threshold is set.")
def _area_fraction(context):
    total = context.area_pixels
    if not total:
        return _nan()
    inside = context.local_image[context.local_mask]
    finite = inside[np.isfinite(inside)]
    if context.threshold is None:
        return 100.0 * finite.size / total
    low, high = context.threshold
    within = finite[(finite >= low) & (finite <= high)]
    return 100.0 * within.size / total


# --------------------------------------------------------------------------
# position
# --------------------------------------------------------------------------

def _position_measurement(id_, label, getter, *, domain, unit_kind="length"):
    @measurement(id=id_, label=label, group="Position", domain=domain,
                 unit_kind=unit_kind)
    def _compute(context, _getter=getter):
        return _getter(context)
    return _compute


def _offset(context) -> tuple[int, int]:
    from imswitch.imcommon.algorithms.roi_geometry import roi_bounds

    r0, _r1, c0, _c1 = roi_bounds(context.roi)
    return int(r0), int(c0)


_position_measurement(
    "centroid_r_px", "Y (px)",
    lambda ctx: _centroid(ctx)[0] + _offset(ctx)[0], domain="pixel")
_position_measurement(
    "centroid_c_px", "X (px)",
    lambda ctx: _centroid(ctx)[1] + _offset(ctx)[1], domain="pixel")
_position_measurement(
    "centroid_r_cal", "Y",
    lambda ctx: (_centroid(ctx)[0] + _offset(ctx)[0]) * ctx.row_scale,
    domain="calibrated")
_position_measurement(
    "centroid_c_cal", "X",
    lambda ctx: (_centroid(ctx)[1] + _offset(ctx)[1]) * ctx.col_scale,
    domain="calibrated")
_position_measurement(
    "com_r_px", "YM (px)",
    lambda ctx: _center_of_mass(ctx)[0] + _offset(ctx)[0], domain="pixel")
_position_measurement(
    "com_c_px", "XM (px)",
    lambda ctx: _center_of_mass(ctx)[1] + _offset(ctx)[1], domain="pixel")
_position_measurement(
    "com_r_cal", "YM",
    lambda ctx: (_center_of_mass(ctx)[0] + _offset(ctx)[0]) * ctx.row_scale,
    domain="calibrated")
_position_measurement(
    "com_c_cal", "XM",
    lambda ctx: (_center_of_mass(ctx)[1] + _offset(ctx)[1]) * ctx.col_scale,
    domain="calibrated")

_position_measurement("by_px", "BY (px)", lambda ctx: _offset(ctx)[0], domain="pixel")
_position_measurement("bx_px", "BX (px)", lambda ctx: _offset(ctx)[1], domain="pixel")
_position_measurement(
    "height_px", "Height (px)",
    lambda ctx: int(ctx.local_mask.shape[0]), domain="pixel")
_position_measurement(
    "width_px", "Width (px)",
    lambda ctx: int(ctx.local_mask.shape[1]), domain="pixel")
_position_measurement(
    "by_cal", "BY", lambda ctx: _offset(ctx)[0] * ctx.row_scale, domain="calibrated")
_position_measurement(
    "bx_cal", "BX", lambda ctx: _offset(ctx)[1] * ctx.col_scale, domain="calibrated")
_position_measurement(
    "height_cal", "Height",
    lambda ctx: ctx.local_mask.shape[0] * ctx.row_scale, domain="calibrated")
_position_measurement(
    "width_cal", "Width",
    lambda ctx: ctx.local_mask.shape[1] * ctx.col_scale, domain="calibrated")


# --------------------------------------------------------------------------
# shape
# --------------------------------------------------------------------------

@measurement(id="perimeter_px", label="Perim. (px)", group="Shape",
             domain="pixel",
             note="Vector ROIs: exact polygon length. Masks: the Crofton "
                  "estimator, which is closer to ImageJ's traced boundary "
                  "than naive edge counting.")
def _perimeter_px(context):
    return _outline_lengths(context)[0]


@measurement(id="perimeter_cal", label="Perim.", group="Shape",
             domain="calibrated", unit_kind="length")
def _perimeter_cal(context):
    return _outline_lengths(context)[1]


def _axis_lengths(context: MeasurementContext, *, scaled: bool = False):
    """``(major, minor)`` of the equivalent ellipse, optionally in world units.

    The same second-moment definition scikit-image uses (4*sqrt of the
    covariance eigenvalues), computed here so the calibrated form can scale the
    **coordinates** rather than the answer. Scaling the answer by a mean pixel
    size is wrong whenever the pixels are not square, and wrong in a way that
    depends on the shape's orientation — a horizontal and a vertical bar of the
    same size would report the same major axis.
    """
    rows, cols = np.nonzero(context.local_mask)
    if rows.size < 2:
        return _nan(), _nan()
    points = np.column_stack([rows, cols]).astype(float)
    if scaled:
        points = points * np.array([context.row_scale, context.col_scale], dtype=float)
    centred = points - points.mean(axis=0)
    covariance = (centred.T @ centred) / points.shape[0]
    eigenvalues = np.linalg.eigvalsh(covariance)
    eigenvalues = np.clip(eigenvalues, 0.0, None)
    return float(4.0 * np.sqrt(eigenvalues[-1])), float(4.0 * np.sqrt(eigenvalues[0]))


@measurement(id="major_px", label="Major (px)", group="Shape", domain="pixel")
def _major_px(context):
    return _axis_lengths(context)[0]


@measurement(id="minor_px", label="Minor (px)", group="Shape", domain="pixel")
def _minor_px(context):
    return _axis_lengths(context)[1]


@measurement(id="major_cal", label="Major", group="Shape", domain="calibrated",
             unit_kind="length")
def _major_cal(context):
    return _axis_lengths(context, scaled=True)[0]


@measurement(id="minor_cal", label="Minor", group="Shape", domain="calibrated",
             unit_kind="length")
def _minor_cal(context):
    return _axis_lengths(context, scaled=True)[1]


@measurement(id="angle", label="Angle", group="Shape",
             note="Fitted-ellipse orientation in degrees, in the PIXEL frame. "
                  "Under anisotropic pixels an angle in world units would "
                  "differ; pixel_aspect says when that matters.")
def _angle(context):
    props = _regionprops(context)
    if props is None:
        return _nan()
    return float(np.degrees(props.orientation) % 180.0)


@measurement(id="circularity", label="Circ.", group="Shape",
             note="4*pi*area / perimeter^2 on the pixel grid, clipped to 1.")
def _circularity(context):
    perimeter, _cal = _outline_lengths(context)
    if not perimeter or not np.isfinite(perimeter):
        return _nan()
    value = 4.0 * np.pi * context.area_pixels / (perimeter ** 2)
    return float(min(value, 1.0))


@measurement(id="aspect_ratio", label="AR", group="Shape")
def _aspect_ratio(context):
    props = _regionprops(context)
    if props is None or not props.minor_axis_length:
        return _nan()
    return float(props.major_axis_length / props.minor_axis_length)


@measurement(id="roundness", label="Round", group="Shape")
def _roundness(context):
    props = _regionprops(context)
    if props is None or not props.major_axis_length:
        return _nan()
    return float(4.0 * context.area_pixels / (np.pi * props.major_axis_length ** 2))


@measurement(id="solidity", label="Solidity", group="Shape")
def _solidity(context):
    props = _regionprops(context)
    if props is None:
        return _nan()
    try:
        return float(props.solidity)
    except Exception:
        return _nan()


@measurement(id="feret_px", label="Feret (px)", group="Shape", domain="pixel")
def _feret_px(context):
    return _feret(context)["max"]


@measurement(id="feret_cal", label="Feret", group="Shape", domain="calibrated",
             unit_kind="length")
def _feret_cal(context):
    return _feret(context, scaled=True)["max"]


@measurement(id="feret_min_px", label="MinFeret (px)", group="Shape", domain="pixel")
def _feret_min_px(context):
    return _feret(context)["min"]


@measurement(id="feret_min_cal", label="MinFeret", group="Shape",
             domain="calibrated", unit_kind="length")
def _feret_min_cal(context):
    return _feret(context, scaled=True)["min"]


@measurement(id="feret_angle", label="FeretAngle", group="Shape")
def _feret_angle(context):
    return _feret(context)["angle"]


@measurement(id="feret_y_px", label="FeretY (px)", group="Shape", domain="pixel")
def _feret_y(context):
    return _feret(context)["y"] + _offset(context)[0]


@measurement(id="feret_x_px", label="FeretX (px)", group="Shape", domain="pixel")
def _feret_x(context):
    return _feret(context)["x"] + _offset(context)[1]


@measurement(id="feret_y_cal", label="FeretY", group="Shape",
             domain="calibrated", unit_kind="length")
def _feret_y_cal(context):
    return (_feret(context)["y"] + _offset(context)[0]) * context.row_scale


@measurement(id="feret_x_cal", label="FeretX", group="Shape",
             domain="calibrated", unit_kind="length")
def _feret_x_cal(context):
    return (_feret(context)["x"] + _offset(context)[1]) * context.col_scale


@measurement(id="pixel_aspect", line_safe=True, label="PixelAR", group="Shape", default_on=True,
             note="row_scale / col_scale. Away from 1, the shape descriptors "
                  "above are pixel-grid values and not directly comparable "
                  "with ImageJ's.")
def _pixel_aspect(context):
    return context.pixel_aspect


# --------------------------------------------------------------------------
# line ROIs (P-3.7)
# --------------------------------------------------------------------------

def _line_points(context) -> np.ndarray | None:
    vertices = getattr(context.roi, "vertices", None)
    from imswitch.imcommon.algorithms.roi_geometry import roi_capabilities

    if vertices is None or not roi_capabilities(context.roi.roi_type).is_line:
        return None
    return np.asarray(vertices, dtype=float)


@measurement(id="length_px", line_safe=True, label="Length (px)", group="Line", domain="pixel")
def _length_px(context):
    points = _line_points(context)
    if points is None or len(points) < 2:
        return _nan()
    return float(np.sum(np.linalg.norm(np.diff(points, axis=0), axis=1)))


@measurement(id="length_cal", line_safe=True, label="Length", group="Line", domain="calibrated",
             unit_kind="length")
def _length_cal(context):
    points = _line_points(context)
    if points is None or len(points) < 2:
        return _nan()
    scaled = points * np.array([context.row_scale, context.col_scale])
    return float(np.sum(np.linalg.norm(np.diff(scaled, axis=0), axis=1)))


@measurement(id="line_angle", line_safe=True, label="LineAngle", group="Line")
def _line_angle(context):
    points = _line_points(context)
    if points is None or len(points) < 2:
        return _nan()
    delta = points[-1] - points[0]
    return float(np.degrees(np.arctan2(-delta[0], delta[1])) % 180.0)


#: Statistics along a line, as their own columns. They repeat what ``mean``,
#: ``min``, ``max`` and ``std`` already report for a line ROI — which is
#: deliberate: those columns hold area statistics for an area ROI, so a set
#: measuring both needs somewhere a line's numbers are unambiguously a line's.
_LINE_STATISTICS = (
    ("line_mean", "LineMean", lambda values: float(np.mean(values))),
    ("line_min", "LineMin", lambda values: float(np.min(values))),
    ("line_max", "LineMax", lambda values: float(np.max(values))),
    (
        "line_std",
        "LineStdDev",
        lambda values: float(np.std(values, ddof=1)) if values.size > 1 else _nan(),
    ),
)


def _register_line_statistics() -> None:
    for identifier, label, reduce in _LINE_STATISTICS:
        def compute(context, _reduce=reduce):
            if not context.is_line:
                return _nan()
            values = context.values
            return _nan() if values.size == 0 else _reduce(values)

        measurement(
            id=identifier,
            label=label,
            group="Line",
            line_safe=True,
            unit_kind="intensity",
            note="Sampled along the line, averaged over the configured line "
                 "width; NaN for an area ROI.",
        )(compute)


_register_line_statistics()


@measurement(id="line_width", line_safe=True, label="LineWidth", group="Line",
             note="Perpendicular samples averaged, as in ImageJ. 1 samples "
                  "the line itself.")
def _line_width(context):
    return int(context.line_width) if context.is_line else _nan()


__all__ = [
    "ALWAYS_ON",
    "ANISOTROPY_TOLERANCE",
    "MEASUREMENTS",
    "Measurement",
    "MeasurementContext",
    "applicable_selection",
    "column_label",
    "default_selection",
    "groups",
    "measure",
    "measurement",
    "selected_measurements",
]


# --------------------------------------------------------------------------
# turning the registry into rows
# --------------------------------------------------------------------------

#: Ids that are always emitted: without them a row cannot be read safely.
#: `spatial_unit` says what the calibrated columns mean, and `geometry_match`
#: says how comparable the ROI was to the image it was measured on.
ALWAYS_ON = ("spatial_unit", "geometry_match")


def default_selection() -> tuple[str, ...]:
    """The measurements shown out of the box."""
    return tuple(
        entry.id for entry in MEASUREMENTS.values() if entry.default_on
    )


def selected_measurements(selection=None) -> tuple[Measurement, ...]:
    """The chosen measurements, in registration order.

    ``None`` means "not configured" and takes the defaults. An **empty**
    selection means exactly that: the caller unticked everything, and turning
    that back into the defaults would make the choice impossible to express.
    """
    ids = default_selection() if selection is None else tuple(selection)
    return tuple(MEASUREMENTS[i] for i in ids if i in MEASUREMENTS)


def applicable_selection(selection=None, *, unit: str = "px") -> tuple[str, ...]:
    """The selection with measurements that cannot mean anything dropped.

    On an uncalibrated frame a ``_cal`` column is not a calibrated value — it
    is the pixel value under a heading that implies otherwise, sitting beside
    the identical ``_px`` column. Dropping it says "there is no calibration
    here", which is the true thing.
    """
    ids = default_selection() if selection is None else tuple(selection)
    if unit and unit != "px":
        return ids
    return tuple(
        key
        for key in ids
        if MEASUREMENTS.get(key) is None or MEASUREMENTS[key].domain != "calibrated"
    )


def measure(context: MeasurementContext, selection=None) -> dict[str, Any]:
    """Compute the selected measurements for one ROI on one plane.

    A measurement that fails takes its own column down, not the row: one
    unfittable ellipse should not cost the mean and the area beside it.
    """
    row: dict[str, Any] = {}
    for entry in selected_measurements(selection):
        if context.is_line and not entry.line_safe:
            # An area or shape descriptor of a line is not a small number, it
            # is no number: computing one from the empty mask reported a
            # straight line's circularity as 0.0, which reads as measured.
            row[entry.id] = float("nan")
            continue
        try:
            row[entry.id] = entry.compute(context)
        except Exception:
            row[entry.id] = float("nan")

    row["spatial_unit"] = context.unit
    row["geometry_match"] = context.geometry_match
    if context.anisotropic:
        # Stated on the row rather than left for the reader to infer from
        # pixel_aspect: the shape descriptors are pixel-grid values here.
        row["shape_note"] = "anisotropic pixels: shape descriptors are pixel-grid"
    return row


def column_label(measurement_id: str, unit: str = "px") -> str:
    """The header for a column, which is the only place a unit appears."""
    entry = MEASUREMENTS.get(measurement_id)
    if entry is None:
        return measurement_id
    if entry.domain != "calibrated" or unit in ("", "px"):
        return entry.label
    suffix = f"{unit}²" if entry.unit_kind == "area" else unit
    return f"{entry.label} ({suffix})"


def groups() -> dict[str, list[Measurement]]:
    """Measurements by group, in registration order — what the dialog shows."""
    ordered: dict[str, list[Measurement]] = {}
    for entry in MEASUREMENTS.values():
        ordered.setdefault(entry.group, []).append(entry)
    return ordered
