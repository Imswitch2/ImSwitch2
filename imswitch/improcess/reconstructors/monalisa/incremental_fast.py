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

from dataclasses import dataclass, replace

import numpy as np

from imswitch.improcess.reconstructors.base import Incompatible
from .pipeline import (
    _oriented_aspect, _raster_coordinates, geometry_with_step, prepare_geometry,
    reconstruct_stack, resolve_scan_frame_gain,
)
from .extraction import ExtractionResult
from .placement import prepare_placement
from .scan_frame import orient_recorded_index, orient_step_columns
from imswitch.improcess.reconstructors.monalisa_lattice.reconstructor import (
    _recorded_output_scale, pipeline_params, recorded_geometry_inputs,
)
from imswitch.improcess.reconstructors.monalisa_lattice.scan import (
    layout_scan, timepoint_group_view, validated_timepoint_groups,
)
from .calibration import PatternLocalization


@dataclass(frozen=True)
class IncrementalFastGeometry:
    """One specified Gaussian calibration and a group-local scan mapping."""

    geometry: object
    scan: object
    orientation: str
    output_pixel_nm: object
    camera_index: np.ndarray
    source_scan: object = None

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
        source_scan = layout_scan(resolved_layout, int(frames_per_timepoint))
        if source_scan is None:
            raise Incompatible("Live Fast needs a recorded acquisition layout")
        try:
            validated_timepoint_groups(source_scan, int(frames_per_timepoint))
        except ValueError as exc:
            raise Incompatible(str(exc)) from exc
        source_scan, step, step_aspect, pixel_nm = recorded_geometry_inputs(source_scan, local_params)
        scan = timepoint_group_view(source_scan, 0)
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
        return cls(oriented, scan, orientation, output_pixel_nm, camera_index, source_scan)

    def for_group(self, group: int) -> "IncrementalFastGeometry":
        """Share the extraction operator/localization with another recorded plane.

        Group-local indices (including reversed or interleaved traversal) are
        the only differences. The scientific model is calibrated once for the
        entire logical acquisition, as in finite recorded reconstruction.
        """
        source = self.source_scan or self.scan
        if source.num_stacks == 1 and int(group) == 0:
            return self
        plane = timepoint_group_view(source, int(group))
        camera_index = orient_recorded_index(plane.index, self.orientation)
        oriented = geometry_with_step(
            self.geometry, self.geometry.step, camera_index,
            aspect=self.geometry.diagnostics.get("step_aspect"),
        )
        if tuple(oriented.raster.shape) != tuple(self.geometry.raster.shape):
            raise Incompatible("Live Fast planes have incompatible output rasters")
        return replace(self, geometry=oriented, scan=plane, camera_index=camera_index)


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
        diagnostics.  Timepoint-level intensity and cell-offset corrections
        use these retained coefficients; no raw frames or second solve are
        required. Reassignment and pinholes remain explicitly unsupported.
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
        geometry = self.calibrated.geometry
        scan = self.calibrated.scan
        # These are chronological scan coordinates, NOT necessarily the
        # oriented physical index: bidirectional traversal must agree with
        # the finite reconstructor's gain_positions(stack).
        gain, gain_diagnostics = resolve_scan_frame_gain(
            extracted, geometry.operator.valid, scan.in_plane_shape,
            scan.gain_positions(0), geometry.params.frame_gain,
        )
        result = reconstruct_stack(
            None, geometry, self.calibrated.camera_index,
            frame_gain=gain, extracted=extracted,
        )
        from dataclasses import replace
        return replace(
            result, diagnostics={**gain_diagnostics, **result.diagnostics},
        )


class IncrementalFastMultiAccumulator:
    """Route one timepoint's frames to independent in-plane Fast accumulators.

    The router owns the global frame cursor; each child receives its group's
    subsequence in exact recorded order, with no frame copied to another Z
    plane/condition. Each camera frame is Gaussian-extracted exactly once.
    """

    def __init__(self, calibrated: IncrementalFastGeometry):
        self.calibrated = calibrated
        self.scan = calibrated.source_scan or calibrated.scan
        self._group_indices = validated_timepoint_groups(self.scan, len(self.scan.stack))
        self._groups = [
            IncrementalFastAccumulator(calibrated.for_group(group))
            for group in range(self.scan.num_stacks)
        ]
        self.shape = (
            *(count for count in (self.scan.num_conditions, self.scan.num_planes) if count > 1),
            *self._groups[0].shape,
        )
        self._placement = self._groups[0]._placement
        self.reset()

    def reset(self):
        self._received = 0
        for group in self._groups:
            group.reset()
        # Keep one in-progress spatial buffer. Building a fresh stack on
        # every viewer refresh costs O(Z*Y*X) allocations in 3D.
        self._preview_volume = None
        self._preview_dirty = set(range(len(self._groups)))
        self._preview_has_pixels = False

    @property
    def frames_received(self):
        return self._received

    def push(self, frames):
        frames = np.asarray(frames)
        if frames.ndim != 3:
            raise ValueError("Incremental Fast expects (frame, camera_y, camera_x)")
        stop = self._received + len(frames)
        if stop > len(self.scan.stack):
            raise ValueError("More frames than the declared multi-plane scan")
        route = np.asarray(self.scan.stack[self._received:stop], dtype=int)
        for group in np.unique(route):
            local = np.flatnonzero(route == group)
            accumulator = self._groups[int(group)]
            expected = self._group_indices[int(group)][
                accumulator.frames_received:accumulator.frames_received + len(local)
            ]
            if not np.array_equal(expected, self._received + local):
                raise ValueError("Live Fast frames arrived out of their recorded group order")
            accumulator.push(frames[local])
            self._preview_dirty.add(int(group))
        self._received = stop

    def image(self):
        """A reusable Z/C volume, with unmeasured pixels represented by NaN.

        Return None until an actual pixel is reconstructable. Interpolating
        placement can need several frames before its first valid partial
        solve; this must not prevent first-frame geometry initialization.
        """
        if not self._received:
            return None
        if self._preview_volume is None:
            self._preview_volume = np.full(self.shape, np.nan, dtype=np.float32)
        groups = self._preview_volume.reshape(len(self._groups), *self._groups[0].shape)
        for group in sorted(self._preview_dirty):
            pixels = self._groups[group].image()
            if pixels is None:
                groups[group].fill(np.nan)
            else:
                groups[group] = pixels
                if not self._preview_has_pixels and np.isfinite(pixels).any():
                    self._preview_has_pixels = True
        self._preview_dirty.clear()
        return self._preview_volume if self._preview_has_pixels else None

    def finalize_stacks(self):
        if self._received != len(self.scan.stack):
            raise ValueError("Cannot finalize an incomplete multi-plane timepoint")
        return {group: acc.finalize_stack() for group, acc in enumerate(self._groups)}
