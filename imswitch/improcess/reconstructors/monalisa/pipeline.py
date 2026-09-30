"""The lattice reconstruction pipeline: calibrate, extract, place.

One entry point for every illumination lattice and every scan, built from the
parts next to it. :func:`reconstruct_scan` does everything for the frames of
one scan; it consists of

1. :func:`prepare_geometry`, once per recording: detect the lattice, refine
   it on the measured focus centres, calibrate the spot model, build the
   extraction operator, and check the scan against the lattice.
2. The orientation of the scan, read from the data
   (:func:`.scan_frame.choose_orientation`) unless it is given.
3. The shift factor of the foci, measured on the data
   (:func:`.reassignment.measure_shift_factor`): whether the recording is one
   of confined foci, whose amplitudes are placed, or of foci wide enough for
   every pixel of a spot to carry its own image.
4. :func:`reconstruct_stack`, once per stack: extract and place, or reassign.

This is the two-stage estimator of
``docs/monalisa_optimal_reconstruction.md``, as amended by the first
recordings (section 9). It is not wired into the reconstructor plugin or the
live session; the scan is described by its step vectors and integer positions
in camera pixels, which the acquisition layout will supply.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

from .cell_offsets import fit_cell_offsets
from .frame_gain import fit_frame_gain
from .extraction import (
    DEFAULT_MIN_PIXELS,
    DEFAULT_REACH_SIGMA,
    ExtractionOperator,
    calibrate_haze_sigma,
)
from .lattice import Lattice, detect_lattice
from .placement import (
    COMMENSURATE_TOLERANCE,
    DEFAULT_SMOOTHING,
    Coverage,
    OutputRaster,
    PlacedImage,
    commensurability,
    coverage,
    lock_step_to_lattice,
    place,
    place_nearest,
    sample_positions,
)
from .reassignment import (
    PinholeStack,
    ShiftFactor,
    measure_shift_factor,
    pinhole_stack,
    reassign,
)
from .scan_frame import choose_orientation, raster_scan_index
from .sharpen import wiener_sharpen
from .spot_model import SpotModel, _default_radius, calibrate_spot_model, shared_sigma_fit

# A haze term is only used when it removes this share of the residual.
HAZE_RESIDUAL_RATIO = 0.8
# Below this shift factor a focus is confined: its amplitude is all it has.
CONFINED_SHIFT_FACTOR = 0.1


@dataclass(frozen=True)
class PipelineParams:
    """What remains to choose once the geometry is read from the data.

    Attributes:
        reach_sigma: Footprint radius in spot sigmas.
        background: ``"none"``, ``"constant"``, ``"constant+haze"``, or
            ``"auto"``: constant, plus haze when the mean frame calls for it.
        haze_sigma_px: Haze width; fitted to the mean frame when ``None``.
        joint: Fit all foci of a frame together. ``False`` is the isolated
            per-focus fit, for comparison.
        spot_mode: How the focus centres and widths are taken from the
            calibration image, see :func:`.spot_model.calibrate_spot_model`.
            ``"shared"``, the lattice and one width, is the default: on
            recordings of cells the centres and widths of single foci depend
            on the specimen, and the field fitted to them did not improve a
            reconstruction. ``"field"`` is for a recording of a uniform
            specimen, which measures the distortion.
        spot_sigma_px: Spot width override; measured when ``None``.
        period_band_px: ``(shortest, longest)`` period the lattice detection
            considers.
        lock_tolerance: The scan step is locked to the lattice when that
            changes it by no more than this fraction; 0 keeps the step.
        commensurate_tolerance: Samples within this distance of a raster
            pixel, in raster pixels, are placed without interpolation.
        smoothing: Roughness penalty of the gridding.
        reassignment: ``"auto"`` measures the shift factor and reassigns the
            pixels of the spots when the foci are not confined, ``"off"``
            places amplitudes, a number is the shift factor to reassign with.
        frame_gain: ``"smooth"`` divides every frame by its gain, fitted as
            a smooth function of the position in the scan
            (:mod:`.frame_gain`); ``"off"`` leaves the frames as they are.
        cell_offsets: ``"seams"`` takes the offset of every focus off its
            amplitudes, read from the borders of the cells
            (:mod:`.cell_offsets`); ``"off"`` does not.
        sharpen_sigma_px: When given, the result also holds the image
            sharpened by a Wiener filter that undoes a Gaussian blur of this
            many output pixels (:mod:`.sharpen`).
        sharpen_regularization: Noise over signal power of that filter.
        pinhole_stack: ``"raw"`` adds the raw frames assembled into one
            image per pixel of the footprint, each where its focus was;
            ``"shifted"`` the same with every image moved by the shift
            factor times its offset; ``"off"`` neither
            (:func:`.reassignment.pinhole_stack`). A check that needs no
            model, at the price of memory: some 70 images of the size of
            the reconstruction.
    """

    reach_sigma: float = DEFAULT_REACH_SIGMA
    background: str = "constant"
    haze_sigma_px: float | None = None
    joint: bool = True
    spot_mode: str = "shared"
    spot_sigma_px: float | None = None
    period_band_px: tuple[float, float] | None = None
    lock_tolerance: float = 0.02
    commensurate_tolerance: float = COMMENSURATE_TOLERANCE
    smoothing: float = DEFAULT_SMOOTHING
    reassignment: str | float = "auto"
    frame_gain: str = "smooth"
    cell_offsets: str = "seams"
    sharpen_sigma_px: float | None = None
    sharpen_regularization: float = 0.1
    pinhole_stack: str = "off"


@dataclass(frozen=True)
class StackGeometry:
    """Everything that is fixed for a recording."""

    lattice: Lattice
    spot_model: SpotModel
    operator: ExtractionOperator
    raster: OutputRaster
    step: np.ndarray
    index_matrix: np.ndarray
    commensurate_residual: float
    coverage: Coverage
    params: PipelineParams
    shift_factor: ShiftFactor | None = None
    diagnostics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StackReconstruction:
    """The image planes of one stack.

    ``amplitude`` is the reconstruction; ``background`` and ``haze`` are the
    other coefficients of the fit, placed the same way.

    ``amplitude.variance`` is the variance of each pixel for white noise of
    the level the fit's residual shows. Photon noise is larger under the
    spots than between them, so for photon-limited frames the plane is a
    lower bound (it was 1.6 times too low on the synthetic scans of the
    tests); it is present for placed images only.

    ``owner`` says for every pixel which focus measured it (-1 where none
    did): the cells of the image, for :func:`.quality.seam_contrast`.

    ``sharpened`` is ``amplitude.image`` after the sharpening filter, when
    one was asked for; the estimate itself is never replaced by it.
    """

    amplitude: PlacedImage
    background: PlacedImage | None
    haze: PlacedImage | None
    noise_sigma: np.ndarray
    owner: np.ndarray | None = None
    sharpened: np.ndarray | None = None
    pinholes: PinholeStack | None = None
    geometry: StackGeometry | None = None
    diagnostics: dict = field(default_factory=dict)


def prepare_geometry(
    mean_frame: np.ndarray,
    step: np.ndarray | None,
    scan_index: np.ndarray,
    params: PipelineParams | None = None,
    lattice: Lattice | None = None,
    modulation_frame: np.ndarray | None = None,
    pixel_mask: np.ndarray | None = None,
) -> StackGeometry:
    """Read the geometry of a recording from its frames and its scan.

    Args:
        mean_frame: Mean of the frames of one scan.
        step: 2x2, the camera-space vectors of one scan step along the
            camera's x and y, as columns. ``None`` when the pixel size is
            not known: the step is then the one with which the scan covers
            the cell of the lattice once, equal along both axes.
        scan_index: ``(frames, 2)`` integer scan positions in steps.
        params: Pipeline parameters.
        lattice: The illumination lattice, when already known; detected
            otherwise.
        modulation_frame: Temporal variance of the frames. When given, the
            lattice and the spots are measured on it instead of on the mean
            frame: it shows the foci without the static background.
        pixel_mask: ``(rows, cols)`` bool, ``False`` for camera pixels the
            extraction is to leave out.
    """
    params = params or PipelineParams()
    mean_frame = np.asarray(mean_frame, dtype=float)
    rows, cols = mean_frame.shape
    finding = find_foci(mean_frame, modulation_frame, params, lattice=lattice)
    calibration, lattice, first = finding.calibration, finding.lattice, finding.spot
    image = calibration.image
    modulation = calibration.name == "variance"
    lattice_residual = finding.lattice_residual_px

    margin = 3.0 * first.shared_sigma
    x, y = lattice.points_in_frame(rows, cols, margin=margin)
    spot_model = calibrate_spot_model(
        image, x, y, first.shared_sigma, mode=params.spot_mode, modulation=modulation
    )
    sigma = spot_model.sigma
    if params.spot_sigma_px is not None:
        sigma = np.full(x.shape, float(params.spot_sigma_px))

    background = params.background
    haze_sigma = params.haze_sigma_px
    haze_ratio = None
    if background in ("auto", "constant+haze") and haze_sigma is None:
        haze_sigma, haze_ratio = calibrate_haze_sigma(
            mean_frame, spot_model.x, spot_model.y, sigma,
            reach_sigma=params.reach_sigma,
        )
    if background == "auto":
        use_haze = haze_ratio is not None and haze_ratio <= HAZE_RESIDUAL_RATIO
        background = "constant+haze" if use_haze else "constant"

    operator = ExtractionOperator.build(
        spot_model.x, spot_model.y, sigma, (rows, cols),
        reach_sigma=params.reach_sigma,
        background=background,
        haze_sigma=haze_sigma if background == "constant+haze" else None,
        joint=params.joint,
        pixel_mask=pixel_mask,
        min_pixels=DEFAULT_MIN_PIXELS if pixel_mask is None else DEFAULT_MIN_PIXELS // 2,
    )

    scan_index = np.rint(np.asarray(scan_index)).astype(int).reshape(-1, 2)
    step_given = step is not None
    if not step_given:
        positions = np.unique(scan_index, axis=0).shape[0]
        pitch = float(np.sqrt(lattice.cell_area / positions))
        step = np.diag([pitch, pitch])
    step = np.asarray(step, dtype=float).reshape(2, 2)
    nominal_step = step
    lock_change = 0.0
    if params.lock_tolerance > 0:
        step, lock_change = lock_step_to_lattice(
            lattice.matrix, step, params.lock_tolerance
        )
    index_matrix, residual = commensurability(lattice.matrix, step)
    cell_coverage = coverage(index_matrix, scan_index)
    raster = OutputRaster.covering((rows, cols), step, lattice.offset)

    diagnostics = {
        "lattice": lattice.describe(),
        "lattice_fit_residual_px": lattice_residual,
        "calibration_image": calibration.name,
        "calibration_contrast": dict(calibration.contrast),
        "spot_sigma_px": spot_model.shared_sigma,
        "spot_model": dict(spot_model.diagnostics),
        "background": background,
        "haze_sigma_px": haze_sigma if background == "constant+haze" else None,
        "haze_residual_ratio": haze_ratio,
        "num_foci": int(operator.valid.sum()),
        "nominal_step_px": nominal_step.tolist(),
        "step_given": step_given,
        "step_px": step.tolist(),
        "step_lock_change": lock_change,
        "step_locked": bool(not np.array_equal(step, nominal_step)),
        "commensurate_residual": residual,
        "coverage_cell_pixels": cell_coverage.cell_pixels,
        "coverage_holes": cell_coverage.holes,
        "coverage_overlaps": cell_coverage.overlaps,
    }
    return StackGeometry(
        lattice=lattice,
        spot_model=spot_model,
        operator=operator,
        raster=raster,
        step=step,
        index_matrix=index_matrix,
        commensurate_residual=residual,
        coverage=cell_coverage,
        params=params,
        diagnostics=diagnostics,
    )


#: The variance frame is calibrated on unless the foci explain less than
#: this fraction of what they explain on the mean frame. On two dim
#: recordings the variance was shot noise (the foci explained 0.04 and 0.08
#: of it, against 0.25 and 0.42 of the mean frame); on six others the
#: variance kept 0.76 to 2.8 times the mean frame's contrast.
VARIANCE_CONTRAST_RATIO = 0.5


@dataclass(frozen=True)
class CalibrationImage:
    """The image the lattice and the spots are measured on, and why.

    ``contrast`` maps each candidate (``"variance"``, ``"mean"``) to the
    fraction of it the foci explain (:class:`.spot_model.SharedSigmaFit`),
    NaN where the lattice could not be found on it. ``sigma`` is the shared
    spot width of the chosen image, before the widening of a variance.
    """

    name: str
    image: np.ndarray
    lattice: Lattice
    sigma: float
    contrast: dict[str, float]


def choose_calibration_image(
    mean_frame: np.ndarray,
    modulation_frame: np.ndarray | None,
    params: PipelineParams | None = None,
    lattice: Lattice | None = None,
) -> CalibrationImage:
    """Pick the variance frame, or the mean frame when the variance is noise.

    The temporal variance shows the foci without the static background,
    and is preferred; but on a dim recording it is the shot noise of the
    mean, flat, and the lattice read from it and the widths fitted on it
    are not the foci's. Each candidate is scored by the fraction of it the
    foci explain, and the variance is kept unless it falls below
    :data:`VARIANCE_CONTRAST_RATIO` of the mean frame's.
    """
    params = params or PipelineParams()
    band = params.period_band_px or (3.0, None)
    mean_frame = np.asarray(mean_frame, dtype=float)
    candidates = []
    if modulation_frame is not None:
        candidates.append(("variance", np.asarray(modulation_frame, dtype=float)))
    candidates.append(("mean", mean_frame))

    scored = {}
    failure = None
    for name, image in candidates:
        try:
            found = lattice or detect_lattice(
                image, min_period=band[0], max_period=band[1]
            )
            x, y = found.points_in_frame(*image.shape)
            fit = shared_sigma_fit(image, x, y, radius=_default_radius(x, y, 2.0))
        except ValueError as exc:
            failure = failure or exc
            scored[name] = None
            continue
        scored[name] = (image, found, fit)
    contrast = {
        name: (np.nan if scored[name] is None else scored[name][2].explained)
        for name, _ in candidates
    }
    if all(value is None for value in scored.values()):
        raise failure
    chosen = "mean"
    if scored.get("variance") is not None and (
        scored["mean"] is None
        or contrast["variance"] >= VARIANCE_CONTRAST_RATIO * contrast["mean"]
    ):
        chosen = "variance"
    image, found, fit = scored[chosen]
    return CalibrationImage(chosen, image, found, fit.sigma, contrast)


@dataclass(frozen=True)
class FociFinding:
    """Where the foci are, read from the frames and nothing else.

    The first half of :func:`prepare_geometry`, and what the reconstructor's
    preview draws on the raw frames: the lattice, refined on the measured
    centres of the foci inside the frame, and the spot width.
    """

    calibration: CalibrationImage
    lattice: Lattice
    spot: SpotModel
    lattice_residual_px: float

    def points(self, num_rows: int, num_cols: int) -> tuple[np.ndarray, np.ndarray]:
        """The foci inside a frame of that size, ``(x, y)`` in camera pixels."""
        return self.lattice.points_in_frame(num_rows, num_cols)

    def describe(self) -> str:
        """One line for a status label or a log."""
        a1 = np.linalg.norm(self.lattice.a1)
        a2 = np.linalg.norm(self.lattice.a2)
        rows, cols = self.calibration.image.shape
        count = self.points(rows, cols)[0].size
        measured = self.spot.diagnostics.get("num_measured", 0)
        contrast = ", ".join(
            f"{name} {value:.2f}" for name, value in self.calibration.contrast.items()
            if np.isfinite(value)
        )
        return (
            f"{count} foci on a lattice of {a1:.2f} x {a2:.2f} px, "
            f"{measured} of them measured; spot sigma {self.spot.shared_sigma:.2f} px; "
            f"read from the {self.calibration.name} frame (foci contrast: {contrast})"
        )


def find_foci(
    mean_frame: np.ndarray,
    modulation_frame: np.ndarray | None = None,
    params: PipelineParams | None = None,
    lattice: Lattice | None = None,
) -> FociFinding:
    """Find the lattice of foci and their width on the frames.

    The image is chosen by :func:`choose_calibration_image`; the lattice is
    refined on the centres of the foci measured on it. A lattice finer than
    three spot widths is a harmonic of the pattern and refused.
    """
    params = params or PipelineParams()
    mean_frame = np.asarray(mean_frame, dtype=float)
    rows, cols = mean_frame.shape
    calibration = choose_calibration_image(
        mean_frame, modulation_frame, params, lattice=lattice
    )
    image, lattice = calibration.image, calibration.lattice
    modulation = calibration.name == "variance"

    # Refine the lattice on the measured centres of the foci inside the frame.
    inner_x, inner_y = lattice.points_in_frame(rows, cols)
    first = calibrate_spot_model(
        image, inner_x, inner_y, params.spot_sigma_px,
        mode="measured", modulation=modulation,
    )
    measured = first.measured if first.measured is not None else np.zeros(0, bool)
    lattice_residual = np.nan
    if measured.sum() >= 6:
        lattice, residual = lattice.fit_to_points(
            first.x[measured], first.y[measured]
        )
        lattice_residual = float(np.sqrt(np.mean(np.sum(residual**2, axis=1))))

    # Foci closer than three of their widths are not foci of their own: a
    # lattice that fine is a harmonic of the one that is there.
    spacing = lattice.nearest_spacing()
    if spacing < 3.0 * first.shared_sigma:
        raise ValueError(
            f"The lattice found has a period of {spacing:.2f} px, finer than "
            f"spots of sigma {first.shared_sigma:.2f} px allow: it is a "
            "harmonic of the pattern. Give the band of periods to look in "
            "(period_band_px), or a larger frame"
        )
    return FociFinding(calibration, lattice, first, lattice_residual)


def _raster_coordinates(geometry: StackGeometry, scan_index: np.ndarray):
    offsets = np.asarray(scan_index, dtype=float) @ geometry.step.T
    model = geometry.spot_model
    qx, qy = sample_positions(model.x, model.y, offsets[:, 0], offsets[:, 1])
    return geometry.raster.coordinates(qx, qy)


def _shift_factor_to_use(geometry: StackGeometry):
    """The shift factor to reassign with, or ``None`` to place amplitudes."""
    choice = geometry.params.reassignment
    if isinstance(choice, str):
        if choice == "off":
            return None
        if choice != "auto":
            raise ValueError(f"Unknown reassignment {choice!r}")
        factor = geometry.shift_factor
        if factor is None or factor.alpha < CONFINED_SHIFT_FACTOR:
            return None
        return (factor.alpha_x, factor.alpha_y)
    return float(choice)


def reconstruct_stack(
    frames: np.ndarray,
    geometry: StackGeometry,
    scan_index: np.ndarray,
    frame_gain: np.ndarray | None = None,
) -> StackReconstruction:
    """Reconstruct one stack: the frames of one scan of the cell.

    Args:
        frames: ``(frames, rows, cols)``.
        geometry: From :func:`prepare_geometry`.
        scan_index: ``(frames, 2)`` scan positions in steps; the sample is
            displaced by ``geometry.step @ scan_index[k]`` in frame ``k``.
        frame_gain: ``(frames,)`` gain of every frame; the amplitudes are
            divided by it.
    """
    frames = np.asarray(frames)
    scan_index = np.asarray(scan_index, dtype=float).reshape(-1, 2)
    if scan_index.shape[0] != frames.shape[0]:
        raise ValueError(
            f"{frames.shape[0]} frames but {scan_index.shape[0]} scan positions"
        )
    operator = geometry.operator
    extracted = operator.apply(frames)
    gx, gy = _raster_coordinates(geometry, scan_index)
    valid = operator.valid
    gx, gy = gx[:, valid], gy[:, valid]

    gain = np.ones(frames.shape[0])
    if frame_gain is not None:
        gain = np.asarray(frame_gain, dtype=float).reshape(frames.shape[0])
    amplitudes = extracted.amplitude / gain[:, None]
    variance = (
        (extracted.noise_sigma / gain)[:, None] ** 2
        * operator.variance_gain[None, valid]
    )
    variance = np.where(variance > 0, variance, np.nan)
    if not np.all(np.isfinite(variance)):
        variance = None

    def placed(values: np.ndarray | None, with_variance: bool) -> PlacedImage | None:
        if values is None:
            return None
        return place(
            gx, gy, values[:, valid], geometry.raster.shape,
            variance=variance if with_variance else None,
            tolerance=geometry.params.commensurate_tolerance,
            smoothing=geometry.params.smoothing,
        )

    focus = np.broadcast_to(np.flatnonzero(valid)[None, :], gx.shape)
    owner = place_nearest(gx, gy, focus, geometry.raster.shape).image
    owner = np.where(np.isfinite(owner), owner, -1).astype(np.int64)
    offsets, offset_share = None, None
    if geometry.params.cell_offsets == "seams":
        cells = place_nearest(gx, gy, amplitudes[:, valid], geometry.raster.shape)
        offsets, offset_share = fit_cell_offsets(
            cells.image, owner, operator.num_foci, return_share=True
        )
        amplitudes = amplitudes - offsets[None, :]
    elif geometry.params.cell_offsets != "off":
        raise ValueError(f"Unknown cell offsets {geometry.params.cell_offsets!r}")

    alpha = _shift_factor_to_use(geometry)
    if alpha is None:
        amplitude = placed(amplitudes, True)
    else:
        model = geometry.spot_model
        amplitude = reassign(
            frames, operator, model.x, model.y,
            scan_index @ geometry.step.T, geometry.raster, alpha,
            frame_gain=gain, focus_offset=offsets,
        )
    pinholes = None
    if geometry.params.pinhole_stack != "off":
        if geometry.params.pinhole_stack not in ("raw", "shifted"):
            raise ValueError(
                f"Unknown pinhole stack {geometry.params.pinhole_stack!r}"
            )
        model = geometry.spot_model
        factor = geometry.shift_factor
        shift = None
        if geometry.params.pinhole_stack == "shifted":
            shift = alpha if alpha is not None else (
                (factor.alpha_x, factor.alpha_y) if factor is not None else 0.0
            )
        pinholes = pinhole_stack(
            frames, model.x[valid], model.y[valid],
            scan_index @ geometry.step.T, geometry.raster,
            radius=geometry.params.reach_sigma * model.shared_sigma,
            alpha=shift,
        )

    sharpened = None
    if geometry.params.sharpen_sigma_px:
        sharpened = wiener_sharpen(
            amplitude.image, geometry.params.sharpen_sigma_px,
            geometry.params.sharpen_regularization,
        )
    diagnostics = {
        "placement": amplitude.method,
        "shift_factor_used": alpha,
        "cell_offset_rms": None if offsets is None else float(np.std(offsets[valid])),
        "cell_offset_share": offset_share,
        "max_placement_residual": amplitude.max_residual,
        "holes": int(np.sum(~np.isfinite(amplitude.image))),
        "noise_sigma_mean": float(np.mean(extracted.noise_sigma)),
    }
    return StackReconstruction(
        amplitude=amplitude,
        background=placed(extracted.background, False),
        haze=placed(extracted.haze, False),
        noise_sigma=extracted.noise_sigma,
        owner=owner,
        sharpened=sharpened,
        pinholes=pinholes,
        geometry=geometry,
        diagnostics=diagnostics,
    )


def reconstruct_scan(
    frames: np.ndarray,
    step: np.ndarray | None,
    scan_shape: tuple[int, int],
    params: PipelineParams | None = None,
    orientation: str | None = None,
    lattice: Lattice | None = None,
    pixel_mask: np.ndarray | None = None,
    shift_factor: ShiftFactor | None = None,
    geometry: StackGeometry | None = None,
) -> StackReconstruction:
    """Reconstruct the frames of one raster scan, reading all it can from them.

    Args:
        frames: ``(num_fast * num_slow, rows, cols)`` in the order recorded.
        step: 2x2, the nominal camera-space vectors of one scan step along
            the camera's x and y, as columns: for a stage along the camera
            axes ``diag(step / pixel size)``. ``None`` when the pixel size
            is not known (see :func:`prepare_geometry`).
        scan_shape: ``(num_fast, num_slow)``.
        params: Pipeline parameters.
        orientation: Fast axis and directions, e.g. ``"-x+y"`` (see
            :func:`.scan_frame.raster_scan_index`); read from the data when
            ``None``.
        lattice: The illumination lattice, when already known.
        pixel_mask: ``(rows, cols)`` bool, ``False`` for camera pixels to
            leave out: hot pixels, or one half of a split for
            :mod:`.quality`.
        shift_factor: The shift factor, when already measured.
        geometry: The geometry of an earlier scan of the same recording
            (``result.geometry``). The scans of a time lapse are then
            reconstructed on one raster with one lattice, spot model and
            shift factor; only the gain of the frames and the offsets of the
            cells are read from every scan anew. ``params`` are taken from
            it.
    """
    params = params or PipelineParams()
    frames = np.asarray(frames)
    num_fast, num_slow = int(scan_shape[0]), int(scan_shape[1])
    if frames.shape[0] != num_fast * num_slow:
        raise ValueError(
            f"{frames.shape[0]} frames do not make a scan of "
            f"{num_fast} x {num_slow} steps"
        )
    if geometry is not None:
        params = geometry.params
        if shift_factor is None:
            shift_factor = geometry.shift_factor
    else:
        mean_frame = frames.mean(axis=0, dtype=np.float64)
        modulation_frame = frames.var(axis=0, dtype=np.float64)
        first_index = raster_scan_index(num_fast, num_slow, orientation or "+x+y")
        geometry = prepare_geometry(
            mean_frame, step, first_index, params,
            lattice=lattice, modulation_frame=modulation_frame,
            pixel_mask=pixel_mask,
        )
    operator = geometry.operator
    valid = operator.valid
    diagnostics = {}

    amplitude = operator.apply(frames).amplitude[:, valid]
    gain = None
    if params.frame_gain == "smooth":
        fitted = fit_frame_gain(amplitude, (num_fast, num_slow))
        gain = fitted.gain
        amplitude = amplitude / gain[:, None]
        diagnostics["frame_gain_rms"] = fitted.rms
        diagnostics["frame_gain_residual_rms"] = fitted.residual_rms
        diagnostics["frame_gain_range"] = (float(gain.min()), float(gain.max()))
    elif params.frame_gain != "off":
        raise ValueError(f"Unknown frame gain {params.frame_gain!r}")

    if orientation is None:

        def assemble(scan_index: np.ndarray) -> np.ndarray:
            gx, gy = _raster_coordinates(geometry, scan_index)
            return place(
                gx[:, valid], gy[:, valid], amplitude, geometry.raster.shape,
                tolerance=np.inf,
            ).image

        orientation, scores = choose_orientation(assemble, num_fast, num_slow)
        ranked = sorted(score for score in scores.values() if np.isfinite(score))
        diagnostics["orientation_scores"] = scores
        diagnostics["orientation_margin"] = (
            float((ranked[1] - ranked[0]) / max(ranked[-1] - ranked[0], 1e-30))
            if len(ranked) > 1 else 0.0
        )
    scan_index = raster_scan_index(num_fast, num_slow, orientation)
    geometry = replace(
        geometry, coverage=coverage(geometry.index_matrix, scan_index)
    )
    diagnostics["orientation"] = orientation

    if shift_factor is not None:
        geometry = replace(geometry, shift_factor=shift_factor)
        diagnostics["shift_factor"] = shift_factor.alpha
    elif params.reassignment == "auto" or params.pinhole_stack == "shifted":
        model = geometry.spot_model
        try:
            factor = measure_shift_factor(
                frames, model.x, model.y, scan_index @ geometry.step.T,
                geometry.raster,
            )
        except ValueError:
            factor = None
        geometry = replace(geometry, shift_factor=factor)
        if factor is not None:
            diagnostics["shift_factor"] = factor.alpha
            diagnostics["shift_factor_xy"] = (factor.alpha_x, factor.alpha_y)
            diagnostics["shift_factor_spread"] = factor.spread

    result = reconstruct_stack(frames, geometry, scan_index, frame_gain=gain)
    geometry_diagnostics = dict(geometry.diagnostics)
    geometry_diagnostics.update(
        coverage_holes=geometry.coverage.holes,
        coverage_overlaps=geometry.coverage.overlaps,
    )
    diagnostics.update(result.diagnostics)
    return replace(
        result,
        geometry=replace(geometry, diagnostics=geometry_diagnostics),
        diagnostics=diagnostics,
    )


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
