"""BeadRec reconstructor — offline raster reconstruction of a bead scan.

Reconstructs a bead image from a recorded camera frame stream (one scan position
per frame; each pixel = the mean intensity in a detection ROI of that frame),
optionally fitting one of the shared Gaussian, donut, exponential, or periodic
models. Reuses the hardware-agnostic bead algorithms now in
:mod:`imswitch.imcommon.algorithms.bead_recognition` (moved out of imcontrol so
ImProcess can use them without a cross-module import).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.algorithms.bead_recognition import (
    RoiBounds,
    append_roi_means,
    create_reconstruction_buffer,
    fit_bead,
    mean_intensity_in_roi,
    normalize_roi_bounds,
    reconstruction_image,
    rescale_reconstruction_to_pixel_size,
)
from imswitch.imcommon.algorithms.bead_fits import FIT_MODELS
from imswitch.imcommon.model.acquisition_layout import (
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    iter_recorded_coordinates,
)
from imswitch.improcess.model.array_result import ArrayProcessingResult
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


def _scan_loops(layout: AcquisitionLayout) -> dict[str, object]:
    """Index the scan/condition loops of a frame-stream layout by kind."""
    return {
        loop.kind: loop
        for loop in layout.event_loops
        if loop.kind in {"scan_x", "scan_y", "condition"}
    }


def raster_geometry_from_layout(layout: AcquisitionLayout | None):
    """Return ``(scan_dims, step_sizes, condition_loop)`` recorded by a scan.

    ``None`` when the layout does not describe a two-dimensional raster, which
    leaves the manual entries authoritative.
    """
    if layout is None or layout.payload_kind != PAYLOAD_DETECTOR_FRAME_STREAM:
        return None
    loops = _scan_loops(layout)
    fast, slow = loops.get("scan_x"), loops.get("scan_y")
    if fast is None or slow is None:
        return None
    steps = tuple(
        float(loop.step) if loop.step else 0.0 for loop in (fast, slow)
    )
    return (fast.count, slow.count), steps, loops.get("condition")


def _condition_labels(loop) -> tuple[str, ...]:
    if loop is None:
        return ()
    if loop.labels:
        return tuple(loop.labels)
    return tuple(f"condition_{index}" for index in range(loop.count))


def raster_positions_from_layout(
    layout: AcquisitionLayout, scan_dims
) -> list[tuple[int, int]]:
    """Map each stored frame to its ``(condition, raster index)`` slot.

    Placement follows the resolved logical coordinates rather than arrival
    order, so serpentine traversal, a detector recorded for only some
    conditions, and per-expanded-line enable masks all land in the right pixel
    without this plugin repeating the index arithmetic.
    """
    x_pixels = scan_dims[0]
    positions = []
    for coordinates in iter_recorded_coordinates(layout):
        condition = 0
        raster_x = raster_y = 0
        for loop in layout.event_loops:
            value = coordinates[loop.id]
            if loop.kind == "scan_x":
                raster_x = value
            elif loop.kind == "scan_y":
                raster_y = value
            elif loop.kind == "condition":
                condition = value
        positions.append((condition, raster_y * x_pixels + raster_x))
    return positions


def reconstruct_bead_image(
    frames, scan_dims, roi_bounds=None, step_sizes=None, raster_indices=None
):
    """Raster-reconstruct a bead image from a frame stack.

    ``frames``: ``(N, Y, X)``; ``scan_dims``: ``(x_pixels, y_pixels)``;
    ``roi_bounds``: ``(x0, y0, x1, y1)`` detection ROI or ``None`` (full frame);
    ``step_sizes``: ``(x_step, y_step)`` for anisotropic-pixel rescale or ``None``;
    ``raster_indices``: flat destination pixel per frame, from the resolved
    acquisition layout, or ``None`` to fill the raster in arrival order.
    """
    frames = np.asarray(frames)
    if frames.ndim != 3:
        raise ValueError("BeadRec expects a 3D frame stack (N, Y, X)")
    _, height, width = frames.shape
    roi = (RoiBounds(0, 0, width, height) if roi_bounds is None
           else normalize_roi_bounds(roi_bounds, (height, width)))

    buffer = create_reconstruction_buffer(scan_dims)
    if raster_indices is None:
        # append_roi_means stops at the end of the buffer, so an over-long
        # stack would be silently discarded. The caller checks the count.
        if frames.shape[0] != buffer.size:
            raise ValueError(
                f"BeadRec needs exactly {buffer.size} frames for a "
                f"{scan_dims[0]}x{scan_dims[1]} raster, got {frames.shape[0]}"
            )
        update = append_roi_means(buffer, 0, list(frames), roi, wrap=False)
        buffer = update.buffer
    else:
        if len(raster_indices) != frames.shape[0]:
            raise ValueError(
                f"BeadRec received {len(raster_indices)} raster positions for "
                f"{frames.shape[0]} frames"
            )
        for frame, index in zip(frames, raster_indices):
            buffer[index] = mean_intensity_in_roi(np.asarray(frame), roi)
    image = reconstruction_image(buffer, scan_dims)

    if step_sizes and len(step_sizes) >= 2 and step_sizes[0] > 0 and step_sizes[1] > 0:
        image = rescale_reconstruction_to_pixel_size(image, step_sizes)
    return np.asarray(image, dtype=np.float32)


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
    file_extensions = ["hdf5", "tiff", "tif", "zarr"]
    description = "Raster-reconstruct a recorded bead scan (+ optional fit)"
    default_save_subdir = "beadrec"

    #: A raster of camera frames. Extra loops are kept rather than rejected:
    #: a condition loop is reconstructed per condition instead of being mixed
    #: into one raster. ``allow_ambiguous`` is on because the manual Scan X/Y
    #: entries are a legitimate override for a recording without a layout.
    acquisition_requirements = AcquisitionRequirements(
        payload_kinds=frozenset({PAYLOAD_DETECTOR_FRAME_STREAM}),
        allowed_extra_loops="split",
        allow_ambiguous=True,
    )

    def make_param_widget(self, parent):
        return _BeadRecParamWidget(parent)

    def make_metadata_dialog(self, parent):
        return None

    def _resolved_layout(self, data_obj) -> AcquisitionLayout | None:
        """The recorded layout, or ``None`` when the source cannot supply one."""
        try:
            resolved = data_obj.acquisition_layout
        except Exception:
            return None
        if resolved is None or resolved.confidence == "low":
            return None
        return resolved.layout

    def process(
        self, data_obj: "DataObj", params: dict, context=None
    ) -> ArrayProcessingResult:
        self.validate_source(data_obj)
        frames = _as_frame_stack(data_obj.data)
        n_frames = int(frames.shape[0])

        layout = self._resolved_layout(data_obj)
        recorded = raster_geometry_from_layout(layout)
        manual_dims = infer_scan_dims(
            n_frames, params.get("scan_x", 0), params.get("scan_y", 0)
        )
        if recorded is not None:
            # The recording is authoritative. A stale spinbox must not quietly
            # reshape a scan whose geometry was recorded; the supported way to
            # correct a wrong layout is a persisted layout override.
            scan_dims, steps, condition = recorded
            geometry_source = "layout"
            if manual_dims is not None and tuple(manual_dims) != tuple(scan_dims):
                raise ValueError(
                    f"Scan X/Y is set to {manual_dims[0]}x{manual_dims[1]}, but "
                    f"this recording declares {scan_dims[0]}x{scan_dims[1]}. "
                    f"Clear the manual entries to use the recorded geometry, or "
                    f"persist a layout override if the recording is wrong."
                )
        elif manual_dims is not None:
            scan_dims, steps, condition = manual_dims, None, None
            geometry_source = "manual"
        else:
            raise ValueError(
                "BeadRec cannot determine the raster: this source carries no "
                "acquisition layout and neither Scan X nor Scan Y pixels were "
                "set. Enter the scan size, or open a recording that carries "
                "its acquisition layout."
            )

        roi = params.get("roi")
        if steps and steps[0] > 0 and steps[1] > 0:
            step_sizes = steps
        else:
            step_sizes = (params.get("step_x", 1.0), params.get("step_y", 1.0))

        pixels = scan_dims[0] * scan_dims[1]
        metadata = {
            "scan_dims": scan_dims,
            "n_frames": n_frames,
            "geometry_source": geometry_source,
        }

        if geometry_source == "layout":
            slots = raster_positions_from_layout(layout, scan_dims)
            if n_frames != len(slots):
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
            recorded = sorted(grouped)
            incomplete = [
                index for index in recorded if len(grouped[index][0]) != pixels
            ]
            if incomplete:
                raise ValueError(
                    f"BeadRec needs a complete {scan_dims[0]}x{scan_dims[1]} "
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
                for index in recorded
            ]
            labels = _condition_labels(condition)
            metadata["condition_labels"] = tuple(
                labels[index] if index < len(labels) else f"condition_{index}"
                for index in recorded
            )
        else:
            if n_frames != pixels:
                raise ValueError(
                    f"BeadRec needs exactly {pixels} frames for a "
                    f"{scan_dims[0]}x{scan_dims[1]} raster, but this source "
                    f"has {n_frames}. Frames are never padded or discarded to "
                    f"force a fit."
                )
            images = [reconstruct_bead_image(frames, scan_dims, roi, step_sizes)]
            recorded = [0]

        if len(images) > 1:
            # Conditions stay a separate axis; mixing them into one raster
            # averages two different illumination states into each pixel.
            image = np.stack(images)
            axis_labels = ["Condition", "Y", "X"]
            fit_target = images[0]
        else:
            # One image, but the label still says which condition it is.
            image = images[0]
            axis_labels = ["Y", "X"]
            fit_target = image

        fit_model = params.get("fit_model", "none")
        if fit_model and fit_model != "none":
            try:
                fit = fit_bead(fit_target, fit_model)
                metadata["fit"] = {
                    "model": fit.model, "r_squared": fit.r_squared,
                    "center_px": tuple(fit.center_px), **fit.params,
                }
            except Exception as exc:  # a bad/flat image must not crash the recon
                metadata["fit_error"] = str(exc)

        name = getattr(data_obj, "name", None) or "bead"
        return ArrayProcessingResult(
            name=f"{name} (beadrec)",
            data=image,
            axis_labels=axis_labels,
            metadata=metadata,
        )
