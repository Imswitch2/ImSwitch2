from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor
from typing import Literal, Mapping, Sequence

import numpy as np
from scipy.optimize import curve_fit
from scipy.signal import find_peaks
from skimage import measure, morphology
from skimage.transform import rescale

from .bead_fits import FIT_MODELS, FitResult


@dataclass(frozen=True)
class BeadAnalysisParameters:
    """Passive bead-recognition analysis parameters."""

    min_area: int = 50
    max_area: int = 1000
    tol_peaks_pos: int = 10
    thresh_coeff: float = 0.2
    erosion_coeff: float = 0.2

    @classmethod
    def from_mapping(cls, values: Mapping[str, object] | None) -> "BeadAnalysisParameters":
        """Create parameters from a widget/settings dictionary."""
        if values is None:
            return cls()
        return cls(
            min_area=int(values.get("min_area", cls.min_area)),
            max_area=int(values.get("max_area", cls.max_area)),
            tol_peaks_pos=int(values.get("tol_peaks_pos", cls.tol_peaks_pos)),
            thresh_coeff=float(values.get("thresh_coeff", cls.thresh_coeff)),
            erosion_coeff=float(values.get("erosion_coeff", cls.erosion_coeff)),
        )

    def as_dict(self) -> dict[str, int | float]:
        """Return the legacy dictionary representation used by BeadRecWidget."""
        return {
            "min_area": self.min_area,
            "max_area": self.max_area,
            "tol_peaks_pos": self.tol_peaks_pos,
            "thresh_coeff": self.thresh_coeff,
            "erosion_coeff": self.erosion_coeff,
        }


@dataclass(frozen=True)
class RoiBounds:
    """Integer ROI bounds in NumPy row/column slicing order."""

    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        """Return ROI width in pixels."""
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        """Return ROI height in pixels."""
        return self.y1 - self.y0

    def as_slices(self) -> tuple[slice, slice]:
        """Return NumPy slices as `(rows, columns)`."""
        return slice(self.y0, self.y1), slice(self.x0, self.x1)


@dataclass(frozen=True)
class ReconstructionUpdate:
    """Result of appending detector frames to a reconstruction buffer."""

    buffer: np.ndarray
    next_index: int
    frames_written: int
    wrapped: bool


@dataclass(frozen=True)
class BeadAcquisitionConfig:
    """Immutable reconstruction acquisition settings for one BeadRec run."""

    scan_dims: tuple[int, int]
    wrap: bool = True
    poll_interval_s: float = 0.0001

    @classmethod
    def from_scan_dims(
        cls,
        scan_dims: Sequence[int],
        *,
        wrap: bool = True,
        poll_interval_s: float = 0.0001,
    ) -> "BeadAcquisitionConfig":
        """Create a validated acquisition config from scan dimensions."""
        dims = _normalize_scan_dims(scan_dims)
        if poll_interval_s <= 0:
            raise ValueError("Poll interval must be positive")
        return cls(scan_dims=dims, wrap=wrap, poll_interval_s=float(poll_interval_s))

    @property
    def total_pixels(self) -> int:
        """Return the number of reconstruction pixels expected for this run."""
        return self.scan_dims[0] * self.scan_dims[1]


@dataclass(frozen=True)
class BeadWorkerUpdate:
    """Thread-safe worker update emitted to the BeadRec controller."""

    buffer: np.ndarray
    filled_pixels: int
    total_pixels: int
    frames_written: int
    wrapped: bool


@dataclass(frozen=True)
class CenterDetectionResult:
    """Result of a bead center search."""

    coord: tuple[int, int] | None
    reason: str | None = None

    @property
    def found(self) -> bool:
        """Return whether the search found a center coordinate."""
        return self.coord is not None


@dataclass(frozen=True)
class DonutAnalysisResult:
    """Pure donut-analysis metrics and intermediate masks."""

    accepted: bool
    reason: str | None
    coord: tuple[int, int] | None
    min_value: float | None = None
    background: float | None = None
    background_std: float | None = None
    fill_x: float | None = None
    fill_y: float | None = None
    fill_x_std: float | None = None
    fill_y_std: float | None = None
    binarized: np.ndarray | None = None
    selected_mask: np.ndarray | None = None
    eroded_mask: np.ndarray | None = None
    background_mask: np.ndarray | None = None
    line_x: np.ndarray | None = None
    line_y: np.ndarray | None = None
    peak_x_positions: tuple[int, int] | None = None
    peak_y_positions: tuple[int, int] | None = None
    peak_x_values: tuple[float, float] | None = None
    peak_y_values: tuple[float, float] | None = None


@dataclass(frozen=True)
class BeadRecResultRecord:
    """Saved BeadRec reconstruction plus passive metadata."""

    name: str
    image: np.ndarray
    axial_name: str | None = None
    source_path: str | None = None
    timestamp: float | None = None
    scaled: bool = False

    def metadata(self) -> dict[str, object]:
        """Return JSON-safe metadata without image pixel data."""
        return {
            "name": self.name,
            "axial_name": self.axial_name,
            "source_path": self.source_path,
            "timestamp": self.timestamp,
            "scaled": self.scaled,
        }


CenterSearchMode = Literal["Maxima", "Minima"]


def normalize_roi_bounds(bounds: Sequence[float], image_shape: Sequence[int]) -> RoiBounds:
    """Clip visual ROI bounds to valid integer image coordinates.

    `bounds` must be `(x0, y0, x1, y1)`. `image_shape` must provide at least
    `(height, width)`. Floating ROI coordinates are rounded outward so the
    selected visual region is preserved.
    """
    if len(bounds) != 4:
        raise ValueError("ROI bounds must contain exactly four values")
    if len(image_shape) < 2:
        raise ValueError("Image shape must contain at least height and width")

    height = int(image_shape[0])
    width = int(image_shape[1])
    if height <= 0 or width <= 0:
        raise ValueError("Image shape must be positive")

    x0_raw, y0_raw, x1_raw, y1_raw = (float(value) for value in bounds)
    x0_unclipped = floor(min(x0_raw, x1_raw))
    y0_unclipped = floor(min(y0_raw, y1_raw))
    x1_unclipped = ceil(max(x0_raw, x1_raw))
    y1_unclipped = ceil(max(y0_raw, y1_raw))

    x0 = min(max(x0_unclipped, 0), width)
    y0 = min(max(y0_unclipped, 0), height)
    x1 = min(max(x1_unclipped, 0), width)
    y1 = min(max(y1_unclipped, 0), height)

    roi = RoiBounds(x0=x0, y0=y0, x1=x1, y1=y1)
    if roi.width <= 0 or roi.height <= 0:
        raise ValueError("ROI does not overlap the image")
    return roi


def mean_intensity_in_roi(frame: np.ndarray, roi: RoiBounds) -> float:
    """Return the mean intensity inside `roi` for one 2D detector frame."""
    if frame.ndim != 2:
        raise ValueError("Bead reconstruction expects 2D detector frames")
    clipped_roi = normalize_roi_bounds((roi.x0, roi.y0, roi.x1, roi.y1), frame.shape)
    rows, cols = clipped_roi.as_slices()
    return float(np.mean(frame[rows, cols]))


def create_reconstruction_buffer(scan_dims: Sequence[int]) -> np.ndarray:
    """Create a flat reconstruction buffer for `(x_pixels, y_pixels)` scan dims."""
    dims = _normalize_scan_dims(scan_dims)
    return np.zeros(dims[0] * dims[1], dtype=float)


def reconstruction_image(buffer: np.ndarray, scan_dims: Sequence[int]) -> np.ndarray:
    """Reshape a flat reconstruction buffer into BeadRec display orientation."""
    dims = _normalize_scan_dims(scan_dims)
    expected_size = dims[0] * dims[1]
    if buffer.size != expected_size:
        raise ValueError(f"Buffer size {buffer.size} does not match scan dims {tuple(dims)}")
    return np.reshape(buffer, (dims[1], dims[0]))


def append_roi_means(
    buffer: np.ndarray,
    start_index: int,
    frames: Sequence[np.ndarray],
    roi: RoiBounds,
    *,
    wrap: bool = True,
) -> ReconstructionUpdate:
    """Append ROI means from frames into a reconstruction buffer.

    The returned buffer is a copy. This keeps the helper deterministic and
    independent from the live controller state while preserving the current
    BeadRec wraparound behavior by default.
    """
    if buffer.ndim != 1:
        raise ValueError("Reconstruction buffer must be one-dimensional")
    if buffer.size == 0:
        raise ValueError("Reconstruction buffer cannot be empty")
    if not 0 <= start_index < buffer.size:
        raise ValueError("Start index is outside the reconstruction buffer")

    updated = buffer.astype(float, copy=True)
    index = start_index
    frames_written = 0
    wrapped = False

    for frame in frames:
        if index >= updated.size:
            if not wrap:
                break
            index = 0
            wrapped = True
        updated[index] = mean_intensity_in_roi(np.asarray(frame), roi)
        index += 1
        frames_written += 1

    if index == updated.size:
        if wrap:
            index = 0
            wrapped = frames_written > 0

    return ReconstructionUpdate(
        buffer=updated,
        next_index=index,
        frames_written=frames_written,
        wrapped=wrapped,
    )


def rescale_reconstruction_to_pixel_size(
    image: np.ndarray,
    step_sizes: Sequence[float],
) -> np.ndarray:
    """Rescale a reconstructed image using physical scan step sizes.

    `step_sizes` are expected as `(x_step, y_step)` in physical units. The
    display image is row-major `(y, x)`, so y uses `step_sizes[1]` and x uses
    `step_sizes[0]`.
    """
    if image.ndim != 2:
        raise ValueError("Reconstruction scaling expects a 2D image")
    if len(step_sizes) < 2:
        raise ValueError("At least x/y step sizes are required")

    x_step = float(step_sizes[0])
    y_step = float(step_sizes[1])
    if x_step <= 0 or y_step <= 0:
        raise ValueError("Step sizes must be positive")
    if np.isclose(x_step, y_step):
        return image

    reference = min(x_step, y_step)
    scale_y = y_step / reference
    scale_x = x_step / reference
    return rescale(
        image,
        (scale_y, scale_x),
        anti_aliasing=False,
        mode="reflect",
        preserve_range=True,
    )


def find_center_foci(
    image: np.ndarray,
    params: Mapping[str, object] | BeadAnalysisParameters | None = None,
) -> CenterDetectionResult:
    """Find a foci center as the maximum inside the largest valid component."""
    prm = _coerce_analysis_params(params)
    prepared = _prepare_component_mask(image, prm)
    if not prepared.accepted:
        return CenterDetectionResult(coord=None, reason=prepared.reason)

    component_mask = prepared.selected_mask[3:-3, 3:-3]
    masked_image = np.asarray(image).copy()
    masked_image[~component_mask] = 0
    max_y, max_x = np.unravel_index(np.argmax(masked_image), masked_image.shape)
    return CenterDetectionResult(coord=(int(max_y), int(max_x)))


def find_center_donut(
    image: np.ndarray,
    params: Mapping[str, object] | BeadAnalysisParameters | None = None,
) -> CenterDetectionResult:
    """Find a donut center as the minimum inside the eroded selected component."""
    result = analyze_donut(image, params)
    return CenterDetectionResult(coord=result.coord, reason=result.reason)


def find_bead_center(
    image: np.ndarray,
    mode: CenterSearchMode,
    params: Mapping[str, object] | BeadAnalysisParameters | None = None,
) -> CenterDetectionResult:
    """Find a bead center using the named legacy mode."""
    if mode == "Maxima":
        return find_center_foci(image, params)
    if mode == "Minima":
        return find_center_donut(image, params)
    raise ValueError("Center search mode unknown, should be 'Maxima', or 'Minima'")


def analyze_donut(
    image: np.ndarray,
    params: Mapping[str, object] | BeadAnalysisParameters | None = None,
) -> DonutAnalysisResult:
    """Analyze a donut image without plotting or GUI side effects."""
    prm = _coerce_analysis_params(params)
    prepared = _prepare_component_mask(image, prm)
    if not prepared.accepted:
        return DonutAnalysisResult(
            accepted=False,
            reason=prepared.reason,
            coord=None,
            binarized=prepared.binarized,
        )

    im = _validate_2d_image(image)
    im_bw2 = morphology.closing(prepared.selected_mask, morphology.disk(5))
    props = measure.regionprops(im_bw2.astype(int))
    if not props:
        return DonutAnalysisResult(
            accepted=False,
            reason="selected component disappeared after closing",
            coord=None,
            binarized=prepared.binarized,
            selected_mask=im_bw2[3:-3, 3:-3],
        )

    blob_diameter = props[0].equivalent_diameter_area
    radius = max(round(blob_diameter * prm.erosion_coeff), 1)
    im_bw3 = morphology.erosion(im_bw2, morphology.disk(radius))
    eroded_crop = im_bw3[3:-3, 3:-3]
    if not np.any(eroded_crop):
        return DonutAnalysisResult(
            accepted=False,
            reason="eroded component is empty",
            coord=None,
            binarized=prepared.binarized,
            selected_mask=im_bw2[3:-3, 3:-3],
            eroded_mask=eroded_crop,
        )

    background_mask = morphology.dilation(prepared.binarized[3:-3, 3:-3], morphology.disk(3))
    background_mask[:2, :] = True
    background_mask[-2:, :] = True
    background_mask[:, :2] = True
    background_mask[:, -2:] = True

    background_values = im[~background_mask]
    background_values = background_values[background_values != 0]
    if background_values.size == 0:
        return DonutAnalysisResult(
            accepted=False,
            reason="background mask contains no non-zero pixels",
            coord=None,
            binarized=prepared.binarized,
            selected_mask=im_bw2[3:-3, 3:-3],
            eroded_mask=eroded_crop,
            background_mask=background_mask,
        )
    background = float(np.mean(background_values))
    background_std = float(np.std(background_values))

    masked_image = im.copy()
    masked_image[~eroded_crop] = np.inf
    min_value = float(np.min(masked_image))
    min_y, min_x = np.unravel_index(np.argmin(masked_image), masked_image.shape)

    line_x = im[min_y, :]
    line_y = im[:, min_x]
    x_profile = _select_two_profile_peaks(line_x, min_x, prm.tol_peaks_pos)
    y_profile = _select_two_profile_peaks(line_y, min_y, prm.tol_peaks_pos)
    if x_profile is None or y_profile is None:
        return DonutAnalysisResult(
            accepted=False,
            reason="insufficient peaks near donut center",
            coord=(int(min_y), int(min_x)),
            min_value=min_value,
            background=background,
            background_std=background_std,
            binarized=prepared.binarized,
            selected_mask=im_bw2[3:-3, 3:-3],
            eroded_mask=eroded_crop,
            background_mask=background_mask,
            line_x=line_x,
            line_y=line_y,
        )

    fill_x, fill_x_std = _fill_metric(min_value, background, background_std, x_profile[1])
    fill_y, fill_y_std = _fill_metric(min_value, background, background_std, y_profile[1])
    return DonutAnalysisResult(
        accepted=True,
        reason=None,
        coord=(int(min_y), int(min_x)),
        min_value=min_value,
        background=background,
        background_std=background_std,
        fill_x=fill_x,
        fill_y=fill_y,
        fill_x_std=fill_x_std,
        fill_y_std=fill_y_std,
        binarized=prepared.binarized,
        selected_mask=im_bw2[3:-3, 3:-3],
        eroded_mask=eroded_crop,
        background_mask=background_mask,
        line_x=line_x,
        line_y=line_y,
        peak_x_positions=x_profile[0],
        peak_y_positions=y_profile[0],
        peak_x_values=x_profile[1],
        peak_y_values=y_profile[1],
    )


def _normalize_scan_dims(scan_dims: Sequence[int]) -> tuple[int, int]:
    if len(scan_dims) < 2:
        raise ValueError("Scan dims must contain at least x and y")
    dims = (int(scan_dims[0]), int(scan_dims[1]))
    if dims[0] <= 0 or dims[1] <= 0:
        raise ValueError("Scan dims must be positive")
    return dims


@dataclass(frozen=True)
class _PreparedComponentMask:
    accepted: bool
    reason: str | None
    binarized: np.ndarray
    selected_mask: np.ndarray | None = None


def _coerce_analysis_params(
    params: Mapping[str, object] | BeadAnalysisParameters | None,
) -> BeadAnalysisParameters:
    if isinstance(params, BeadAnalysisParameters):
        return params
    return BeadAnalysisParameters.from_mapping(params)


def _validate_2d_image(image: np.ndarray) -> np.ndarray:
    im = np.asarray(image)
    if im.ndim != 2:
        raise ValueError("Bead recognition expects a 2D image")
    if im.size == 0:
        raise ValueError("Bead recognition image cannot be empty")
    return im


def _prepare_component_mask(
    image: np.ndarray,
    params: BeadAnalysisParameters,
) -> _PreparedComponentMask:
    im = _validate_2d_image(image)
    im_pad = np.pad(im, pad_width=3, mode="constant", constant_values=float(np.min(im)))
    value_range = float(np.max(im) - np.min(im))
    threshold = params.thresh_coeff * value_range + float(np.min(im))
    binarized = im_pad > threshold

    labels = measure.label(binarized)
    props = measure.regionprops_table(labels, properties=("area",))
    areas = np.asarray(props["area"])
    if areas.size == 0:
        return _PreparedComponentMask(
            accepted=False,
            reason="no connected components after thresholding",
            binarized=binarized,
        )

    sorted_idx = np.argsort(areas)[::-1]
    selected_idx = int(sorted_idx[0])
    selected_area = float(areas[selected_idx])
    if not (params.min_area < selected_area < params.max_area):
        return _PreparedComponentMask(
            accepted=False,
            reason=f"largest component area {selected_area:g} outside allowed range",
            binarized=binarized,
        )

    return _PreparedComponentMask(
        accepted=True,
        reason=None,
        binarized=binarized,
        selected_mask=labels == (selected_idx + 1),
    )


def _select_two_profile_peaks(
    line: np.ndarray,
    center: int,
    tolerance: int,
) -> tuple[tuple[int, int], tuple[float, float]] | None:
    peak_locations, _props = find_peaks(line)
    valid = np.where(np.abs(peak_locations - center) <= tolerance)[0]
    if valid.size < 2:
        return None

    filtered_positions = peak_locations[valid]
    filtered_values = line[filtered_positions]
    order = np.argsort(filtered_values)[::-1]
    positions = (
        int(filtered_positions[order[0]]),
        int(filtered_positions[order[1]]),
    )
    values = (
        float(filtered_values[order[0]]),
        float(filtered_values[order[1]]),
    )
    return positions, values


def _fill_metric(
    min_value: float,
    background: float,
    background_std: float,
    peak_values: tuple[float, float],
) -> tuple[float, float]:
    max_value = 0.5 * (peak_values[0] + peak_values[1])
    denominator = max_value - background
    if np.isclose(denominator, 0):
        return float("nan"), float("nan")

    fill = (min_value - background) / denominator
    std_max = abs(peak_values[0] - peak_values[1]) / (2**0.5)
    denom_squared = denominator**2
    df_dbg = -(max_value - min_value) / denom_squared
    df_dmax = (min_value - background) / denom_squared
    fill_std = (df_dbg**2 * background_std**2 + df_dmax**2 * std_max**2) ** 0.5
    return float(fill), float(fill_std)


def fit_bead(
    image: np.ndarray,
    model_name: str,
    params: Mapping[str, object] | BeadAnalysisParameters | None = None,
    roi: tuple[int, int, int, int] | None = None,
) -> FitResult:
    """Fit a parametric model to a bead image.

    Parameters
    ----------
    image : np.ndarray
        2D bead image to fit.
    model_name : str
        Model identifier from FIT_MODELS registry.
    params : Mapping or BeadAnalysisParameters, optional
        Analysis parameters for ROI detection (only used if roi is None).
    roi : tuple of int, optional
        ROI bounds as (x0, y0, x1, y1). If None, uses largest-CC bounding box
        from the existing _prepare_component_mask, expanded by a small margin.

    Returns
    -------
    FitResult
        Fit result with parameters, uncertainties, R², and center_px in
        full-image coordinates.

    Raises
    ------
    ValueError
        If model_name is not in FIT_MODELS, image is flat/constant, or fit fails.
    """
    if model_name not in FIT_MODELS:
        raise ValueError(f"Unknown model '{model_name}', available: {list(FIT_MODELS.keys())}")

    model = FIT_MODELS[model_name]
    im = _validate_2d_image(image)

    if np.ptp(im) == 0:
        raise ValueError("Cannot fit a flat/constant image (no intensity variation)")

    roi_offset_y = 0
    roi_offset_x = 0

    if roi is None:
        prm = _coerce_analysis_params(params)
        prepared = _prepare_component_mask(im, prm)

        if prepared.is_rejected:
            roi_y0, roi_y1 = 0, im.shape[0]
            roi_x0, roi_x1 = 0, im.shape[1]
        else:
            assert prepared.bbox is not None
            roi_y0, roi_x0, roi_y1, roi_x1 = prepared.bbox
            margin = 3
            roi_y0 = max(0, roi_y0 - margin)
            roi_x0 = max(0, roi_x0 - margin)
            roi_y1 = min(im.shape[0], roi_y1 + margin)
            roi_x1 = min(im.shape[1], roi_x1 + margin)

        roi_im = im[roi_y0:roi_y1, roi_x0:roi_x1]
        roi_mask = None if prepared.is_rejected else prepared.mask[roi_y0:roi_y1, roi_x0:roi_x1]
        roi_offset_y = roi_y0
        roi_offset_x = roi_x0
    else:
        x0, y0, x1, y1 = roi
        roi_y0, roi_y1 = y0, y1
        roi_x0, roi_x1 = x0, x1
        roi_im = im[roi_y0:roi_y1, roi_x0:roi_x1]
        roi_mask = None
        roi_offset_y = roi_y0
        roi_offset_x = roi_x0

    height, width = roi_im.shape
    y_grid, x_grid = np.indices(roi_im.shape, dtype=float)
    xy_data = (x_grid.ravel(), y_grid.ravel())
    z_data = roi_im.ravel()

    p0 = model.initial_guess(roi_im, roi_mask)
    bounds = model.bounds(roi_im, roi_mask)

    try:
        popt, pcov = curve_fit(
            model.model,
            xy_data,
            z_data,
            p0=p0,
            bounds=bounds,
            maxfev=10000,
        )
    except RuntimeError as e:
        raise ValueError(f"Fit failed to converge: {e}") from e

    fitted_values = model.model(xy_data, *popt).reshape(roi_im.shape)
    residuals = roi_im - fitted_values
    ss_res = float(np.sum(residuals**2))
    ss_tot = float(np.sum((roi_im - np.mean(roi_im)) ** 2))
    r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    perr = np.sqrt(np.diag(pcov))
    params_dict = {name: float(val) for name, val in zip(model.param_names, popt)}
    param_std_dict = {name: float(std) for name, std in zip(model.param_names, perr)}

    center_idx_x = model.param_names.index("x0")
    center_idx_y = model.param_names.index("y0")
    center_px_local = (popt[center_idx_y], popt[center_idx_x])
    center_px_full = (
        center_px_local[0] + roi_offset_y,
        center_px_local[1] + roi_offset_x,
    )

    summary = model.summary(params_dict)

    return FitResult(
        model=model_name,
        params=params_dict,
        param_std=param_std_dict,
        r_squared=float(r_squared),
        center_px=center_px_full,
        summary=summary,
    )
