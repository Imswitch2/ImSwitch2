"""MoNaLISA lattice reconstructor: the lattice pipeline as an ImProcess plugin.

Experimental, and beside the MoNaLISA reconstructor, not in its place. It
reads the illumination lattice, the spot and the confinement of the foci
from the frames, for any lattice, and reconstructs with the pipeline of
``docs/monalisa_optimal_reconstruction.md`` (sections 8 to 10).

The scan comes from the recording's acquisition layout when it has one:
every frame is placed by its recorded position, Z planes and timepoints
become axes of the result, and a camera axis the recording does not step
(a scan of Y and Z alone) comes out one pixel per lattice cell. A recording
without a layout is one raster of ``num_fast x num_slow`` steps per stack
whose orientation is read from the data, as before.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.reconstructors.base import DetectionPreview, Reconstructor

from ..monalisa.pipeline import (
    PipelineParams,
    find_foci,
    prepare_geometry,
    reconstruct_scan,
)
from ..monalisa.scan_frame import ORIENTATIONS
from .params_widget import AUTO, DEFAULT_PARAMS, MonalisaLatticeParamsWidget
from .result import MonalisaLatticeResult
from .scan import LayoutScan, layout_scan, recorded_scan

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
    # The footprint is 2.5 spot widths (96 % of a Gaussian spot; the joint
    # fit makes a wide footprint safe) and the foci are always fitted
    # together: neither is a choice the panel offers.
    return PipelineParams(
        background=background,
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

    Takes a recording of one or several scans of the unit cell, ``(frames,
    rows, cols)``: X and Y, Y and Z, X, Y and Z, each over time. Nothing of
    the pattern is typed in; the scan is taken from the recording's
    acquisition layout, or from its attributes, unless it is set.
    """

    name = "MoNaLISA lattice (experimental)"
    id = "monalisa-lattice"
    file_extensions = ["hdf5", "h5", "zarr", "tiff"]
    description = (
        "Square, rectangular, hexagonal or rotated patterns; lattice, spot, "
        "scan step and orientation read from the frames; XY, YZ and XYZ scans "
        "placed by the acquisition layout"
    )
    execution_policy = "worker"
    requires_frame_stacks = True
    supports_consolidation = True

    def __init__(self):
        self._logger = initLogger("MonalisaLatticeReconstructor")

    @classmethod
    def default_params(cls) -> dict:
        return dict(DEFAULT_PARAMS)

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField('scan_steps_fast', 'int', 0, label='Steps, fast axis',
                       min=0, max=10000, group='Scan', help='0: taken from the recording'),
            ParamField('scan_steps_slow', 'int', 0, label='Steps, slow axis',
                       min=0, max=10000, group='Scan', help='0: taken from the recording'),
            ParamField('scan_step_nm', 'float', 0.0, label='Step', suffix='nm',
                       min=0, max=10000, group='Scan', help='0: taken from the recording'),
            ParamField('pixel_size_nm', 'float', 0.0, label='Camera pixel', suffix='nm',
                       min=0, max=10000, group='Scan',
                       help='0: the scan is taken to cover the cell of the lattice once'),
            ParamField('orientation', 'select', AUTO, label='Orientation',
                       options=(AUTO, *ORIENTATIONS), group='Scan',
                       help='Fast axis first, for recordings without an acquisition layout'),
            ParamField('background', 'select', 'constant', label='Background',
                       options=('constant', 'none', 'auto'), group='Estimator',
                       help='auto: a haze term where the mean frame calls for one'),
            ParamField('frame_gain', 'bool', True, label='Frame gain', group='Corrections',
                       help='Divide out the brightness of the frames over the scan'),
            ParamField('cell_offsets', 'bool', True, label='Cell offsets', group='Corrections',
                       help='Take the offset of every focus off, read from the cell borders'),
            ParamField('reassignment', 'select', 'auto', label='Reassignment',
                       options=('auto', 'off', 'fixed'), group='Corrections',
                       help='auto: measure the shift factor and reassign when foci are wide'),
            ParamField('shift_factor', 'float', 0.0, label='Shift factor',
                       min=0.0, max=1.0, step=0.05, group='Corrections',
                       help='Used when Reassignment is fixed'),
            ParamField('sharpen', 'bool', False, label='Sharpened image', group='Outputs'),
            ParamField('sharpen_sigma_px', 'float', 1.5, label='Sharpening blur',
                       min=0.1, max=10.0, step=0.25, suffix='px', group='Outputs',
                       help='Width of the blur the filter undoes, output pixels'),
            ParamField('sharpen_regularization', 'float', 0.1,
                       label='Sharpening regularization', min=0.001, max=10.0,
                       step=0.01, group='Outputs',
                       help='Smaller sharpens more and amplifies more noise'),
            ParamField('pinhole_stack', 'bool', False, label='Pinhole stack (sanity check)',
                       group='Outputs',
                       help='The raw frames as one image per pixel of the footprint'),
        )

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
        return DetectionPreview(x, y, finding.describe(), marker="point")

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

    def _reuse_geometry(self, frames, settings):
        """Return the last explicitly localized geometry for these camera frames.

        The cache is intentionally session-local and deliberately simple: a
        successful reconstruction with *Reuse last localization* disabled
        replaces it.  This is a practical escape hatch for timepoints where
        the same illumination pattern is present but too weak to localize.
        """
        reference = getattr(self, "_localization_reference", None)
        if reference is None:
            raise ValueError(
                "Reuse last localization is enabled, but no localization has "
                "been cached yet. Reconstruct one good recording first with "
                "this option off, then enable it for the failing frames."
            )
        frame_shape = tuple(np.asarray(frames).shape[-2:])
        if frame_shape != reference["frame_shape"]:
            raise ValueError(
                "The cached localization was measured on camera frames of "
                f"shape {reference['frame_shape']}, but this recording has "
                f"shape {frame_shape}. Re-localize a compatible recording first."
            )
        geometry = replace(reference["geometry"], params=settings)
        return geometry, reference["orientation"], reference["source"]

    def _remember_geometry(self, geometry, orientation, frames, source) -> None:
        """Keep one successful localization for an explicit later reuse."""
        self._localization_reference = {
            "geometry": geometry,
            "orientation": str(orientation),
            "frame_shape": tuple(np.asarray(frames).shape[-2:]),
            "source": str(source),
        }
        self._logger.info(f"{source}: cached localization for later reuse")

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

        # Steps typed into the panel describe the recording as a raster of
        # their own; otherwise the recording's layout places every frame.
        recorded = None
        if int(params["scan_steps_fast"]) <= 0 and int(params["scan_steps_slow"]) <= 0:
            recorded = layout_scan(
                getattr(data_obj, "acquisition_layout", None), frames.shape[0]
            )
        if recorded is not None:
            return self._process_recorded(
                data_obj, frames, recorded, params, started, report, check_cancelled
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
        reuse_localization = bool(params["reuse_localization"])
        reference_source = None
        geometry = None
        if reuse_localization:
            geometry, reference_orientation, reference_source = self._reuse_geometry(
                frames, settings
            )
            if orientation is None:
                orientation = reference_orientation
        shape = (scan.num_fast, scan.num_slow)
        per_stack = scan.frames_per_stack

        check_cancelled()
        report(
            "align", 0, 1,
            f"Reusing localization from {reference_source}"
            if reuse_localization else
            "Finding the lattice and the orientation of the scan",
        )
        results = []
        for index in range(scan.num_stacks):
            check_cancelled()
            report("assemble", index, scan.num_stacks,
                   f"Reconstructing scan {index + 1} of {scan.num_stacks}")
            stack = frames[index * per_stack:(index + 1) * per_stack]
            result = reconstruct_scan(
                stack, step, shape, settings,
                orientation=orientation, geometry=geometry,
            )
            if index == 0:
                geometry = result.geometry
                orientation = result.diagnostics["orientation"]
                self._check_pinhole_stack(result, scan.num_stacks)
                if not reuse_localization:
                    self._remember_geometry(
                        geometry, orientation, frames, data_obj.name
                    )
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
        diagnostics["localization"] = (
            f"reused from {reference_source}"
            if reuse_localization else f"from this recording ({data_obj.name})"
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

    def _process_recorded(
        self, data_obj, frames, scan: LayoutScan, params, started, report, check_cancelled
    ) -> MonalisaLatticeResult:
        """Reconstruct a recording whose acquisition layout places its frames.

        One stack per Z plane and timepoint, all on the raster of one
        geometry read from every frame of the recording: the lattice and
        the spot do not change between planes, and a plane with little in
        focus would read them badly on its own. The stack with the most
        modulation goes first and measures the shift factor for the rest.
        """
        typed_step = float(params["scan_step_nm"])
        if typed_step > 0:
            # What the panel says outranks what the recording says, as on
            # the attribute path: one step for every scanned axis.
            scan = replace(
                scan,
                step_nm=tuple(
                    typed_step if count > 1 else None for count in scan.counts
                ),
                source="acquisition layout, step from the panel",
            )
        self._logger.info(f"{data_obj.name}: scan of {scan.describe()}")
        pixel_nm = float(params["pixel_size_nm"])
        step = None
        step_aspect = None
        if pixel_nm > 0:
            if not any(scan.step_nm):
                raise ValueError(
                    "A camera pixel size is set but the recording does not "
                    "say the size of its scan steps: set the step, or leave "
                    "the pixel size at 0"
                )
            # An axis the recording does not step, or whose step it does not
            # say, is left to the lattice (a NaN column).
            step = np.full((2, 2), np.nan)
            for axis, step_nm in enumerate(scan.step_nm):
                if step_nm:
                    step[:, axis] = 0.0
                    step[axis, axis] = step_nm / pixel_nm
        elif all(scan.step_nm):
            # No pixel size, but the recording says both steps: their ratio
            # is known even if their size in pixels is not.
            step_aspect = float(scan.step_nm[0]) / float(scan.step_nm[1])
        settings = pipeline_params(params)
        notes = {}
        if len(scan.scanned_axes) == 1:
            # The shift factor is a displacement in the plane; a scan that
            # samples one axis cannot measure it, and a stack shifted by an
            # unmeasured factor would say nothing.
            if settings.reassignment == "auto":
                settings = replace(settings, reassignment="off")
                notes["reassignment"] = (
                    f"not measured: only {scan.scanned_axes[0]} is scanned"
                )
            if settings.pinhole_stack == "shifted":
                settings = replace(settings, pinhole_stack="raw")
        # The layout says where every frame is along the stage axes; how the
        # stage lies against the camera (signs, a swap) is read from the data
        # unless it is set, as for a raster.
        orientation = None if params["orientation"] == AUTO else str(params["orientation"])
        reuse_localization = bool(params["reuse_localization"])
        reference_source = None

        check_cancelled()
        order = self._stack_order(frames, scan)
        if reuse_localization:
            geometry, reference_orientation, reference_source = self._reuse_geometry(
                frames, settings
            )
            if orientation is None:
                orientation = reference_orientation
            report("align", 0, 1, f"Reusing localization from {reference_source}")
        else:
            report("align", 0, 1, "Finding the lattice and the spot")
            mean_frame = frames.mean(axis=0, dtype=np.float64)
            modulation_frame = frames.var(axis=0, dtype=np.float64)
            geometry = prepare_geometry(
                mean_frame, step, scan.index[scan.frames_of(order[0])], settings,
                modulation_frame=modulation_frame, step_aspect=step_aspect,
            )
        shape = scan.in_plane_shape

        results = {}
        for number, stack in enumerate(order):
            check_cancelled()
            report("assemble", number, scan.num_stacks,
                   f"Reconstructing plane {number + 1} of {scan.num_stacks}")
            chosen = scan.frames_of(stack)
            result = reconstruct_scan(
                frames[chosen], step, shape, settings,
                orientation=orientation, geometry=geometry,
                scan_index=scan.index[chosen],
                gain_positions=scan.gain_positions(stack),
                step_aspect=step_aspect,
            )
            if number == 0:
                # The first stack's raster, mapping and shift factor serve
                # the rest: one recording, one geometry.
                geometry = result.geometry
                orientation = result.diagnostics["orientation"]
                self._check_pinhole_stack(result, scan.num_stacks)
                if not reuse_localization:
                    self._remember_geometry(
                        geometry, orientation, frames, data_obj.name
                    )
            results[int(stack)] = result
        report("finalize", 0, 1, "Assembling the result")

        first = results[int(order[0])]
        # The output raster is in camera axes; the mapping says which stage
        # axis, and so which recorded step, lies along each.
        swapped = str(orientation)[1] == "y"
        step_px = np.asarray(geometry.step, dtype=float)
        pitch_px = tuple(float(np.linalg.norm(step_px[:, axis])) for axis in (0, 1))
        recorded_nm = tuple(
            scan.step_nm[1 - axis if swapped else axis] for axis in (0, 1)
        )
        camera_pixel_nm = pixel_nm if pixel_nm > 0 else next(
            (step_nm / pitch_px[axis] for axis, step_nm in enumerate(recorded_nm)
             if step_nm and pitch_px[axis] > 0),
            None,
        )
        output_nm = tuple(
            step_nm if step_nm else (
                pitch_px[axis] * camera_pixel_nm if camera_pixel_nm else None
            )
            for axis, step_nm in enumerate(recorded_nm)
        )
        output_pixel_nm = (
            (output_nm[1], output_nm[0]) if all(output_nm) else None
        )
        diagnostics = self._diagnostics(
            scan, first, camera_pixel_nm, output_pixel_nm,
            time.perf_counter() - started,
        )
        diagnostics["localization"] = (
            f"reused from {reference_source}"
            if reuse_localization else f"from this recording ({data_obj.name})"
        )
        diagnostics.update(notes)
        for key, value in diagnostics.items():
            self._logger.info(f"{data_obj.name}: {key}: {value}")

        labels = (
            (["T"] if scan.num_time > 1 else [])
            + (["C"] if scan.num_conditions > 1 else [])
            + (["Z"] if scan.num_planes > 1 else [])
            + ["Y", "X"]
        )

        def planes(pick):
            images = [pick(results[stack]) for stack in range(scan.num_stacks)]
            if any(image is None for image in images):
                return None
            stacked = np.nan_to_num(np.stack(images)).astype(np.float32)
            # Stacks are numbered (t, c, z); an axis of one is left out.
            shape = tuple(n for n in scan.stack_shape if n > 1) + stacked.shape[1:]
            return stacked.reshape(shape)

        pinholes = None
        offsets = None
        if first.pinholes is not None:
            pinholes = planes(lambda result: result.pinholes.images)
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
            axis_labels=labels,
            plane_step_nm=scan.plane_step_nm,
            condition_labels=scan.condition_labels if scan.num_conditions > 1 else (),
        )

    def consolidate(self, results: list) -> MonalisaLatticeResult:
        """The results of several recordings as one, along a ``T`` axis.

        A time lapse recorded one file per timepoint is reconstructed one
        file at a time, each on its own geometry; the images are stacked in
        the order given, behind the ``T`` axis they have when a file holds
        several timepoints. Results of different sizes cannot be stacked and
        are refused by name.
        """
        results = list(results)
        if not results:
            raise ValueError("No results to consolidate")
        if len(results) == 1:
            return results[0]
        first = results[0]
        for other in results[1:]:
            if other.axis_labels != first.axis_labels or other.data.shape != first.data.shape:
                raise ValueError(
                    f"Cannot consolidate '{other.name}' into '{first.name}': "
                    f"{tuple(other.data.shape)} {other.axis_labels} does not match "
                    f"{tuple(first.data.shape)} {first.axis_labels}; were these "
                    "recorded with the same scan?"
                )
        has_time = first.axis_labels[0] == "T"
        stack = (np.concatenate if has_time else np.stack)

        def along(pick):
            parts = [pick(result) for result in results]
            return None if any(part is None for part in parts) else stack(parts)

        labels = list(first.axis_labels) if has_time else ["T", *first.axis_labels]
        diagnostics = dict(first.diagnostics)
        diagnostics["consolidated"] = f"{len(results)} results: " + ", ".join(
            result.name for result in results
        )
        return MonalisaLatticeResult(
            name=first.name,
            image=along(lambda result: result.data),
            output_pixel_nm=first.output_pixel_size_nm,
            params=first.params,
            diagnostics=diagnostics,
            sharpened=along(lambda result: result.sharpened),
            background=along(lambda result: result.background),
            pinholes=along(lambda result: result.pinholes),
            pinhole_offsets=first.pinhole_offsets,
            axis_labels=labels,
            plane_step_nm=first.plane_step_nm,
            condition_labels=first.condition_labels,
        )

    @staticmethod
    def _stack_order(frames: np.ndarray, scan: LayoutScan) -> list[int]:
        """The stacks, the one with the most modulation first.

        The temporal variance of a stack's frames is the foci moving over the
        specimen; a plane with nothing in focus has little of it, and is not
        the one to measure the shift factor on.
        """
        if scan.num_stacks == 1:
            return [0]
        modulation = []
        for stack in range(scan.num_stacks):
            chosen = frames[scan.frames_of(stack)]
            modulation.append(float(chosen.var(axis=0, dtype=np.float64).mean()))
        first = int(np.argmax(modulation))
        return [first] + [stack for stack in range(scan.num_stacks) if stack != first]

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
    def _resolution_rows(factor, spot_sigma_px, camera_pixel_nm, *, reassigned) -> dict:
        """What the shift factor says about the foci and the resolution.

        The spot on the camera is the emitting region of a focus imaged
        through the detection PSF: for a specimen uniform over the focus
        (the mean frame of a scan is that average) its variance is the sum
        of the two, ``sigma_spot^2 = sigma_e^2 + sigma_d^2``. The shift
        factor is ``sigma_e^2 / (sigma_e^2 + sigma_d^2)``
        (``docs/monalisa_optimal_reconstruction.md``, section 2.5), so the
        effective excitation width is ``sqrt(alpha) * sigma_spot``: the PSF
        of the placed amplitudes. Reassignment narrows it by
        ``sqrt(1 - alpha)``. A factor within its own noise gives an upper
        bound.
        """
        if factor is None:
            return {}
        unit = "nm" if camera_pixel_nm else "px"
        scale = float(camera_pixel_nm) if camera_pixel_nm else 1.0
        alpha = float(np.clip(factor.alpha, 0.0, 0.999))
        noise = 2.0 * float(factor.spread) if np.isfinite(factor.spread) else 0.0

        def fwhm(value: float) -> float:
            return 2.355 * float(np.sqrt(min(value, 0.999))) * float(spot_sigma_px) * scale

        rows = {}
        if alpha <= noise:
            bound = fwhm(alpha + noise)
            rows[f"excitation FWHM ({unit})"] = (
                f"below {bound:.0f} (shift factor within its noise: confined foci)"
            )
            rows[f"expected resolution ({unit})"] = f"below {bound:.0f}"
            return rows
        excitation = fwhm(alpha)
        rows[f"excitation FWHM ({unit})"] = round(excitation, 0)
        resolution = excitation * float(np.sqrt(1.0 - alpha)) if reassigned else excitation
        rows[f"expected resolution ({unit})"] = round(resolution, 0)
        return rows

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
                ) + (
                    f"; variance spots {geometry['calibration_width_ratio']:.2f} x "
                    "the mean's width"
                    + (" (shot noise, not modulation)"
                       if geometry["calibration_image"] == "mean" else "")
                    if geometry.get("calibration_width_ratio") else ""
                )
            ),
        }
        if camera_pixel_nm:
            rows["spot FWHM (nm)"] = round(2.355 * sigma * camera_pixel_nm, 0)
            rows["camera pixel (nm)"] = round(float(camera_pixel_nm), 2)
        if output_pixel_nm is not None and np.all(np.asarray(output_pixel_nm) > 0):
            pixel = np.atleast_1d(np.asarray(output_pixel_nm, dtype=float))
            if pixel.size > 1 and abs(pixel[0] - pixel[-1]) >= 0.01:
                rows["output pixel (µm)"] = (
                    f"Y {pixel[0] / 1000:.5g} / X {pixel[-1] / 1000:.5g}"
                )
            else:
                rows["output pixel (µm)"] = round(float(pixel[0]) / 1000.0, 5)
        if geometry.get("step_completed_axes"):
            rows["raster from the lattice"] = ", ".join(
                f"{axis}: one period" if axis not in geometry["scanned_axes"]
                else f"{axis}: the period over the steps"
                for axis in geometry["step_completed_axes"]
            )
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
            positions = max(1, int(scan.frames_per_stack))
            covering = float(np.sqrt(lattice.cell_area / positions))
            used = float(np.sqrt(abs(np.linalg.det(np.asarray(geometry["step_px"])))))
            ratio = covering / used if used > 0 else float("nan")
            step_nm = scan.step_nm
            if isinstance(step_nm, tuple):
                step_nm = next((value for value in step_nm if value), None)
            coverage += (
                f"; a step {ratio:.2f} x the one used"
                + (f" ({ratio * step_nm:.1f} nm)" if step_nm else "")
                + " would cover the cell once: check the recorded step"
            )
        rows["coverage of the cell"] = coverage
        rows["orientation"] = found["orientation"]
        if found.get("frames_placed_by"):
            rows["frames placed by"] = found["frames_placed_by"]
        if "orientation_margin" in found:
            rows["orientation, lead over the next"] = round(
                float(found["orientation_margin"]), 2
            )
        if "frame_gain_rms" in found:
            rows["frame gain (% rms)"] = round(100 * found["frame_gain_rms"], 1)
        elif "frame_gain_skipped" in found:
            rows["frame gain"] = f"not fitted: {found['frame_gain_skipped']}"
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
            rows.update(MonalisaLatticeReconstructor._resolution_rows(
                result.geometry.shift_factor, sigma, camera_pixel_nm,
                reassigned=found["placement"] == "reassigned",
            ))
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
