"""Incremental *unified* Fast extraction, placement and timepoint finalization.

The historical MonalisaLiveSession supplies the streaming mechanism (one
persistent geometry and a chunk-fed output buffer), not its rectangular
Gaussian/placement algorithm. This module uses the same ExtractionOperator,
recorded scan positions and PlacementPlan as the finite lattice pipeline.

Provisional pixels are never completed timepoints. At the scan boundary,
retained ExtractionResult batches feed the same reconstruct_stack() used by
finite Fast; Gaussian fitting is not repeated and raw frames are not buffered.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from imswitch.improcess.reconstructors.base import Incompatible
from .pipeline import (
    _oriented_aspect, _raster_coordinates, geometry_with_step, prepare_geometry,
    reconstruct_stack,
)
from .extraction import ExtractionResult
from .placement import prepare_placement
from .scan_frame import orient_recorded_index, orient_step_columns
from imswitch.improcess.reconstructors.monalisa_lattice.reconstructor import (
    _recorded_output_scale, pipeline_params, recorded_geometry_inputs,
)
from imswitch.improcess.reconstructors.monalisa_lattice.scan import layout_scan
from .calibration import PatternLocalization


@dataclass(frozen=True)
class IncrementalFastGeometry:
    """One fully specified scan geometry, shared by every timepoint."""

    geometry: object
    scan: object
    orientation: str
    output_pixel_nm: object
    camera_index: np.ndarray

    @classmethod
    def from_locked(cls, reconstructor, params, resolved_layout, first_frame, frames_per_timepoint):
        """Initialize from an explicit localization and orientation, not a partial fit."""
        local_params = reconstructor.unified_lattice_params(params)
        localization = PatternLocalization.from_dict(local_params["_pattern_localization"])
        orientation = local_params["orientation"]
        if localization is None or orientation == "Auto":
            raise Incompatible("Immediate Fast Live initialization requires explicit localization and orientation")
        if local_params["spot_sigma_px"] is None:
            raise Incompatible("Immediate Fast Live initialization requires a manual/default spot width")
        return cls._make(local_params, resolved_layout, frames_per_timepoint,
                         orientation, first_frame=np.asarray(first_frame),
                         localization=localization)

    @classmethod
    def from_calibrated(cls, reconstructor, params, resolved_layout, frames_per_timepoint,
                        first_frame, calibration):
        """Reuse T0's measured values; never redetect from incomplete T1."""
        resolved = (calibration or {}).get("resolved_localization")
        orientation = str((calibration or {}).get("resolved_orientation") or "")
        localization = PatternLocalization.from_dict(resolved)
        if localization is None or not orientation:
            raise Incompatible("First Fast timepoint did not resolve live calibration")
        local_params = reconstructor.unified_lattice_params(params)
        local_params["_pattern_localization"] = localization.to_dict()
        local_params["orientation"] = orientation
        # A first complete T0 may have used a measured spot sigma. This
        # checkpoint only enables Fast with a fixed sigma (see capability gate).
        return cls._make(local_params, resolved_layout, frames_per_timepoint,
                         orientation, first_frame=np.asarray(first_frame),
                         localization=localization)

    @classmethod
    def _make(cls, local_params, resolved_layout, frames_per_timepoint,
              orientation, *, first_frame, localization):
        scan = layout_scan(resolved_layout, int(frames_per_timepoint))
        if scan is None or scan.num_stacks != 1 or scan.frames_per_stack != int(frames_per_timepoint):
            raise Incompatible("Live Fast previews need one recorded, contiguous XY scan per timepoint")
        scan, step, step_aspect, pixel_nm = recorded_geometry_inputs(scan, local_params)
        settings = pipeline_params(local_params)
        # Use the *complete* recorded scan indices for lattice/step topology
        # even when only one actual camera frame has arrived.
        unrotated = prepare_geometry(
            np.asarray(first_frame), step, scan.index, settings,
            localization=localization, step_aspect=step_aspect,
        )
        camera_index = orient_recorded_index(scan.index, orientation)
        camera_step = orient_step_columns(step, orientation)
        oriented = geometry_with_step(
            unrotated, camera_step, camera_index,
            aspect=_oriented_aspect(step_aspect, orientation),
        )
        _, output_pixel_nm = _recorded_output_scale(scan, oriented, orientation, pixel_nm)
        return cls(oriented, scan, orientation, output_pixel_nm, camera_index)


class IncrementalFastAccumulator:
    """Extract each incoming frame once; accumulate its unified Fast pixels.

    Exact/commensurate placement maintains weighted pixel sums. For genuine
    interpolating placement the *same* gridding method runs on the extracted
    samples currently available; no silent conversion to direct placement.
    Only the preview solve is repeated, not Gaussian extraction.
    """

    def __init__(self, calibrated: IncrementalFastGeometry):
        self.calibrated = calibrated
        geometry = calibrated.geometry
        gx, gy = _raster_coordinates(geometry, calibrated.camera_index)
        valid = np.asarray(geometry.operator.valid, dtype=bool)
        if not np.any(valid):
            raise Incompatible("No valid Gaussian foci for incremental Fast placement")
        self._valid = valid
        self._focus_count = int(valid.sum())
        self._gx = np.asarray(gx[:, valid], dtype=float)
        self._gy = np.asarray(gy[:, valid], dtype=float)
        self._placement = prepare_placement(
            self._gx, self._gy, geometry.raster.shape,
            tolerance=geometry.params.commensurate_tolerance,
            smoothing=geometry.params.smoothing,
            mode=geometry.params.placement_mode,
        )
        self._size = int(np.prod(geometry.raster.shape))
        self.reset()

    @property
    def frames_received(self):
        return self._received

    @property
    def shape(self):
        return tuple(self.calibrated.geometry.raster.shape)

    def reset(self):
        self._received = 0
        self._sum = np.zeros(self._size, dtype=np.float64)
        self._count = np.zeros(self._size, dtype=np.int64)
        self._values = [] if self._placement.method != "exact" else None
        # Retain the *fitted coefficients*, never the original camera frames.
        # One list entry per bounded worker chunk, assembled only once at T end.
        self._extracted_batches: list[ExtractionResult] = []

    def push(self, frames):
        frames = np.asarray(frames)
        if frames.ndim != 3 or frames.shape[1:] != self.calibrated.geometry.operator.frame_shape:
            raise ValueError("Incremental Fast frame shape changed")
        count = int(frames.shape[0])
        if not count:
            return
        if self._received + count > len(self.calibrated.camera_index):
            raise ValueError("Incremental Fast received more frames than scan geometry declares")
        extracted = self.calibrated.geometry.operator.apply_device(
            frames, self.calibrated.geometry.params.device,
        )
        self._extracted_batches.append(extracted)
        amplitudes = np.asarray(extracted.amplitude[:, self._valid], dtype=np.float64)
        if self._placement.method == "exact":
            first = self._received * self._focus_count
            last = (self._received + count) * self._focus_count
            pixels = np.asarray(self._placement.flat_pixel[first:last], dtype=np.int64)
            values = amplitudes.reshape(-1)
            keep = (pixels >= 0) & np.isfinite(values)
            self._sum += np.bincount(pixels[keep], weights=values[keep], minlength=self._size)
            self._count += np.bincount(pixels[keep], minlength=self._size)
        else:
            # Partial gridding is derived from already extracted amplitudes.
            # The completed scan uses reconstruct_stack() on those same fits.
            self._values.append(amplitudes)
        self._received += count

    def image(self):
        if self._received <= 0:
            return None
        if self._placement.method == "exact":
            with np.errstate(divide="ignore", invalid="ignore"):
                values = np.where(self._count > 0, self._sum / self._count, np.nan)
            return values.reshape(self.shape).astype(np.float32)
        amplitudes = np.concatenate(self._values, axis=0)
        current = prepare_placement(
            self._gx[:self._received], self._gy[:self._received], self.shape,
            smoothing=self.calibrated.geometry.params.smoothing,
            mode="interpolate",
        )
        try:
            return np.asarray(current.place(amplitudes).image, dtype=np.float32)
        except (ValueError, RuntimeError, np.linalg.LinAlgError):
            # Early partial scans may be underdetermined for cubic gridding.
            # No fallback to nearest placement: show nothing until the next
            # preview opportunity, but keep the actual stream alive.
            return None

    def finalize_stack(self):
        """Complete one scan using its retained extraction; no second GPU/CPU fit.

        The canonical finite pipeline handles final placement, background and
        diagnostics. Fast streaming capability excludes corrections requiring
        the original raw frames, so passing ``frames=None`` is intentional.
        A partial or overlong scan cannot become a scientific result.
        """
        expected = len(self.calibrated.camera_index)
        if self._received != expected:
            raise ValueError(
                f"Cannot finalize {self._received}/{expected} Fast scan frames"
            )
        if not self._extracted_batches:
            raise ValueError("No extracted Fast frames to finalize")
        batches = self._extracted_batches

        def concatenate(field):
            parts = [getattr(batch, field) for batch in batches]
            if all(part is None for part in parts):
                return None
            if any(part is None for part in parts):
                raise ValueError(f"Inconsistent extraction layer {field!r}")
            return np.concatenate(parts, axis=0)

        extracted = ExtractionResult(
            amplitude=concatenate("amplitude"),
            background=concatenate("background"),
            haze=concatenate("haze"),
            noise_sigma=concatenate("noise_sigma"),
            coefficients=concatenate("coefficients"),
        )
        return reconstruct_stack(
            None, self.calibrated.geometry, self.calibrated.camera_index,
            extracted=extracted,
        )
