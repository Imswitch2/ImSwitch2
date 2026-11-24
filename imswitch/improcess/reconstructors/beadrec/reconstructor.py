"""BeadRec reconstructor — offline raster reconstruction of a bead scan.

Reconstructs a bead image from a recorded camera frame stream (one scan position
per frame; each pixel = the mean intensity in a detection ROI of that frame),
optionally fitting one of the shared Gaussian, donut, exponential, or periodic
models. Any two scan axes give an image (XY, XZ, YZ) and all three a volume.
Reuses the hardware-agnostic bead algorithms now in
:mod:`imswitch.imcommon.algorithms.bead_recognition` (moved out of imcontrol so
ImProcess can use them without a cross-module import).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from qtpy import QtWidgets
from skimage.transform import rescale

from imswitch.imcommon.algorithms.bead_recognition import (
    RoiBounds,
    fit_bead,
    mean_intensity_in_roi,
    normalize_roi_bounds,
)
from imswitch.imcommon.algorithms.bead_fits import FIT_MODELS
from imswitch.imcommon.model import initLogger
from imswitch.imcommon.model.acquisition_layout import (
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLoop,
    UnconsumedLoopError,
    iter_physical_coordinates,
    select_loops,
)
from imswitch.improcess.model.core_dims import WHOLE_INPUT
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.reconstructors.base import (
    AcquisitionRequirements,
    Reconstructor,
)

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj

_FIT_CHOICES = ("none", *FIT_MODELS.keys())


def _as_frame_stack(data) -> np.ndarray:
    """Collapse an N-D recording to a ``(frames, Y, X)`` stack."""
    arr = np.asarray(data)
    if arr.ndim < 3:
        raise ValueError("BeadRec needs a frame stack (>=3D: …, Y, X)")
    return arr.reshape(-1, arr.shape[-2], arr.shape[-1])


def infer_scan_dims(n_frames, scan_x=0, scan_y=0):
    """Resolve ``(x_pixels, y_pixels)`` from the two manual entries.

    Either entry alone determines the other exactly. With neither, the raster
    is genuinely unknown: guessing ``sqrt(n_frames)`` silently turned a
    648-frame 18x18x2 line-step scan into a 25x25 raster and dropped the last
    23 frames, so an unknown raster is now an error the caller must report
    rather than a shape this function invents.
    """
    scan_x, scan_y = int(scan_x), int(scan_y)
    if scan_x > 0 and scan_y > 0:
        return scan_x, scan_y
    if scan_x > 0:
        return scan_x, max(1, n_frames // scan_x)
    if scan_y > 0:
        return max(1, n_frames // scan_y), scan_y
    return None


#: The scan axes BeadRec places, in image order, outermost first. A volume is
#: ``(Z, Y, X)``, and a section keeps the same priority: an XZ scan has Z down
#: the rows and X along the columns, as an XY scan has Y down the rows.
_SCAN_AXES = (("Z", "scan_z"), ("Y", "scan_y"), ("X", "scan_x"))

#: BeadRec places every scan axis and splits conditions into separate images.
#: Every other loop -- a time lapse, a repeat, a tile -- is a dimension it has
#: no image for, and is refused rather than folded into the same raster.
_BEADREC_ROLES = {**dict(_SCAN_AXES), "condition": "condition"}


@dataclass(frozen=True)
class RasterAxes:
    """The scan loops one BeadRec image is built from, in image order.

    ``labels`` and ``loops`` run outermost first, so ``shape`` is the image
    shape. ``scan_dims`` and ``step_sizes`` run the other way, column first,
    which is BeadRec's long-standing ``(x_pixels, y_pixels)`` convention
    extended by a third entry for a volume.
    """

    labels: tuple[str, ...]
    loops: tuple[AcquisitionLoop, ...]
    condition: AcquisitionLoop | None = None

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(int(loop.count) for loop in self.loops)

    @property
    def scan_dims(self) -> tuple[int, ...]:
        return tuple(reversed(self.shape))

    @property
    def step_sizes(self) -> tuple[float, ...]:
        """Physical step per axis, column first; ``0.0`` where none was recorded."""
        return tuple(
            float(loop.step) if loop.step else 0.0 for loop in reversed(self.loops)
        )


def _select_raster_axes(
    layout: AcquisitionLayout, *, authoritative: bool = True
) -> RasterAxes | None:
    """The scan axes and condition loop, or ``None`` when this is not a raster.

    Any two of X, Y and Z make an image and all three a volume. A one-step
    scan axis is a placeholder rather than a dimension -- a designer may emit
    Z with a single plane for an XY scan -- so it is dropped while that still
    leaves two axes. A declared layout with a loop BeadRec cannot place is an
    error; an inferred one is declined so the manual entries stay in charge.
    """
    try:
        selected = select_loops(layout, consumer="BeadRec", roles=_BEADREC_ROLES)
    except UnconsumedLoopError as error:
        if authoritative:
            raise ValueError(str(error)) from error
        return None
    present = [
        (label, selected[label]) for label, _kind in _SCAN_AXES
        if selected[label] is not None
    ]
    while len(present) > 2:
        singleton = next(
            (i for i, (_label, loop) in enumerate(present) if int(loop.count) == 1),
            None,
        )
        if singleton is None:
            break
        present.pop(singleton)
    if len(present) < 2:
        return None
    return RasterAxes(
        labels=tuple(label for label, _loop in present),
        loops=tuple(loop for _label, loop in present),
        condition=selected["condition"],
    )


def raster_axes_from_layout(
    layout: AcquisitionLayout | None, *, authoritative: bool = True
) -> RasterAxes | None:
    """The :class:`RasterAxes` a scan recorded, or ``None`` for no raster.

    ``None`` leaves the manual entries authoritative.
    """
    if layout is None or layout.payload_kind != PAYLOAD_DETECTOR_FRAME_STREAM:
        return None
    return _select_raster_axes(layout, authoritative=authoritative)


def raster_geometry_from_layout(
    layout: AcquisitionLayout | None, *, authoritative: bool = True
):
    """Return ``(scan_dims, step_sizes, condition_loop)`` recorded by a scan.

    Both tuples are column first (see :class:`RasterAxes`). ``None`` when the
    layout does not describe a 2D or 3D raster, which leaves the manual
    entries authoritative.
    """
    axes = raster_axes_from_layout(layout, authoritative=authoritative)
    if axes is None:
        return None
    return axes.scan_dims, axes.step_sizes, axes.condition


def _condition_labels(loop) -> tuple[str, ...]:
    if loop is None:
        return ()
    if loop.labels:
        return tuple(loop.labels)
    return tuple(f"condition_{index}" for index in range(loop.count))


def raster_positions_from_layout(
    layout: AcquisitionLayout, scan_dims=None
) -> list[tuple[int, int]]:
    """Map each stored frame to its ``(condition, raster index)`` slot.

    The raster index is the frame's flat, row-major position in the image
    (or volume) that :class:`RasterAxes` describes. Placement follows the
    resolved coordinates rather than arrival order, so serpentine traversal,
    a detector recorded for only some conditions, and per-expanded-line
    enable masks all land in the right pixel without this plugin repeating
    the index arithmetic. Coordinates are physically oriented -- a negative
    stage direction mirrors that axis -- by the one imcommon helper every
    image-building consumer shares, so this plugin and MoNaLISA cannot
    disagree about the same file.
    """
    axes = _select_raster_axes(layout, authoritative=True)
    if axes is None:
        raise ValueError(
            "BeadRec raster positions need at least two of the scan_x, "
            "scan_y and scan_z loops"
        )
    if scan_dims is not None and tuple(int(d) for d in scan_dims) != axes.scan_dims:
        raise ValueError(
            f"BeadRec was given scan dims {tuple(scan_dims)}, but the layout "
            f"describes {axes.scan_dims}"
        )
    condition = axes.condition
    positions = []
    for coordinates in iter_physical_coordinates(layout):
        index = 0
        for loop in axes.loops:
            index = index * int(loop.count) + coordinates[loop.id]
        positions.append(
            (coordinates[condition.id] if condition is not None else 0, index)
        )
    return positions


def _raster_text(scan_dims) -> str:
    return "x".join(str(int(d)) for d in scan_dims)


def _rescale_to_square_pixels(image: np.ndarray, step_sizes) -> np.ndarray:
    """Resample every axis to the finest scan step, so pixels are square.

    ``step_sizes`` is column first, like ``scan_dims``. The image is returned
    unchanged when a step is missing or not positive, or all steps agree.
    This is the N-D form of ``rescale_reconstruction_to_pixel_size``, which
    the live BeadRec uses for its XY image, with the same resampling.
    """
    if step_sizes is None or len(step_sizes) < image.ndim:
        return image
    steps = tuple(float(step) for step in reversed(tuple(step_sizes)[: image.ndim]))
    if any(step <= 0 for step in steps):
        return image
    reference = min(steps)
    if all(np.isclose(step, reference) for step in steps):
        return image
    return rescale(
        image,
        tuple(step / reference for step in steps),
        anti_aliasing=False,
        mode="reflect",
        preserve_range=True,
    )


def reconstruct_bead_image(
    frames, scan_dims, roi_bounds=None, step_sizes=None, raster_indices=None
):
    """Raster-reconstruct a bead image (or volume) from a frame stack.

    ``frames``: ``(N, Y, X)`` camera frames; ``scan_dims``: pixels per scan
    axis, column first -- ``(x_pixels, y_pixels)`` for an image,
    ``(x_pixels, y_pixels, planes)`` for a volume; ``roi_bounds``:
    ``(x0, y0, x1, y1)`` detection ROI or ``None`` (full frame);
    ``step_sizes``: physical steps in the order of ``scan_dims`` for the
    anisotropic-pixel rescale, or ``None``; ``raster_indices``: flat
    destination pixel per frame, from the resolved acquisition layout, or
    ``None`` to fill the raster in arrival order.
    """
    frames = np.asarray(frames)
    if frames.ndim != 3:
        raise ValueError("BeadRec expects a 3D frame stack (N, Y, X)")
    dims = tuple(int(d) for d in scan_dims)
    if len(dims) not in (2, 3) or min(dims) < 1:
        raise ValueError(
            f"BeadRec scan dims must be two or three positive counts, got {scan_dims}"
        )
    _, height, width = frames.shape
    roi = (RoiBounds(0, 0, width, height) if roi_bounds is None
           else normalize_roi_bounds(roi_bounds, (height, width)))

    buffer = np.zeros(int(np.prod(dims)), dtype=float)
    if raster_indices is None:
        # Arrival order fills the raster exactly once; an over-long stack
        # would otherwise lose its tail without a word.
        if frames.shape[0] != buffer.size:
            raise ValueError(
                f"BeadRec needs exactly {buffer.size} frames for a "
                f"{_raster_text(dims)} raster, got {frames.shape[0]}"
            )
        raster_indices = range(buffer.size)
    elif len(raster_indices) != frames.shape[0]:
        raise ValueError(
            f"BeadRec received {len(raster_indices)} raster positions for "
            f"{frames.shape[0]} frames"
        )
    for frame, index in zip(frames, raster_indices):
        buffer[index] = mean_intensity_in_roi(np.asarray(frame), roi)
    image = _rescale_to_square_pixels(buffer.reshape(dims[::-1]), step_sizes)
    return np.asarray(image, dtype=np.float32)


def _fit_entry(image: np.ndarray, model: str, axes: tuple[str, ...]) -> dict:
    """One fit as result metadata; ``{"error": ...}`` when it fails.

    ``axes`` names the image's rows and columns: the models' ``x0`` and
    ``sigma_x`` follow the columns and ``y0`` and ``sigma_y`` the rows, so an
    XZ section reports its axial width as ``sigma_y``.
    """
    try:
        fit = fit_bead(image, model)
    except Exception as exc:  # a bad/flat image must not crash the recon
        return {"error": str(exc)}
    return {
        "model": fit.model, "r_squared": fit.r_squared,
        "center_px": tuple(fit.center_px), "axes": tuple(axes), **fit.params,
    }


def _bead_centre_voxel(volume: np.ndarray) -> tuple[int, ...]:
    """The voxel nearest the background-subtracted intensity centroid.

    The centroid rather than the brightest voxel: a donut's brightest voxel
    sits on its ring, and a section through it would miss the centre.
    """
    weights = np.clip(volume - np.median(volume), 0.0, None)
    total = float(weights.sum())
    if not np.isfinite(total) or total <= 0:
        return tuple(size // 2 for size in volume.shape)
    centre = []
    for axis, size in enumerate(volume.shape):
        others = tuple(other for other in range(volume.ndim) if other != axis)
        profile = weights.sum(axis=others)
        centre.append(int(round(float(np.dot(np.arange(size), profile)) / total)))
    return tuple(centre)


def _fit_volume_sections(volume: np.ndarray, model: str, labels) -> dict:
    """Fit the XY, XZ and YZ sections through the bead centre of a volume.

    The models are 2D, so a volume is fitted as its three orthogonal
    sections; each entry records which plane it was cut at.
    """
    centre = _bead_centre_voxel(volume)
    sections = {}
    for axis, label in enumerate(labels):
        kept = tuple(other for i, other in enumerate(labels) if i != axis)
        entry = _fit_entry(np.take(volume, centre[axis], axis=axis), model, kept)
        entry["section"] = {label: centre[axis]}
        sections["".join(reversed(kept))] = entry
    return sections


class _BeadRecParamWidget(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        form = QtWidgets.QFormLayout(self)
        self.scan_x = QtWidgets.QSpinBox(); self.scan_x.setRange(0, 100000)
        self.scan_y = QtWidgets.QSpinBox(); self.scan_y.setRange(0, 100000)
        self.full_frame = QtWidgets.QCheckBox("Full frame"); self.full_frame.setChecked(True)
        self.roi = {k: QtWidgets.QSpinBox() for k in ("x0", "y0", "x1", "y1")}
        for box in self.roi.values():
            box.setRange(0, 100000)
        self.step_x = QtWidgets.QDoubleSpinBox(); self.step_x.setRange(0.0, 1e6); self.step_x.setValue(1.0)
        self.step_y = QtWidgets.QDoubleSpinBox(); self.step_y.setRange(0.0, 1e6); self.step_y.setValue(1.0)
        self.fit_model = QtWidgets.QComboBox(); self.fit_model.addItems(_FIT_CHOICES)

        form.addRow("Scan X pixels (0=auto)", self.scan_x)
        form.addRow("Scan Y pixels (0=auto)", self.scan_y)
        form.addRow(self.full_frame)
        for k, box in self.roi.items():
            form.addRow(f"ROI {k}", box)
        form.addRow("Step X", self.step_x)
        form.addRow("Step Y", self.step_y)
        form.addRow("Fit model", self.fit_model)

        def _toggle_roi():
            for box in self.roi.values():
                box.setEnabled(not self.full_frame.isChecked())
        self.full_frame.toggled.connect(lambda _c: _toggle_roi())
        _toggle_roi()

    def get_values(self) -> dict:
        roi = None if self.full_frame.isChecked() else (
            self.roi["x0"].value(), self.roi["y0"].value(),
            self.roi["x1"].value(), self.roi["y1"].value(),
        )
        return {
            "scan_x": self.scan_x.value(),
            "scan_y": self.scan_y.value(),
            "roi": roi,
            "step_x": float(self.step_x.value()),
            "step_y": float(self.step_y.value()),
            "fit_model": self.fit_model.currentText(),
        }


class BeadRecReconstructor(Reconstructor):
    """Raster-reconstruct a recorded bead scan, with an optional model fit."""

    name = "Bead reconstruction"
    id = "beadrec"

    #: Collapses every axis in front of (Y, X) into one frame list itself
    #: (scan axes, not time), so it takes the whole input in one call.
    core_dims_in = WHOLE_INPUT
    output_kind = "image"
    file_extensions = ["hdf5", "tiff", "tif", "zarr"]
    description = (
        "Raster-reconstruct a recorded bead scan, XY/XZ/YZ or XYZ (+ optional fit)"
    )
    default_save_subdir = "beadrec"

    #: A raster of camera frames. Extra loops are kept rather than rejected:
    #: a condition loop is reconstructed per condition instead of being mixed
    #: into one raster. ``allow_ambiguous`` is on because the manual Scan X/Y
    #: entries are a legitimate override for a recording without a layout.
    # The scan loops' physical steps ARE the bead fit's pixel size; a layout
    # whose loop has no step was read as 0.0 and the fit reported nonsense in
    # calibrated units. Declaring them here makes the preflight's
    # UNCALIBRATED_ACQUISITION_LOOP refusal live where it matters.
    acquisition_requirements = AcquisitionRequirements(
        payload_kinds=frozenset({PAYLOAD_DETECTOR_FRAME_STREAM}),
        allowed_extra_loops="split",
        allow_ambiguous=True,
        requires_calibrated_loops=frozenset({"scan_x", "scan_y", "scan_z"}),
    )

    @classmethod
    def default_params(cls) -> dict:
        return {   'scan_x': 0,
        'scan_y': 0,
        'roi': None,
        'step_x': 1.0,
        'step_y': 1.0,
        'fit_model': 'none'}

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField('scan_x', 'int', 0, label='Scan X pixels (0=auto)', min=0, max=100000),
            ParamField('scan_y', 'int', 0, label='Scan Y pixels (0=auto)', min=0, max=100000),
            ParamField(
                'roi', 'json', None, label='ROI',
                help='[x0, y0, x1, y1] in pixels to reconstruct; empty for the full frame',
                nullable=True,
            ),
            ParamField('step_x', 'float', 1.0, label='Step X', min=0, max=1000000, decimals=2),
            ParamField('step_y', 'float', 1.0, label='Step Y', min=0, max=1000000, decimals=2),
            ParamField(
                'fit_model', 'select', 'none', label='Fit model',
                options=('none', 'gaussian2d', 'donut_r2_gaussian', 'exponential2d', 'sine1d', 'sine2d'),
            ),
        )

    def make_param_widget(self, parent):
        return _BeadRecParamWidget(parent)

    def make_metadata_dialog(self, parent):
        return None

    @staticmethod
    def _resolved_layout(data_obj):
        """The resolved layout, or ``None`` when the source cannot supply one."""
        try:
            resolved = data_obj.acquisition_layout
        except Exception:
            return None
        if resolved is None or not resolved.is_usable:
            return None
        return resolved

    def process(
        self, data_obj: "DataObj", params: dict, context=None
    ) -> ArrayProcessingResult:
        self.validate_source(data_obj)
        frames = _as_frame_stack(data_obj.data)
        n_frames = int(frames.shape[0])

        resolved = self._resolved_layout(data_obj)
        layout = resolved.layout if resolved is not None else None
        authoritative = bool(resolved is not None and resolved.is_authoritative)
        recorded = raster_axes_from_layout(layout, authoritative=authoritative)
        manual_dims = infer_scan_dims(
            n_frames, params.get("scan_x", 0), params.get("scan_y", 0)
        )
        if recorded is not None and not authoritative and manual_dims is not None:
            # An inferred layout is a reading of the file, not a statement by
            # the acquisition, so it may not overrule what the user typed. It
            # used to: a legacy 4x3 inference refused a valid manual 6x2 on the
            # same twelve frames, which is exactly the "inference must never
            # refuse previously usable data" rule the contract sets out.
            if tuple(manual_dims) != recorded.scan_dims:
                initLogger(self).info(
                    f"Using the manual {_raster_text(manual_dims)} raster over "
                    f"the {_raster_text(recorded.scan_dims)} inferred from "
                    f"legacy metadata; nothing in this file declares its scan."
                )
                recorded = None

        if recorded is not None:
            # The recording is authoritative. A stale spinbox must not quietly
            # reshape a scan whose geometry was recorded; the supported way to
            # correct a wrong layout is a persisted layout override.
            scan_dims, steps, condition = (
                recorded.scan_dims, recorded.step_sizes, recorded.condition
            )
            image_axes = recorded.labels
            geometry_source = "layout"
            if manual_dims is not None and tuple(manual_dims) != scan_dims:
                raise ValueError(
                    f"Scan X/Y is set to {_raster_text(manual_dims)}, but this "
                    f"recording declares a {''.join(reversed(image_axes))} scan "
                    f"of {_raster_text(scan_dims)}. Clear the manual entries to "
                    f"use the recorded geometry, or persist a layout override "
                    f"if the recording is wrong."
                )
        elif manual_dims is not None:
            scan_dims, steps, condition = manual_dims, None, None
            image_axes = ("Y", "X")
            geometry_source = "manual"
        else:
            reason = (
                "its acquisition layout does not describe a scan over two or "
                "three of X, Y and Z"
                if layout is not None
                else "this source carries no acquisition layout"
            )
            raise ValueError(
                f"BeadRec cannot determine the raster: {reason}, and neither "
                f"Scan X nor Scan Y pixels were set. Enter the scan size, or "
                f"open a recording that carries its acquisition layout."
            )

        roi = params.get("roi")
        if steps and all(step > 0 for step in steps):
            step_sizes = steps
        else:
            # The manual steps follow the image's columns and rows; a volume
            # without recorded steps is left at one voxel per scan position.
            step_sizes = (params.get("step_x", 1.0), params.get("step_y", 1.0))

        pixels = int(np.prod(scan_dims))
        metadata = {
            "scan_dims": scan_dims,
            "scan_axes": tuple(image_axes),
            "n_frames": n_frames,
            "geometry_source": geometry_source,
        }

        if geometry_source == "layout":
            slots = raster_positions_from_layout(layout, scan_dims)
            if n_frames != len(slots):
                if not resolved.is_authoritative:
                    # An inferred layout that disagrees with the data is a bad
                    # inference, not bad data. Refusing on it would fail a file
                    # that used to open, so fall back to the manual entries.
                    raise ValueError(
                        f"The acquisition layout inferred for this source "
                        f"describes {len(slots)} frames but it has {n_frames}. "
                        f"Set Scan X/Y pixels to reconstruct it anyway, or "
                        f"persist a layout override."
                    )
                raise ValueError(
                    f"BeadRec needs exactly {len(slots)} frames for the "
                    f"recorded layout, but this source has {n_frames}. Frames "
                    f"are never padded or discarded to force a fit."
                )
            # Which conditions this detector actually recorded, not which ones
            # the producer ran: a detector gated to one line step reconstructs
            # that one condition from all of its frames.
            grouped: dict[int, tuple[list, list]] = {}
            for frame, (condition_index, raster_index) in zip(frames, slots):
                indices, condition_frames = grouped.setdefault(
                    condition_index, ([], [])
                )
                indices.append(raster_index)
                condition_frames.append(frame)
            present = sorted(grouped)
            incomplete = [
                index for index in present if len(grouped[index][0]) != pixels
            ]
            if incomplete:
                raise ValueError(
                    f"BeadRec needs a complete {_raster_text(scan_dims)} "
                    f"raster per condition, but condition(s) "
                    f"{incomplete} cover only part of the scan. A row-dependent "
                    f"detector gate is not a dense raster."
                )
            images = [
                reconstruct_bead_image(
                    np.asarray(grouped[index][1]),
                    scan_dims,
                    roi,
                    step_sizes,
                    raster_indices=grouped[index][0],
                )
                for index in present
            ]
            labels = _condition_labels(condition)
            metadata["condition_labels"] = tuple(
                labels[index] if index < len(labels) else f"condition_{index}"
                for index in present
            )
        else:
            if n_frames != pixels:
                raise ValueError(
                    f"BeadRec needs exactly {pixels} frames for a "
                    f"{_raster_text(scan_dims)} raster, but this source "
                    f"has {n_frames}. Frames are never padded or discarded to "
                    f"force a fit."
                )
            images = [reconstruct_bead_image(frames, scan_dims, roi, step_sizes)]

        if len(images) > 1:
            # Conditions stay a separate axis; mixing them into one raster
            # averages two different illumination states into each pixel.
            image = np.stack(images)
            axis_labels = ["Condition", *image_axes]
        else:
            # One image, but the label still says which condition it is.
            image = images[0]
            axis_labels = list(image_axes)
        fit_target = images[0]

        fit_model = params.get("fit_model", "none")
        if fit_model and fit_model != "none":
            if fit_target.ndim == 2:
                fit = _fit_entry(fit_target, fit_model, image_axes)
                if "error" in fit:
                    metadata["fit_error"] = fit["error"]
                else:
                    metadata["fit"] = fit
            else:
                # The models are 2D: a volume is fitted section by section.
                metadata["fit_sections"] = _fit_volume_sections(
                    fit_target, fit_model, image_axes
                )

        name = getattr(data_obj, "name", None) or "bead"
        return ArrayProcessingResult(
            name=f"{name} (beadrec)",
            data=image,
            axis_labels=axis_labels,
            metadata=metadata,
        )
