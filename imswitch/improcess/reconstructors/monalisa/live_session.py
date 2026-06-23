"""MoNaLISA live streaming reconstruction session."""

import numpy as np

from imswitch.imcommon.model import initLogger
from imswitch.improcess.reconstructors.base import StreamingSession, StreamPlan

from .gauss_processor import make_gauss_processor
from .localizer import localizer
from .result import MonalisaProcessingResult
from .scan_geometry import get_orientation

try:
    import cupy as cp

    CUPY_AVAILABLE = True
except ImportError:
    CUPY_AVAILABLE = False
    cp = None


class MonalisaLiveSession(StreamingSession):
    """
    Streaming reconstruction session for MoNaLISA fast-Gauss live pipeline.

    Implements the StreamingSession contract for chunk-by-chunk reconstruction
    of MoNaLISA SIM data.
    """

    def __init__(self):
        self._logger = initLogger("MonalisaLiveSession")
        self.processor = None
        self.reconstructed = None
        self.nx_s = None
        self.ny_s = None
        self.nx_c = None
        self.ny_c = None
        self.num_frames_in_stack = None
        self.use_gpu = False
        self.name = ""
        self.scan_params = {}

    def begin(self, init_obj, params: dict) -> StreamPlan:
        """
        Inspect the first frames, allocate state, and return the output plan.

        Args:
            init_obj: StreamInit object containing the first chunk and attrs.
            params: Parameter dict (currently unused for fast-Gauss).

        Returns:
            StreamPlan describing the output shape and metadata.
        """
        self.name = init_obj.name
        data = init_obj.data
        attrs = init_obj.attrs

        self.use_gpu = params.get("use_gpu", False)

        self._logger.info(f"Beginning live session for {self.name}")

        if data.ndim != 3:
            raise ValueError(f"Expected 3D data (frames, rows, cols), got shape {data.shape}")

        # Scan geometry may arrive either nested under an "ImswitchData" attr
        # (upstream Zarr layout) or flattened to top-level "ScanStage:*" /
        # "Rec:*" keys (ImSwitch2 ZarrLiveSource / Hdf5LiveSource flatten the
        # structured metadata groups). Accept both.
        imswitch_meta = attrs.get("ImswitchData") or attrs
        if "ScanStage:axis_startpos" not in imswitch_meta:
            raise ValueError(
                "Missing ScanStage scan-geometry attrs; cannot derive scan geometry"
            )

        try:
            axis_startpos = np.array(imswitch_meta["ScanStage:axis_startpos"]).flatten()
            x0, y0, _ = axis_startpos
            x1, y1, _ = imswitch_meta["ScanStage:axis_length"]
            dx, dy, _ = imswitch_meta["ScanStage:axis_step_size"]
            self.nx_s = int(np.ceil((x1 - x0) / dx)) + 1
            self.ny_s = int(np.ceil((y1 - y0) / dy)) + 1
            
            # Prefer recording:num_timepoints, fallback to Rec:LapseTime, then 1
            num_time_points = imswitch_meta.get("recording:num_timepoints")
            if num_time_points is not None:
                num_time_points = max(1, int(num_time_points))
            else:
                num_time_points = imswitch_meta.get("Rec:LapseTime", 1)
        except KeyError as e:
            raise ValueError(f"Missing required scan geometry key: {e}") from e

        self.num_frames_in_stack = self.nx_s * self.ny_s

        self._logger.info(
            f"Scan geometry: nx_s={self.nx_s}, ny_s={self.ny_s}, "
            f"frames_per_stack={self.num_frames_in_stack}, timepoints={num_time_points}"
        )

        loc_result = localizer(data)
        self.nx_c = loc_result.nx_c
        self.ny_c = loc_result.ny_c

        self._logger.info(
            f"Localized: xp={loc_result.xp:.2f}, xo={loc_result.xo:.2f}, "
            f"yp={loc_result.yp:.2f}, yo={loc_result.yo:.2f}, "
            f"nx_c={loc_result.nx_c}, ny_c={loc_result.ny_c}"
        )

        num_rects = params.get("num_rects", 4)
        self.processor = make_gauss_processor(
            loc_result.xp,
            loc_result.xo,
            loc_result.yp,
            loc_result.yo,
            loc_result.nx_c,
            loc_result.ny_c,
            self.nx_s,
            self.ny_s,
            loc_result.num_rows,
            loc_result.num_cols,
            num_rects=num_rects,
            use_gpu=self.use_gpu,
        )

        if data.shape[0] < self.num_frames_in_stack:
            self._logger.warning(
                f"First chunk has only {data.shape[0]} frames, need {self.num_frames_in_stack} "
                f"for orientation detection; using default orientation '+x+y'"
            )
            orientation = "+x+y"
        else:
            chunk_for_orientation = data[: self.num_frames_in_stack]
            if self.use_gpu and CUPY_AVAILABLE:
                chunk_for_orientation = cp.array(chunk_for_orientation)
            proc_pixels = self.processor.process_chunk(chunk_for_orientation)
            orientation = get_orientation(
                loc_result.nx_c, loc_result.ny_c, self.nx_s, self.ny_s, proc_pixels
            )
            self._logger.info(f"Detected scan orientation: {orientation}")

        self.processor.update_frame_inds(
            loc_result.nx_c, loc_result.ny_c, self.nx_s, self.ny_s, orientation
        )

        recon_rows = loc_result.ny_c * self.ny_s
        recon_cols = loc_result.nx_c * self.nx_s

        self.reconstructed = np.zeros(
            (1, 1, num_time_points, 1, recon_rows, recon_cols), dtype=np.float32
        )

        self.scan_params = {
            "dimensions": ["Right-Left", "Up-Down", "Back-Front", "Timepoints"],
            "directions": ["+", "+", "+", "+"],
            "steps": [self.nx_s, self.ny_s, 1, num_time_points],
            "step_sizes": [float(dx), float(dy), 1.0, 1.0],
            "unidirectional": False,
        }

        self._logger.info(
            f"Allocated output buffer: shape {self.reconstructed.shape}, dtype {self.reconstructed.dtype}"
        )

        # begin() consumes the first chunk: scatter it into the output now so
        # the LiveReconstructionController's stream worker only pushes the
        # remaining chunks. The scatter is an idempotent index assignment, so a
        # caller that re-pushes these frames produces the same result.
        self.push(init_obj.data, 0, data.shape[0])

        out_shape = self.reconstructed.shape
        axis_labels = ["Dataset", "Base", "T", "Z", "Y", "X"]
        view_modes = []
        dtype = np.dtype(np.float32)

        return StreamPlan(
            out_shape=out_shape,
            axis_labels=axis_labels,
            view_modes=view_modes,
            dtype=dtype,
            scale_unit="nm",
        )

    def push(self, chunk: np.ndarray, start: int, end: int) -> None:
        """
        Process raw frames in the half-open range [start:end].

        Args:
            chunk: Raw frame data (num_frames, rows, cols).
            start: Starting frame index.
            end: Ending frame index (exclusive).
        """
        if self.processor is None:
            raise RuntimeError("Session not initialized; call begin() first")

        chunk_gpu = cp.array(chunk) if self.use_gpu and CUPY_AVAILABLE else chunk
        proc_pixels = self.processor.process_chunk(chunk_gpu)

        pixel_indices = self.processor.frame_inds[start:end]

        time_index = start // self.num_frames_in_stack
        if time_index >= self.reconstructed.shape[2]:
            self._logger.warning(
                f"Time index {time_index} exceeds allocated timepoints; skipping chunk"
            )
            return

        flat_recon = self.reconstructed[0, 0, time_index, 0].reshape(-1)
        flat_recon[pixel_indices.ravel()] = proc_pixels.ravel()

    def result(self) -> MonalisaProcessingResult:
        """
        Return a snapshot of the current reconstruction.

        Returns:
            MonalisaProcessingResult wrapping the current output buffer.
        """
        if self.reconstructed is None:
            raise RuntimeError("Session not initialized; call begin() first")

        return MonalisaProcessingResult(
            name=self.name,
            data=self.reconstructed.copy(),
            scan_params=self.scan_params,
            axis_labels=["Dataset", "Base", "T", "Z", "Y", "X"],
        )

    def close(self) -> None:
        """Free optional GPU resources."""
        if self.use_gpu and CUPY_AVAILABLE:
            if hasattr(self.processor, "x_interp"):
                del self.processor.x_interp
                del self.processor.y_interp
                del self.processor.lsq_weights
            cp.get_default_memory_pool().free_all_blocks()


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
