"""MoNaLISA lattice reconstructor: the lattice pipeline as an ImProcess plugin.

Experimental, and beside the MoNaLISA reconstructor, not in its place. It
reads the illumination lattice, the spot, the scan step, the orientation of
the scan and the confinement of the foci from the frames, for any lattice,
and reconstructs with the pipeline of
``docs/monalisa_optimal_reconstruction.md`` (sections 8 to 10).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.reconstructors.base import DetectionPreview, Reconstructor

from ..monalisa.pipeline import PipelineParams, find_foci, reconstruct_scan
from .params_widget import AUTO, DEFAULT_PARAMS, MonalisaLatticeParamsWidget
from .result import MonalisaLatticeResult
from .scan import recorded_scan

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj

# A pinhole stack is some seventy images of the size of the reconstruction.
PINHOLE_STACK_LIMIT_BYTES = 8 * 1024**3


def pipeline_params(params: dict) -> PipelineParams:
    """The pipeline's parameters for the parameters of the panel."""
    reassignment = str(params["reassignment"])
    if reassignment == "fixed":
        reassignment = float(params["shift_factor"])
    elif reassignment not in ("auto", "off"):
        raise ValueError(f"Unknown reassignment {reassignment!r}")
    background = str(params["background"])
    if background not in ("constant", "none", "auto"):
        raise ValueError(f"Unknown background {background!r}")
    return PipelineParams(
        reach_sigma=float(params["reach_sigma"]),
        background=background,
        joint=bool(params["joint_fit"]),
        reassignment=reassignment,
        frame_gain="smooth" if params["frame_gain"] else "off",
        cell_offsets="seams" if params["cell_offsets"] else "off",
        sharpen_sigma_px=float(params["sharpen_sigma_px"]) if params["sharpen"] else None,
        sharpen_regularization=float(params["sharpen_regularization"]),
        # Every image where it belongs, so that the stack can be projected.
        pinhole_stack="shifted" if params["pinhole_stack"] else "off",
    )


class MonalisaLatticeReconstructor(Reconstructor):
    """MoNaLISA reconstruction for any illumination lattice, read from the data.

    Takes a recording of one or several raster scans of the unit cell,
    ``(frames, rows, cols)``. Nothing of the pattern is typed in; the scan is
    taken from the recording's metadata unless it is set.
    """

    name = "MoNaLISA lattice (experimental)"
    id = "monalisa-lattice"
    file_extensions = ["hdf5", "h5", "zarr", "tiff"]
    description = (
        "Square, rectangular, hexagonal or rotated patterns; lattice, spot, "
        "scan step and orientation read from the frames"
    )
    execution_policy = "worker"
    requires_frame_stacks = True

    def __init__(self):
        self._logger = initLogger("MonalisaLatticeReconstructor")

    @classmethod
    def default_params(cls) -> dict:
        return dict(DEFAULT_PARAMS)

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return MonalisaLatticeParamsWidget(parent)

    def make_metadata_dialog(self, parent: QtWidgets.QWidget) -> QtWidgets.QDialog | None:
        return None

    def detection_preview(self, data_obj, displayed_image, params: dict):
        """The foci read from the frames, for the widget's *Show found foci*.

        Lattice and spot width depend on the frames alone, not on the
        displayed one or the parameters, so they are found once per
        recording and kept.
        """
        if data_obj is None:
            return None
        frames_key = (id(data_obj), getattr(data_obj, "name", None))
        cached = getattr(self, "_preview_cache", None)
        if cached is None or cached[0] != frames_key:
            frames = self._frames(data_obj)[0]
            if frames.ndim != 3:
                raise ValueError(f"Expected (frames, rows, cols), got {frames.shape}")
            finding = find_foci(
                frames.mean(axis=0, dtype=np.float64),
                frames.var(axis=0, dtype=np.float64),
            )
            self._preview_cache = cached = (frames_key, finding)
        finding = cached[1]
        image = np.asarray(displayed_image)
        rows, cols = image.shape[-2:] if image.ndim >= 2 else finding.calibration.image.shape
        x, y = finding.points(rows, cols)
        return DetectionPreview(x, y, finding.describe())

    @staticmethod
    def _frames(data_obj) -> tuple[np.ndarray, dict]:
        """The frames and the attributes of a recording, loaded and released."""
        preloaded = bool(getattr(data_obj, "dataLoaded", True))
        load = getattr(data_obj, "checkAndLoadData", None)
        try:
            if callable(load):
                load()
            frames = np.asarray(data_obj.data)
            attrs = dict(getattr(data_obj, "attrs", None) or {})
        finally:
            unload = getattr(data_obj, "checkAndUnloadData", None)
            if not preloaded and callable(unload):
                unload()
        return frames, attrs

    def process(
        self, data_obj: "DataObj", params: dict, context=None
    ) -> MonalisaLatticeResult:
        started = time.perf_counter()
        params = {**DEFAULT_PARAMS, **dict(params or {})}

        def report(phase, completed, total, message):
            if context is not None:
                context.report(phase, completed, total, message)

        def check_cancelled():
            if context is not None:
                context.check_cancelled()

        report("inspect", 0, 1, "Reading the frames")
        frames, attrs = self._frames(data_obj)
        if frames.ndim != 3:
            raise ValueError(
                f"Expected frames of shape (frames, rows, cols), got {frames.shape}"
            )

        scan = recorded_scan(
            attrs, frames.shape[0],
            int(params["scan_steps_fast"]), int(params["scan_steps_slow"]),
            float(params["scan_step_nm"]),
        )
        self._logger.info(f"{data_obj.name}: scan of {scan.describe()}")
        pixel_nm = float(params["pixel_size_nm"])
        step = None
        if pixel_nm > 0:
            if scan.step_nm is None:
                raise ValueError(
                    "A camera pixel size is set but the size of a scan step is "
                    "not known: set the step, or leave the pixel size at 0"
                )
            step = np.diag([scan.step_nm / pixel_nm] * 2)
        orientation = None if params["orientation"] == AUTO else str(params["orientation"])
        settings = pipeline_params(params)
        shape = (scan.num_fast, scan.num_slow)
        per_stack = scan.frames_per_stack

        check_cancelled()
        report("align", 0, 1, "Finding the lattice and the orientation of the scan")
        results = []
        geometry = None
        for index in range(scan.num_stacks):
            check_cancelled()
            report("assemble", index, scan.num_stacks,
                   f"Reconstructing scan {index + 1} of {scan.num_stacks}")
            stack = frames[index * per_stack:(index + 1) * per_stack]
            result = reconstruct_scan(
                stack, step, shape, settings,
                orientation=orientation, geometry=geometry,
            )
            if geometry is None:
                geometry = result.geometry
                orientation = result.diagnostics["orientation"]
                self._check_pinhole_stack(result, scan.num_stacks)
            results.append(result)
        report("finalize", 0, 1, "Assembling the result")

        first = results[0]
        step_px = np.asarray(geometry.step, dtype=float)
        pitch_px = float(np.sqrt(abs(np.linalg.det(step_px))))
        camera_pixel_nm = pixel_nm if pixel_nm > 0 else (
            scan.step_nm / pitch_px if scan.step_nm else None
        )
        output_pixel_nm = scan.step_nm if scan.step_nm else (
            pixel_nm * pitch_px if pixel_nm > 0 else None
        )
        diagnostics = self._diagnostics(
            scan, first, camera_pixel_nm, output_pixel_nm,
            time.perf_counter() - started,
        )
        for key, value in diagnostics.items():
            self._logger.info(f"{data_obj.name}: {key}: {value}")

        def planes(pick):
            images = [pick(result) for result in results]
            if any(image is None for image in images):
                return None
            return np.nan_to_num(np.stack(images)).astype(np.float32)

        pinholes = None
        offsets = None
        if first.pinholes is not None:
            pinholes = np.nan_to_num(
                np.stack([result.pinholes.images for result in results])
            )
            if scan.num_stacks == 1:
                pinholes = pinholes[0]
            offsets = (first.pinholes.dx, first.pinholes.dy)
        report("finalize", 1, 1, "Done")
        return MonalisaLatticeResult(
            name=f"{data_obj.name}_lattice",
            image=planes(lambda result: result.amplitude.image),
            output_pixel_nm=output_pixel_nm,
            params=params,
            diagnostics=diagnostics,
            sharpened=planes(lambda result: result.sharpened),
            background=planes(
                lambda result: None if result.background is None
                else result.background.image
            ),
            pinholes=pinholes,
            pinhole_offsets=offsets,
        )

    @staticmethod
    def _check_pinhole_stack(result, num_stacks: int) -> None:
        if result.pinholes is None:
            return
        size = result.pinholes.images.nbytes * int(num_stacks)
        if size > PINHOLE_STACK_LIMIT_BYTES:
            raise ValueError(
                f"The pinhole stack of this recording would take "
                f"{size / 1024**3:.1f} GB ({result.pinholes.images.shape[0]} "
                f"pinholes, {num_stacks} scans): use a smaller footprint, or "
                "switch the pinhole stack off"
            )

    @staticmethod
    def _diagnostics(scan, result, camera_pixel_nm, output_pixel_nm, seconds) -> dict:
        geometry = result.geometry.diagnostics
        found = result.diagnostics
        lattice = result.geometry.lattice
        a1, a2 = (np.asarray(lattice.a1), np.asarray(lattice.a2))
        angle = np.degrees(np.arctan2(a1[1], a1[0]))
        between = np.degrees(np.arccos(np.clip(
            abs(a1 @ a2) / (np.linalg.norm(a1) * np.linalg.norm(a2)), 0.0, 1.0
        )))
        sigma = float(geometry["spot_sigma_px"])
        rows = {
            "scan": scan.describe(),
            "lattice periods (px)": (
                f"{np.linalg.norm(a1):.3f}, {np.linalg.norm(a2):.3f}"
            ),
            "lattice angle, tilt (deg)": f"{between:.1f}, {angle:.1f}",
            "foci": int(geometry["num_foci"]),
            "spot sigma (px)": round(sigma, 3),
            "calibration": (
                f"{geometry['calibration_image']} frame; foci contrast: " + ", ".join(
                    f"{name} {value:.2f}"
                    for name, value in geometry["calibration_contrast"].items()
                    if np.isfinite(value)
                )
            ),
        }
        if camera_pixel_nm:
            rows["spot FWHM (nm)"] = round(2.355 * sigma * camera_pixel_nm, 0)
            rows["camera pixel (nm)"] = round(float(camera_pixel_nm), 2)
        if output_pixel_nm:
            rows["output pixel (µm)"] = round(float(output_pixel_nm) / 1000.0, 5)
        rows["scan step"] = (
            "from the cell of the lattice" if not geometry["step_given"]
            else "locked to the lattice, changed by "
                 f"{100 * geometry['step_lock_change']:.2f} %"
            if geometry["step_locked"] else "as given"
        )
        coverage = (
            f"{geometry['coverage_holes']} holes, "
            f"{geometry['coverage_overlaps']} overlaps of "
            f"{geometry['coverage_cell_pixels']} positions"
        )
        if geometry["coverage_holes"] > 0 and geometry["step_given"]:
            # A scan that leaves holes in the cell was stepped too finely
            # for its number of steps: the recorded step is suspect.
            positions = max(1, int(scan.num_fast) * int(scan.num_slow))
            covering = float(np.sqrt(lattice.cell_area / positions))
            used = float(np.sqrt(abs(np.linalg.det(np.asarray(geometry["step_px"])))))
            ratio = covering / used if used > 0 else float("nan")
            coverage += (
                f"; a step {ratio:.2f} x the one used"
                + (f" ({ratio * scan.step_nm:.1f} nm)" if scan.step_nm else "")
                + " would cover the cell once: check the recorded step"
            )
        rows["coverage of the cell"] = coverage
        rows["orientation"] = found["orientation"]
        if "orientation_margin" in found:
            rows["orientation, lead over the next"] = round(
                float(found["orientation_margin"]), 2
            )
        if "frame_gain_rms" in found:
            rows["frame gain (% rms)"] = round(100 * found["frame_gain_rms"], 1)
        if found.get("cell_offset_rms") is not None:
            rows["cell offsets applied (rms)"] = round(found["cell_offset_rms"], 2)
            rows["cell offsets, share that is offset"] = round(
                found["cell_offset_share"], 2
            )
        if "shift_factor" in found:
            rows["shift factor"] = round(float(found["shift_factor"]), 3)
            if "shift_factor_xy" in found:
                rows["shift factor x, y"] = (
                    f"{found['shift_factor_xy'][0]:.3f}, "
                    f"{found['shift_factor_xy'][1]:.3f}"
                )
        rows["image"] = (
            "reassigned" if found["placement"] == "reassigned"
            else "amplitudes placed" if found["placement"] == "exact"
            else "amplitudes gridded"
        )
        if result.pinholes is not None:
            rows["pinhole stack"] = f"{result.pinholes.images.shape[0]} pinholes"
        rows["time (s)"] = round(float(seconds), 1)
        return rows


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
