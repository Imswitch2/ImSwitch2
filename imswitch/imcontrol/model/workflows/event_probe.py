"""Event probe primitives for event-gated target acquisition.

Phase T4 of the tiled-target-timelapse-smart-events plan. Provides model-level
event detection service that can be used by TargetTimelapseWorkflow without
importing Qt/controllers/widgets. Reuses EtSTEDPipelineResult shape where
practical but does not couple to EventTriggeredControllerBase.

EventProbe evaluates a frame source through a detector/pipeline for up to
max_frames after optional min_warmup_frames and returns detected true/false plus
frames_seen, first detected frame, first coordinate, all coordinates, analysis
metadata, and errors.

EventGatedAcquisitionCallback wraps EventProbe and a positive acquisition callback
for use as TargetTimelapseWorkflow acquisition_callback. Probes target; if no
event, returns negative metadata and does not call positive acquisition; if event
detected, calls positive acquisition exactly once and returns metadata including
probe result and acquisition result. No-event is workflow success with skipped
acquisition metadata.

Copyright (C) 2020-2026 ImSwitch developers
This file is part of ImSwitch.

ImSwitch is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

ImSwitch is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional, Tuple

import numpy as np

if TYPE_CHECKING:
    from imswitch.imcontrol.model.workflows.target import Target

logger = logging.getLogger(__name__)


@dataclass
class EventProbeParams:
    """Parameters for event probing.

    Attributes:
        max_frames: Maximum number of frames to evaluate for event detection.
            Must be >= 1.
        min_warmup_frames: Minimum number of warmup frames before starting
            detection. Detector is called for all frames, but events detected
            during warmup are ignored. Defaults to 0.
    """

    max_frames: int
    min_warmup_frames: int = 0

    def __post_init__(self):
        if self.max_frames < 1:
            raise ValueError(f"max_frames must be >= 1, got {self.max_frames}")
        if self.min_warmup_frames < 0:
            raise ValueError(
                f"min_warmup_frames must be >= 0, got {self.min_warmup_frames}"
            )
        if self.min_warmup_frames >= self.max_frames:
            raise ValueError(
                f"min_warmup_frames ({self.min_warmup_frames}) must be < "
                f"max_frames ({self.max_frames})"
            )


@dataclass
class EventProbeResult:
    """Result from event probe evaluation.

    Attributes:
        detected: True if event was detected after warmup period.
        frames_seen: Total number of frames evaluated.
        first_detected_frame: Zero-based frame index where the first event was
            detected after warmup, or None if no event detected.
        first_coord: First detected coordinate (row, col) after warmup, or None
            if no event detected.
        all_coords: All detected coordinates (N, 2) array after warmup. Empty
            (0, 2) array if no event detected.
        analysis_metadata: Arbitrary metadata from detector/pipeline, or None.
        error: Exception message if probe failed, else None.
    """

    detected: bool
    frames_seen: int
    first_detected_frame: Optional[int] = None
    first_coord: Optional[Tuple[float, float]] = None
    all_coords: np.ndarray = field(default_factory=lambda: np.empty((0, 2)))
    analysis_metadata: Any = None
    error: Optional[str] = None


class EventProbe:
    """Model-level event detection service for target probing.

    Evaluates a frame source through a detector/pipeline for up to max_frames
    after optional min_warmup_frames. Returns detected true/false plus frames_seen,
    first detected frame, first coordinate, all coordinates, analysis metadata,
    and errors.

    Does not import Qt/controllers/widgets. Reuses EtSTEDPipelineResult shape
    where practical but does not couple to EventTriggeredControllerBase.

    Args:
        frame_source: Callable returning one frame per call. Signature:
            ``f(target, timepoint, params) -> np.ndarray``.
            Should raise StopIteration when no more frames are available.
            Should raise on acquisition/hardware errors.
        detector: Callable running detection/pipeline on each frame. Signature:
            ``f(frame, frame_idx, params) -> dict``.
            Must return dict with 'coords_detected' (np.ndarray) and optionally
            'analysis_metadata' (any) and 'analysis_image' (np.ndarray).
            coords_detected should be (2,) or (N, 2) array, or None/empty if
            no event detected. Should raise on detection/pipeline errors.
        params: EventProbeParams configuration.
    """

    def __init__(
        self,
        frame_source: Callable[[Target, int, EventProbeParams], np.ndarray],
        detector: Callable[[np.ndarray, int, EventProbeParams], dict],
        params: EventProbeParams,
    ) -> None:
        self.frame_source = frame_source
        self.detector = detector
        self.params = params

    def probe(self, target: Target, timepoint: int) -> EventProbeResult:
        """Probe target for event detection.

        Args:
            target: Target instance from TargetList.
            timepoint: Zero-based timepoint index.

        Returns:
            EventProbeResult with detection status and metadata.
        """
        frames_seen = 0
        first_detected_frame = None
        first_coord = None
        all_coords = []
        analysis_metadata = None
        error = None
        detected = False

        try:
            for frame_idx in range(self.params.max_frames):
                try:
                    frame = self.frame_source(target, timepoint, self.params)
                    frames_seen += 1
                except StopIteration:
                    logger.debug(
                        "Target %d timepoint %d: frame source stopped at frame %d/%d",
                        target.id,
                        timepoint,
                        frame_idx,
                        self.params.max_frames,
                    )
                    break

                detection_result = self.detector(frame, frame_idx, self.params)
                coords_detected = detection_result.get("coords_detected")
                analysis_metadata = detection_result.get("analysis_metadata")

                coords_normalized = self._normalize_coords(coords_detected)

                if frame_idx >= self.params.min_warmup_frames:
                    if coords_normalized.size > 0:
                        if first_coord is None:
                            first_detected_frame = frame_idx
                            first_coord = (
                                float(coords_normalized[0, 0]),
                                float(coords_normalized[0, 1]),
                            )
                            detected = True
                        all_coords.append(coords_normalized)

        except Exception as exc:
            error = str(exc)
            logger.warning(
                "Target %d timepoint %d: probe failed after %d frames - %s",
                target.id,
                timepoint,
                frames_seen,
                error,
            )

        all_coords_array = (
            np.vstack(all_coords) if all_coords else np.empty((0, 2))
        )

        return EventProbeResult(
            detected=detected,
            frames_seen=frames_seen,
            first_detected_frame=first_detected_frame,
            first_coord=first_coord,
            all_coords=all_coords_array,
            analysis_metadata=analysis_metadata,
            error=error,
        )

    def _normalize_coords(self, coords_detected) -> np.ndarray:
        """Normalize detected coordinates to (N, 2) array."""
        if coords_detected is None:
            return np.empty((0, 2))
        coords = np.asarray(coords_detected, dtype=float)
        if coords.size == 0:
            return np.empty((0, 2))
        if coords.shape == (2,):
            return coords.reshape(1, 2)
        if coords.ndim != 2 or coords.shape[1] != 2:
            raise RuntimeError(
                f"Detector returned coordinates with shape {coords.shape}; "
                "expected (2,) or (N, 2)."
            )
        return coords


@dataclass
class EventGatedAcquisitionResult:
    """Result from event-gated acquisition callback.

    Attributes:
        probe_result: EventProbeResult from event probing.
        acquisition_triggered: True if positive acquisition was called.
        acquisition_result: Return value from positive acquisition callback
            if triggered, else None.
        acquisition_error: Exception message if positive acquisition failed,
            else None.
    """

    probe_result: EventProbeResult
    acquisition_triggered: bool
    acquisition_result: Any = None
    acquisition_error: Optional[str] = None


class EventGatedAcquisitionCallback:
    """Acquisition callback wrapper for event-gated target acquisition.

    Usable as TargetTimelapseWorkflow acquisition_callback. Probes target; if
    no event, returns negative metadata and does not call positive acquisition;
    if event detected, calls positive acquisition exactly once and returns
    metadata including probe result and acquisition result.

    No-event is workflow success with skipped acquisition metadata (not a
    failure). Probe errors raise by default so the parent workflow can apply
    its failure policy; callers can opt into treating probe errors as skipped
    acquisitions for exploratory workflows.

    Args:
        probe: EventProbe instance for event detection.
        positive_acquisition: Callable invoked when event is detected. Signature:
            ``f(target, timepoint, probe_result) -> Any``.
            Should raise on acquisition errors. Return value is recorded in
            EventGatedAcquisitionResult.acquisition_result.
        fail_on_probe_error: If True, raise RuntimeError when probing fails. If
            False, return a skipped acquisition result with the probe error
            recorded.
    """

    def __init__(
        self,
        probe: EventProbe,
        positive_acquisition: Callable[[Target, int, EventProbeResult], Any],
        *,
        fail_on_probe_error: bool = True,
    ) -> None:
        self.probe = probe
        self.positive_acquisition = positive_acquisition
        self.fail_on_probe_error = fail_on_probe_error

    def __call__(self, target: Target, timepoint: int) -> EventGatedAcquisitionResult:
        """Probe target and conditionally trigger positive acquisition.

        Signature matches TargetTimelapseWorkflow acquisition_callback.

        Args:
            target: Target instance from TargetList.
            timepoint: Zero-based timepoint index.

        Returns:
            EventGatedAcquisitionResult with probe and acquisition metadata.
            Raises on positive acquisition failure and, by default, probe
            failure.
        """
        probe_result = self.probe.probe(target, timepoint)

        if probe_result.error:
            message = f"Event probe failed: {probe_result.error}"
            if self.fail_on_probe_error:
                logger.error(
                    "Target %d timepoint %d: %s",
                    target.id,
                    timepoint,
                    message,
                )
                raise RuntimeError(message)
            logger.warning(
                "Target %d timepoint %d: probe failed - %s",
                target.id,
                timepoint,
                probe_result.error,
            )
            return EventGatedAcquisitionResult(
                probe_result=probe_result,
                acquisition_triggered=False,
            )

        if not probe_result.detected:
            logger.info(
                "Target %d timepoint %d: no event detected (%d frames) - skipping acquisition",
                target.id,
                timepoint,
                probe_result.frames_seen,
            )
            return EventGatedAcquisitionResult(
                probe_result=probe_result,
                acquisition_triggered=False,
            )

        logger.info(
            "Target %d timepoint %d: event detected at frame %d - triggering acquisition",
            target.id,
            timepoint,
            probe_result.first_detected_frame,
        )

        acquisition_result = None
        acquisition_error = None

        try:
            acquisition_result = self.positive_acquisition(
                target, timepoint, probe_result
            )
        except Exception as exc:
            acquisition_error = str(exc)
            logger.error(
                "Target %d timepoint %d: positive acquisition failed - %s",
                target.id,
                timepoint,
                acquisition_error,
            )
            raise

        return EventGatedAcquisitionResult(
            probe_result=probe_result,
            acquisition_triggered=True,
            acquisition_result=acquisition_result,
            acquisition_error=acquisition_error,
        )
