"""MoNaLISA live streaming reconstruction session."""

import numpy as np

from imswitch.imcommon.model import initLogger
from imswitch.improcess.reconstructors.base import StreamingSession, StreamPlan

from .gauss_processor import (
    DEFAULT_FOOTPRINT_NUM_RECTS,
    DEFAULT_GAUSSIAN_SIGMA_PX,
    make_gauss_processor,
)
from .localizer import localization_from_pattern, localizer
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
        self.bleaching_correction = False
        self._bleach_reference_energy = None
        self.name = ""
        self.scan_params = {}
        self.output_pixel_size_nm = None

    def begin(self, init_obj, params: dict) -> StreamPlan:
        """
        Inspect the first frames, allocate state, and return the output plan.

        Args:
            init_obj: StreamInit object containing the first chunk and attrs.
            params: Parameter dict for the fast-Gauss path.

        Returns:
            StreamPlan describing the output shape and metadata.
        """
        self.name = init_obj.name
        data = init_obj.data
        attrs = init_obj.attrs

        explicit_use_gpu = params.get("use_gpu")
        self.use_gpu = (
            bool(explicit_use_gpu)
            if explicit_use_gpu is not None
            else str(params.get("device", "")).lower() == "gpu"
        )
        self.bleaching_correction = bool(params.get("bleaching_correction", False))
        self._bleach_reference_energy = None

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
            axis_length = np.array(imswitch_meta["ScanStage:axis_length"]).flatten()
            axis_step_size = np.array(imswitch_meta["ScanStage:axis_step_size"]).flatten()
            step_x_nm, step_y_nm = self.scan_stage_step_size_nm(
                imswitch_meta, axis_step_size
            )
            self.nx_s, self.ny_s = self._resolve_scan_steps(
                axis_startpos,
                axis_length,
                axis_step_size,
                imswitch_meta,
                init_obj.stack_info,
                data.shape[0],
            )
        except KeyError as e:
            raise ValueError(f"Missing required scan geometry key: {e}") from e

        self.num_frames_in_stack = self.nx_s * self.ny_s
        num_time_points = self._resolve_num_timepoints(imswitch_meta, init_obj.stack_info)

        self._logger.info(
            f"Scan geometry: nx_s={self.nx_s}, ny_s={self.ny_s}, "
            f"frames_per_stack={self.num_frames_in_stack}, timepoints={num_time_points}"
        )

        working_data = (
            self._apply_bleaching_correction(data)
            if self.bleaching_correction else data
        )

        loc_result = self._resolve_localization(working_data, params)
        self.nx_c = loc_result.nx_c
        self.ny_c = loc_result.ny_c

        self._logger.info(
            f"Localized: xp={loc_result.xp:.2f}, xo={loc_result.xo:.2f}, "
            f"yp={loc_result.yp:.2f}, yo={loc_result.yo:.2f}, "
            f"nx_c={loc_result.nx_c}, ny_c={loc_result.ny_c}"
        )

        num_rects = params.get(
            "fast_gauss_footprint_num_rects",
            params.get("num_rects", DEFAULT_FOOTPRINT_NUM_RECTS),
        )
        gaussian_sigma_px = self._resolve_gaussian_sigma_px(params)
        pinhole_radius_px = self._resolve_pinhole_radius_px(params, gaussian_sigma_px)
        fit_background = self._resolve_fit_background(params)
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
            gaussian_sigma_px=gaussian_sigma_px,
            pinhole_radius_px=pinhole_radius_px,
            fit_background=fit_background,
            use_gpu=self.use_gpu,
        )

        if working_data.shape[0] < self.num_frames_in_stack:
            self._logger.warning(
                f"First chunk has only {working_data.shape[0]} frames, need {self.num_frames_in_stack} "
                f"for orientation detection; using default orientation '+x+y'"
            )
            orientation = "+x+y"
        else:
            chunk_for_orientation = working_data[: self.num_frames_in_stack]
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
        # The reconstructed pixel pitch is the scan step size: the recon buffer
        # is (ny_c*ny_s, nx_c*nx_s), with the ny_c/nx_c foci tiling adjacent
        # illumination periods and the ny_s/nx_s scan steps filling within each
        # period — so one output pixel == one scan step. (Dividing by the focus
        # count made the napari scale ~ny_c/nx_c too small.)
        self.output_pixel_size_nm = (step_y_nm, step_x_nm)

        self.reconstructed = np.zeros(
            (1, 1, num_time_points, 1, recon_rows, recon_cols), dtype=np.float32
        )

        self.scan_params = {
            "dimensions": ["Right-Left", "Up-Down", "Back-Front", "Timepoints"],
            "directions": ["+", "+", "+", "+"],
            "steps": [self.nx_s, self.ny_s, 1, num_time_points],
            "step_sizes": [float(step_x_nm), float(step_y_nm), 1.0, 1.0],
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
        axis_scales = [1.0, 1.0, 1.0, 1.0, *self.output_pixel_size_nm]
        view_modes = []
        dtype = np.dtype(np.float32)

        return StreamPlan(
            out_shape=out_shape,
            axis_labels=axis_labels,
            view_modes=view_modes,
            dtype=dtype,
            scale_unit="nm",
            axis_scales=axis_scales,
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

        offset = 0
        current_start = int(start)
        while offset < chunk.shape[0] and current_start < end:
            local_start = current_start % self.num_frames_in_stack
            frames_left_in_stack = self.num_frames_in_stack - local_start
            frames_left_in_chunk = chunk.shape[0] - offset
            segment_length = min(frames_left_in_stack, frames_left_in_chunk)
            if segment_length <= 0:
                break

            segment = chunk[offset:offset + segment_length]
            segment_start = current_start
            segment_end = segment_start + segment_length
            self._push_single_stack_chunk(segment, segment_start, segment_end)

            offset += segment_length
            current_start = segment_end

    def _push_single_stack_chunk(self, chunk: np.ndarray, start: int, end: int) -> None:
        """Scatter a chunk that is guaranteed not to cross a scan-stack boundary."""
        # start/end are GLOBAL frame indices across timepoints (the lapse source
        # streams scan0, scan1, ... as one continuous range). frame_inds is
        # per-stack (length num_frames_in_stack), so derive the timepoint from
        # the global start and index frame_inds with the LOCAL position within
        # the stack. A chunk never spans a stack boundary (sources read within
        # one stack/group), so a single time_index applies to the whole chunk.
        time_index = start // self.num_frames_in_stack
        if time_index >= self.reconstructed.shape[2]:
            self._logger.warning(
                f"Time index {time_index} exceeds allocated timepoints; skipping chunk"
            )
            return

        local_start = start % self.num_frames_in_stack
        local_end = local_start + (end - start)

        if self.bleaching_correction:
            chunk = self._apply_bleaching_correction(chunk)

        chunk_gpu = cp.array(chunk) if self.use_gpu and CUPY_AVAILABLE else chunk
        proc_pixels = self.processor.process_chunk(chunk_gpu)
        pixel_indices = self.processor.frame_inds[local_start:local_end]

        flat_recon = self.reconstructed[0, 0, time_index, 0].reshape(-1)
        flat_recon[pixel_indices.ravel()] = proc_pixels.ravel()

    def _resolve_gaussian_sigma_px(self, params: dict) -> float:
        """Fit-Gaussian sigma in pixels.

        An explicit ``fast_gauss_gaussian_sigma_px``/``gaussian_sigma_px`` > 0
        wins; otherwise derive it from the optical PSF using the slow-path
        convention ``sigma_px = PSF_FWHM_nm / (2.355 * pixel_size_nm)`` so the
        fast and slow paths share one physical parameterization.
        """
        explicit = params.get(
            "fast_gauss_gaussian_sigma_px", params.get("gaussian_sigma_px", 0.0)
        )
        try:
            explicit = float(explicit)
        except (TypeError, ValueError):
            explicit = 0.0
        if explicit > 0:
            return explicit

        fwhm_nm = params.get("psf_fwhm_nm")
        pixel_size_nm = params.get("pixel_size_nm")
        try:
            if fwhm_nm and pixel_size_nm and float(pixel_size_nm) > 0:
                return float(fwhm_nm) / (2.355 * float(pixel_size_nm))
        except (TypeError, ValueError):
            pass
        return DEFAULT_GAUSSIAN_SIGMA_PX

    @staticmethod
    def _resolve_localization(data: np.ndarray, params: dict):
        """Use explicit widget pattern params when provided; otherwise localize.

        Live reconstruction intentionally uses automatic localization on the
        incoming data. Offline fast-Gauss reconstruction sets
        ``_monalisa_pattern_params`` so it follows the parameter widget in the
        same way as the full SignalExtractor path.
        """
        pattern = params.get("_monalisa_pattern_params")
        if pattern:
            return localization_from_pattern(
                row_offset=pattern["row_offset"],
                col_offset=pattern["col_offset"],
                row_period=pattern["row_period"],
                col_period=pattern["col_period"],
                num_rows=data.shape[-2],
                num_cols=data.shape[-1],
            )
        return localizer(data)

    @staticmethod
    def _resolve_pinhole_radius_px(params: dict, gaussian_sigma_px: float) -> float | None:
        """Circular pinhole radius = k * sigma (k from params; None disables)."""
        mode = params.get("fast_gauss_footprint_mode")
        if mode is not None and str(mode).strip().lower() != "circular pinhole":
            return None

        k = params.get("fast_gauss_pinhole_radius_sigma", 0.0)
        try:
            k = float(k)
        except (TypeError, ValueError):
            k = 0.0
        return k * gaussian_sigma_px if k > 0 else None

    @staticmethod
    def _resolve_fit_background(params: dict) -> bool:
        """Whether to fit a constant background (False = pure matched filter)."""
        bg = str(params.get("bg_modelling", "Constant")).strip().lower()
        return bg not in ("no background", "none")

    def _resolve_num_timepoints(self, metadata: dict, stack_info) -> int:
        """Resolve a positive timepoint count from recorder metadata.

        Some legacy files store missing lapse metadata as the literal string
        ``"null"``. Treat those values as absent instead of letting them reach
        the output-buffer shape.
        """
        for key in ("recording:num_timepoints", "Rec:LapseTime"):
            timepoints = self._coerce_positive_int(metadata.get(key))
            if timepoints is not None:
                return timepoints

        expected_frames = self._coerce_positive_int(
            getattr(stack_info, "expected_frames", None)
        )
        if expected_frames is not None and self.num_frames_in_stack:
            return max(1, int(np.ceil(expected_frames / self.num_frames_in_stack)))

        return 1

    def _resolve_scan_steps(
        self,
        axis_startpos: np.ndarray,
        axis_length: np.ndarray,
        axis_step_size: np.ndarray,
        metadata: dict,
        stack_info,
        init_frame_count: int,
    ) -> tuple[int, int]:
        """Resolve X/Y scan positions, accepting both scan-size conventions.

        Live ImControl metadata stores ``axis_length`` as a physical scan size
        (positions = length / step). The offline adapter historically writes an
        endpoint-style length (positions = (length - start) / step + 1). Use
        the known first-stack frame count to choose the convention that matches
        the actual data.
        """
        if axis_startpos.size < 2 or axis_length.size < 2 or axis_step_size.size < 2:
            raise ValueError("ScanStage axis metadata must contain at least X and Y")

        stack_frames = self._resolve_frames_per_stack(
            metadata,
            stack_info,
            init_frame_count,
        )
        ttl_steps = self._scan_steps_from_ttl(metadata)
        if ttl_steps is not None:
            return ttl_steps

        candidates = []

        size_steps = self._scan_steps_from_size(axis_length, axis_step_size)
        if size_steps is not None:
            candidates.append(size_steps)

        endpoint_steps = self._scan_steps_from_endpoint(
            axis_startpos, axis_length, axis_step_size
        )
        if endpoint_steps is not None:
            candidates.append(endpoint_steps)

        unique_candidates = []
        for candidate in candidates:
            if candidate not in unique_candidates:
                unique_candidates.append(candidate)

        if stack_frames is not None:
            for nx_s, ny_s in unique_candidates:
                if nx_s * ny_s == stack_frames:
                    return nx_s, ny_s

        if unique_candidates:
            return unique_candidates[-1]

        raise ValueError("Could not derive MoNaLISA scan dimensions")

    @staticmethod
    def _scan_steps_from_ttl(metadata: dict) -> tuple[int, int] | None:
        nx_s = MonalisaLiveSession._coerce_positive_int(metadata.get("ScanTTL:Nx"))
        ny_s = MonalisaLiveSession._coerce_positive_int(metadata.get("ScanTTL:Ny"))
        if nx_s is None or ny_s is None:
            return None
        return nx_s, ny_s

    @staticmethod
    def _scan_steps_from_size(
        axis_length: np.ndarray, axis_step_size: np.ndarray
    ) -> tuple[int, int] | None:
        try:
            dx, dy = float(axis_step_size[0]), float(axis_step_size[1])
            if dx == 0 or dy == 0:
                return None
            nx_s = max(1, int(np.ceil(abs(float(axis_length[0])) / abs(dx))))
            ny_s = max(1, int(np.ceil(abs(float(axis_length[1])) / abs(dy))))
            return nx_s, ny_s
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _scan_steps_from_endpoint(
        axis_startpos: np.ndarray,
        axis_length: np.ndarray,
        axis_step_size: np.ndarray,
    ) -> tuple[int, int] | None:
        try:
            dx, dy = float(axis_step_size[0]), float(axis_step_size[1])
            if dx == 0 or dy == 0:
                return None
            nx_s = max(
                1,
                int(np.ceil(abs(float(axis_length[0]) - float(axis_startpos[0])) / abs(dx))) + 1,
            )
            ny_s = max(
                1,
                int(np.ceil(abs(float(axis_length[1]) - float(axis_startpos[1])) / abs(dy))) + 1,
            )
            return nx_s, ny_s
        except (TypeError, ValueError):
            return None

    def _resolve_frames_per_stack(
        self,
        metadata: dict,
        stack_info,
        init_frame_count: int,
    ) -> int | None:
        for value in (
            metadata.get("recording:frames_per_stack"),
            getattr(stack_info, "frames_per_stack", None),
            init_frame_count,
        ):
            frames = self._coerce_positive_int(value)
            if frames is not None:
                return frames
        return None

    @staticmethod
    def scan_stage_step_size_nm(
        metadata: dict, axis_step_size: np.ndarray
    ) -> tuple[float, float]:
        """Return X/Y ScanStage step sizes in nanometers.

        ImControl scan widgets store ``ScanStage:axis_step_size`` in
        micrometers. The improcess scan-parameter dialog stores its
        ``step_sizes`` in nanometers, so callers that adapt dialog values into
        ScanStage metadata can set ``ScanStage:axis_step_size_unit = "nm"`` to
        bypass this conversion.
        """
        if axis_step_size.size < 2:
            raise ValueError("ScanStage axis_step_size must contain X and Y")

        return (
            MonalisaLiveSession._physical_step_to_nm(
                axis_step_size[0],
                MonalisaLiveSession._scan_stage_step_unit(metadata, 0),
            ),
            MonalisaLiveSession._physical_step_to_nm(
                axis_step_size[1],
                MonalisaLiveSession._scan_stage_step_unit(metadata, 1),
            ),
        )

    @staticmethod
    def _scan_stage_step_unit(metadata: dict, axis_index: int) -> str:
        for key in (
            "ScanStage:axis_step_size_unit",
            "ScanStage:axis_step_unit",
            "ScanStage:unit",
        ):
            unit = metadata.get(key)
            if unit is None:
                continue
            if isinstance(unit, (list, tuple, np.ndarray)):
                values = np.asarray(unit, dtype=object).flatten()
                if values.size == 0:
                    continue
                unit = values[min(axis_index, values.size - 1)]
            if isinstance(unit, (bytes, np.bytes_)):
                unit = unit.decode(errors="ignore")
            text = str(unit).strip()
            if text:
                return text
        return "um"

    @staticmethod
    def _physical_step_to_nm(value, unit: str) -> float:
        step = float(value)
        text = str(unit).strip().lower()
        text = text.replace("\u00b5", "u").replace("\u03bc", "u")
        if text in {"nm", "nanometer", "nanometers"} or "nano" in text:
            return step
        if (
            text in {"um", "micrometer", "micrometers", "micron", "microns"}
            or "micro" in text
        ):
            return step * 1000.0
        return step * 1000.0

    @staticmethod
    def _coerce_positive_int(value) -> int | None:
        """Return a positive integer, or None for missing/null-ish metadata."""
        if value is None:
            return None

        if isinstance(value, (list, tuple, np.ndarray)):
            array = np.asarray(value).flatten()
            if array.size != 1:
                return None
            value = array[0]

        if isinstance(value, (bytes, np.bytes_)):
            value = value.decode(errors="ignore")

        if isinstance(value, str):
            text = value.strip()
            if text.lower() in {"", "null", "none", "nan", "n/a", "na"}:
                return None
            value = text

        try:
            number = float(value)
        except (TypeError, ValueError):
            return None

        if not np.isfinite(number) or number <= 0:
            return None
        return max(1, int(number))

    def _apply_bleaching_correction(self, chunk: np.ndarray) -> np.ndarray:
        """
        Apply the MoNaLISA frame-energy bleaching correction to live frames.

        This mirrors the offline reconstructor's 4th-power normalization,
        ``(E_0 / E_i) ** 4``, while keeping the first frame's raw energy as the
        reference across later live chunks.
        """
        corrected = chunk.astype(np.float32, copy=True)
        if corrected.shape[0] == 0:
            return corrected

        energies = np.sum(corrected, axis=(1, 2), dtype=np.float64)
        if self._bleach_reference_energy is None:
            self._bleach_reference_energy = float(energies[0])

        reference_energy = self._bleach_reference_energy
        if reference_energy <= 0:
            return corrected

        safe_energies = np.where(energies > 0, energies, reference_energy)
        scale = (reference_energy / safe_energies) ** 4
        return corrected * scale[:, np.newaxis, np.newaxis].astype(np.float32)

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
            output_pixel_size_nm=self.output_pixel_size_nm,
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
