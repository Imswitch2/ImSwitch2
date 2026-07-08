"""SMLM live streaming localization session."""

import numpy as np

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.localization_schema import empty_localizations
from imswitch.improcess.reconstructors.base import StreamingSession, StreamPlan
from imswitch.improcess.model.result import ViewMode

from .localizer import localize_stack


class SmlmLiveSession(StreamingSession):
    """
    Streaming localization session for SMLM live reconstruction.

    Accumulates localizations from incoming frame chunks during a live recording,
    building a growing localization table. Each call to result() returns a fresh
    LocalizationResult with the accumulated localizations.
    """

    def __init__(self):
        self._logger = initLogger("SmlmLiveSession")
        self._result_name = ""
        self._source_name = ""
        self._source_shape = None
        self._pixel_size_nm = 1.0
        self._threshold = 500.0
        self._roi = 7
        self._sigma = 1.0
        self._method = "gausslq"
        self._localization_chunks = []

    def begin(self, init_obj, params: dict) -> StreamPlan:
        """
        Inspect the first frames, localize them, and return the output plan.

        Args:
            init_obj: StreamInit object containing the first chunk and attrs.
            params: Parameter dict with detection/fit params (threshold, roi,
                sigma, method, pixel_size_nm).

        Returns:
            StreamPlan describing the preview output shape and metadata.
        """
        self._result_name = f"{init_obj.name} localizations"
        self._source_name = init_obj.name
        
        data = init_obj.data
        if data.ndim < 2:
            raise ValueError(f"Expected at least 2D data, got shape {data.shape}")
        
        self._source_shape = tuple(data.shape[-2:])
        
        # Extract and store detection/fit parameters
        self._pixel_size_nm = float(params.get("pixel_size_nm", 1.0) or 1.0)
        self._threshold = float(params.get("threshold", 500.0))
        self._roi = int(params.get("roi", 7))
        self._sigma = float(params.get("sigma", 1.0))
        self._method = str(params.get("method", "gausslq"))

        self._logger.info(
            f"Beginning live SMLM session for {self._result_name} "
            f"(threshold={self._threshold}, roi={self._roi}, "
            f"method={self._method}, pixel_size={self._pixel_size_nm} nm)"
        )

        # Localize the init stack (global frame indices starting at 0)
        locs = localize_stack(
            data,
            threshold=self._threshold,
            roi=self._roi,
            sigma=self._sigma,
            method=self._method,
            pixel_size_nm=self._pixel_size_nm,
        )
        
        if len(locs) > 0:
            self._localization_chunks.append(locs)
        
        self._logger.debug(
            f"Init stack: {data.shape[0]} frames, {len(locs)} localizations"
        )

        # Return a preview-shaped plan: the LocalizationResult will build its
        # own lazy histogram with axis_labels=["Y","X"], scale_unit="nm"
        preview_shape = (256, 256)  # placeholder; result builds the real preview
        
        return StreamPlan(
            out_shape=preview_shape,
            axis_labels=["Y", "X"],
            view_modes=[ViewMode("Preview", (0, 1))],
            dtype=np.dtype(np.float32),
            scale_unit="nm",
        )

    def push(self, chunk: np.ndarray, start: int, end: int) -> None:
        """
        Localize raw frames in the half-open range [start:end).

        The chunk's frame indices are offset by start so the accumulated table
        uses global frame numbers.

        Args:
            chunk: Raw frames (Nframes x H x W).
            start: Global start frame index (inclusive).
            end: Global end frame index (exclusive).
        """
        if chunk.shape[0] == 0:
            return
        
        locs = localize_stack(
            chunk,
            threshold=self._threshold,
            roi=self._roi,
            sigma=self._sigma,
            method=self._method,
            pixel_size_nm=self._pixel_size_nm,
        )
        
        if len(locs) > 0:
            # Offset the frame column by the global start index
            locs["frame"] = locs["frame"] + start
            self._localization_chunks.append(locs)
        
        self._logger.debug(
            f"Chunk [{start}:{end}): {chunk.shape[0]} frames, {len(locs)} localizations"
        )

    def result(self) -> LocalizationResult:
        """
        Return a snapshot of the current localization result.

        Returns:
            LocalizationResult wrapping the concatenation of all accumulated
            localizations. Safe to hand to the UI thread (fresh recarray each call).
        """
        if not self._localization_chunks:
            locs = empty_localizations(0)
        else:
            # Concatenate all accumulated recarrays
            locs = np.concatenate(self._localization_chunks, axis=0)
            # Make a fresh copy for cross-thread safety
            locs = locs.copy()

        return LocalizationResult(
            name=self._result_name,
            locs=locs,
            pixel_size_nm=self._pixel_size_nm,
            dims="2D",
            source_name=self._source_name,
            source_shape=self._source_shape,
            metadata={
                "threshold": self._threshold,
                "roi": self._roi,
                "fit_method": self._method,
            },
        )

    def finish(self) -> LocalizationResult:
        """
        Finalize processing and return the final result.

        Returns:
            LocalizationResult with the complete localization table.
        """
        result = self.result()
        self._logger.info(
            f"SMLM live session finished: {result.count} total localizations"
        )
        return result

    def close(self) -> None:
        """Free accumulated localization data."""
        self._localization_chunks.clear()


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
