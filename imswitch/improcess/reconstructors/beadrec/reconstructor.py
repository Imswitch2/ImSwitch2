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
    normalize_roi_bounds,
    reconstruction_image,
    rescale_reconstruction_to_pixel_size,
)
from imswitch.imcommon.algorithms.bead_fits import FIT_MODELS
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.reconstructors.base import Reconstructor

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
    """Resolve ``(x_pixels, y_pixels)``; 0 means auto (square, or n/other)."""
    scan_x, scan_y = int(scan_x), int(scan_y)
    if scan_x > 0 and scan_y > 0:
        return scan_x, scan_y
    if scan_x > 0:
        return scan_x, max(1, n_frames // scan_x)
    if scan_y > 0:
        return max(1, n_frames // scan_y), scan_y
    side = int(round(np.sqrt(n_frames)))
    return max(1, side), max(1, side)


def reconstruct_bead_image(frames, scan_dims, roi_bounds=None, step_sizes=None):
    """Raster-reconstruct a bead image from a frame stack.

    ``frames``: ``(N, Y, X)``; ``scan_dims``: ``(x_pixels, y_pixels)``;
    ``roi_bounds``: ``(x0, y0, x1, y1)`` detection ROI or ``None`` (full frame);
    ``step_sizes``: ``(x_step, y_step)`` for anisotropic-pixel rescale or ``None``.
    """
    frames = np.asarray(frames)
    if frames.ndim != 3:
        raise ValueError("BeadRec expects a 3D frame stack (N, Y, X)")
    _, height, width = frames.shape
    roi = (RoiBounds(0, 0, width, height) if roi_bounds is None
           else normalize_roi_bounds(roi_bounds, (height, width)))

    buffer = create_reconstruction_buffer(scan_dims)
    update = append_roi_means(buffer, 0, list(frames), roi, wrap=False)
    image = reconstruction_image(update.buffer, scan_dims)

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

    def make_param_widget(self, parent):
        return _BeadRecParamWidget(parent)

    def make_metadata_dialog(self, parent):
        return None

    def process(self, data_obj: "DataObj", params: dict) -> ArrayProcessingResult:
        frames = _as_frame_stack(data_obj.data)
        scan_dims = infer_scan_dims(frames.shape[0],
                                    params.get("scan_x", 0), params.get("scan_y", 0))
        roi = params.get("roi")
        steps = (params.get("step_x", 1.0), params.get("step_y", 1.0))
        image = reconstruct_bead_image(frames, scan_dims, roi, steps)

        metadata = {"scan_dims": scan_dims, "n_frames": int(frames.shape[0])}
        fit_model = params.get("fit_model", "none")
        if fit_model and fit_model != "none":
            try:
                fit = fit_bead(image, fit_model)
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
            axis_labels=["Y", "X"],
            metadata=metadata,
        )
