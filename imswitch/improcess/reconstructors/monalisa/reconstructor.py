"""MoNaLISA SIM reconstructor plugin."""

import copy
from typing import TYPE_CHECKING

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.reconstructors.base import StreamInit, StreamingReconstructor
from .gauss_processor import (
    _build_footprint,
    extract_lattice_amplitudes,
    validate_gaussian_fit_options,
)
from .lattice import Lattice, detect_lattice
from .lattice_recon import (
    assemble_image,
    choose_orientation,
    sample_positions,
    scan_offsets_px,
    solve_per_focus_offsets,
)
from .live_session import MonalisaLiveSession
from .localizer import detection_band
from .orientation import auto_detect_scan_orientation
from .params_widget import MonalisaParamsWidget
from .pattern_finder import PatternFinder
from .result import MonalisaProcessingResult, MonalisaSpotCloud, MonalisaSweepResult
from .signal_extractor import SignalExtractor
from .sweep import SWEEPABLE_PARAMETERS, parse_sweep_values, resolve_sweep_parameter

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


class MonalisaReconstructor(StreamingReconstructor):
    """
    MoNaLISA structured illumination microscopy (SIM) reconstructor.
    
    Extracts spatial frequency components from point-scanning data acquired
    with a patterned illumination grating, then reassigns them to reconstruct
    super-resolved images.
    
    Input data format:
        3D array (frames, rows, cols) where frames are ordered according to scan dimensions
    
    Output format:
        6D array (datasets, bases, timepoints, slices, rows, cols)
        - datasets: typically 1 (or multiple if consolidating multi-data)
        - bases: number of spatial frequency components
        - timepoints, slices, rows, cols: scan dimensions
    """
    
    name = "MoNaLISA"
    id = "monalisa"
    file_extensions = ["hdf5", "zarr"]
    description = "Point-scanning SIM reconstruction with pattern-based signal extraction"
    supports_streaming = True
    supports_consolidation = True

    def __init__(self):
        self._logger = initLogger('MonalisaReconstructor')
        self._pattern_finder = PatternFinder()
        self._signal_extractor = None  # Lazy-loaded on first use (Windows-only)
        self._axis_labels = {
            'r_l_text': 'Right-Left',
            'u_d_text': 'Up-Down',
            'b_f_text': 'Back-Front',
            'timepoints_text': 'Timepoints',
            'p_text': 'pos',
            'n_text': 'neg'
        }
    
    def _ensure_signal_extractor(self):
        """Lazy-load SignalExtractor (Windows-only, requires CUDA DLLs)."""
        if self._signal_extractor is None:
            try:
                self._signal_extractor = SignalExtractor()
            except RuntimeError as e:
                raise RuntimeError(
                    f'SignalExtractor initialization failed: {e}. '
                    'MoNaLISA reconstruction requires Windows + CUDA libraries.'
                ) from e
    
    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        """Create and return the MoNaLISA parameter widget."""
        return MonalisaParamsWidget(parent)
    
    def make_metadata_dialog(self, parent: QtWidgets.QWidget) -> QtWidgets.QDialog | None:
        """
        Create scan parameters dialog for MoNaLISA acquisition metadata.
        
        This manages the 4D scan geometry: dimensions, directions, steps, step_sizes,
        and unidirectional flag. For now, we return None and handle this separately
        in the controller transition phase.
        
        TODO: Wrap ScanParamsDialog in a proper MonalisaScanParamsDialog subclass.
        """
        # For now, return None - the controller will manage ScanParamsDialog directly
        # during the transition phase
        return None
    
    def find_pattern(self, data: np.ndarray, param_widget: QtWidgets.QWidget) -> None:
        """
        Automatically detect the illumination pattern in the data and update widget params.
        
        Args:
            data: Raw scan data (3D: frames, rows, cols)
            param_widget: MonalisaParamsWidget instance to update
        """
        if not isinstance(param_widget, MonalisaParamsWidget):
            raise TypeError(f'Expected MonalisaParamsWidget, got {type(param_widget).__name__}')
        
        # Use first frame for pattern detection
        test_frame = data[0] if data.ndim == 3 else data

        # Find pattern, seeding the period search with the widget's current
        # values — the localizer only scans ~+-20% around its guess.
        current = param_widget.get_values()
        row_offset, col_offset, row_period, col_period = self._pattern_finder.find(
            test_frame,
            xp_guess=current.get('col_period'),
            yp_guess=current.get('row_period'),
        )
        
        # Update widget
        param_widget.set_pattern_params(row_offset, col_offset, row_period, col_period)
        
        self._logger.info(f'Pattern found: row_offset={row_offset:.2f}, col_offset={col_offset:.2f}, '
                         f'row_period={row_period:.2f}, col_period={col_period:.2f}')
    
    def process(
        self, data_obj: 'DataObj', params: dict, context=None
    ) -> MonalisaProcessingResult:
        """
        Reconstruct MoNaLISA SIM data.
        
        Args:
            data_obj: DataObj containing raw scan data + metadata
            params: Parameter dict with keys (from MonalisaParamsWidget.get_values()):
                - pixel_size_nm: float
                - reconstruction_method: str ('MoNaLISA' or 'Fast Gauss MoNaLISA')
                - device: str ('CPU' or 'GPU')
                - row_offset, col_offset, row_period, col_period: float
                - psf_fwhm_nm: float
                - bg_modelling: str
                - bg_gaussian_size_nm: float
                - bleaching_correction: bool
                - scan_params: dict with 'dimensions', 'directions', 'steps', 'step_sizes', 'unidirectional'
        
        Returns:
            MonalisaProcessingResult containing reconstructed 6D image
        """
        # Extract scan parameters (required for MoNaLISA)
        scan_params = params.get('scan_params')
        if scan_params is None:
            raise ValueError('MoNaLISA requires scan_params in params dict')
        
        # Load data
        preloaded = data_obj.dataLoaded
        try:
            data_obj.checkAndLoadData()
            data = data_obj.data
            data_attrs = dict(data_obj.attrs or {})
        finally:
            if not preloaded:
                data_obj.checkAndUnloadData()

        recorded_linesteps = MonalisaLiveSession._coerce_positive_int(
            data_attrs.get('ScanTTL:n_linesteps')
        )
        if recorded_linesteps is not None:
            # Recorded metadata is authoritative for frame order. Keep the
            # existing result contract by folding conditions into its T axis.
            scan_params = copy.deepcopy(scan_params)
            scan_params['n_linesteps'] = recorded_linesteps
            try:
                spatial_frames = int(np.prod(
                    np.asarray(scan_params['steps'][:3], dtype=int)
                ))
                if spatial_frames > 0 and data.shape[0] % spatial_frames == 0:
                    scan_params['steps'][3] = str(data.shape[0] // spatial_frames)
            except (KeyError, TypeError, ValueError):
                pass
        
        # Validate data shape
        if data.ndim != 3:
            raise ValueError(f'Expected 3D data (frames, rows, cols), got shape {data.shape}')

        if params.get('reconstruction_method') == 'Fast Gauss MoNaLISA':
            if params.get('sweep_enabled'):
                return self._process_fast_gauss_sweep(
                    data_obj.name, data, params, scan_params, data_attrs
                )
            return self._process_fast_gauss_offline(
                data_obj.name, data, params, scan_params, data_attrs
            )

        if params.get('sweep_enabled'):
            raise ValueError(
                'Parameter sweep is only supported for the Fast Gauss '
                'MoNaLISA method; disable the sweep or switch methods'
            )

        # Bleaching correction
        if params.get('bleaching_correction', False):
            data = self._apply_bleaching_correction(data)
        
        # Build pattern
        row_offset = np.mod(params['row_offset'], params['row_period'])
        col_offset = np.mod(params['col_offset'], params['col_period'])
        pattern = (row_offset, col_offset, params['row_period'], params['col_period'])
        
        # Build sigmas for signal extraction
        fwhm_nm = np.array([params['psf_fwhm_nm']])
        if params['bg_modelling'] == 'Constant':
            fwhm_nm = np.append(fwhm_nm, 9999)  # Code for constant bg
        elif params['bg_modelling'] == 'No background':
            fwhm_nm = np.append(fwhm_nm, 0)  # Code for zero bg
        elif params['bg_modelling'] == 'Gaussian':
            fwhm_nm = np.append(fwhm_nm, params['bg_gaussian_size_nm'])
        else:
            raise ValueError(f'Invalid BG modelling "{params["bg_modelling"]}"')
        
        sigmas = fwhm_nm / (2.355 * params['pixel_size_nm'])
        
        # Extract coefficients
        device = params['device'].lower()
        self._logger.info(f'Extracting signal with {params["device"]} on {data.shape[0]} frames...')
        self._ensure_signal_extractor()
        coeffs = self._signal_extractor.extractSignal(data, sigmas, pattern, device)
        # SignalExtractor returns shape (numBases, numFrames, gridRows, gridCols).
        # coeffs_to_image() takes a 3D (frames, gridRows, gridCols) slice, so we
        # iterate per base and stack along a new leading Base axis — matching
        # the legacy ReconObj.updateImages contract. Without this loop the
        # output collapsed to a confusing 2-frame stack because the bases axis
        # was treated as if it were the scan-frame axis.
        if coeffs.ndim != 4:
            raise ValueError(
                f'SignalExtractor returned shape {coeffs.shape}; '
                f'expected (numBases, numFrames, gridRows, gridCols)'
            )
        num_bases = coeffs.shape[0]

        # Auto-detect the scan fast/slow axes and pos/neg directions by
        # minimizing the total variation of the signal-base reconstruction.
        # Mirrors Mini_Recon's get_orientation; user can disable via the
        # 'Auto-detect scan orientation' checkbox to keep the dialog values.
        if params.get('auto_scan_orientation', True):
            try:
                best_params, best_label, best_score = auto_detect_scan_orientation(
                    coeffs[0], scan_params, self._axis_labels,
                )
                self._logger.info(
                    f'Auto scan orientation: {best_label}  (TV score {best_score:.3g})'
                )
                scan_params = best_params
            except Exception as exc:
                # The detector is a quality-of-life add-on; falling back to
                # the dialog values must never block a reconstruction.
                self._logger.warning(
                    f'Scan-orientation auto-detect failed, using dialog values: {exc}'
                )

        self._logger.info(
            f'Converting coefficients to images ({num_bases} bases x '
            f'{coeffs.shape[1]} frames -> per-base reconstruction)...'
        )
        # Add the leading Dataset axis (single dataset per process() call) so
        # the retained coefficients match the (Dataset, Base, frames, gridRows,
        # gridCols) contract shared with the legacy controller path.  Retaining
        # them lets the viewer re-reconstruct on scan-param edits and export
        # coefficients.  from_coeffs() reassembles the 6D image, derives the
        # output pixel pitch and auto display levels.
        coeffs_5d = coeffs[np.newaxis, ...]
        result = MonalisaProcessingResult.from_coeffs(
            name=data_obj.name,
            coeffs=coeffs_5d,
            scan_params=scan_params,
            axis_label_map=self._axis_labels,
        )

        self._logger.info(f'Reconstruction complete: shape {result.data.shape}')
        return result

    def consolidate(
        self, results: list[MonalisaProcessingResult]
    ) -> MonalisaProcessingResult:
        """Merge per-file reconstruction results along the leading Dataset axis.

        Equivalent to the legacy consolidated workflow: per-dataset slices are
        reconstructed independently (``reconstruct_images_from_coeffs`` loops
        over the Dataset axis), so concatenating per-file 6D data matches
        stacking the coefficients first and reconstructing once. Coefficients
        are carried over only when every input retained them (the classic
        method does, Fast Gauss does not), keeping 'Update reconstruction'
        working on merged classic results.
        """
        results = list(results)
        if not results:
            raise ValueError('No results to consolidate')
        if any(isinstance(result, MonalisaSweepResult) for result in results):
            # The Dataset axis sits at index 1 for sweeps, so the axis-0
            # concatenation below would silently merge along the Sweep axis.
            raise ValueError(
                'Parameter-sweep results cannot be consolidated; disable the '
                'sweep for multi-data merges (or sweep each dataset '
                'individually)'
            )
        if len(results) == 1:
            return results[0]

        first = results[0]
        for other in results[1:]:
            if other.data.shape[1:] != first.data.shape[1:]:
                raise ValueError(
                    f"Cannot consolidate '{other.name}' into '{first.name}': "
                    f'per-dataset shape {tuple(other.data.shape[1:])} does not '
                    f'match {tuple(first.data.shape[1:])} — were these files '
                    'acquired with the same scan geometry?'
                )

        data = np.concatenate([result.data for result in results], axis=0)
        coeffs_list = [getattr(result, 'coeffs', None) for result in results]
        coeffs = None
        if all(c is not None for c in coeffs_list) and all(
            c.shape[1:] == coeffs_list[0].shape[1:] for c in coeffs_list[1:]
        ):
            coeffs = np.concatenate(coeffs_list, axis=0)

        finite = data[np.isfinite(data)]
        display_levels = None
        if finite.size:
            display_levels = (
                float(np.percentile(finite, 1)),
                float(np.percentile(finite, 99.9)),
            )

        return MonalisaProcessingResult(
            name=first.name,
            data=data,
            scan_params=copy.deepcopy(first.scan_params),
            display_levels=display_levels,
            output_pixel_size_nm=first.output_pixel_size_nm,
            coeffs=coeffs,
            axis_label_map=dict(first.axis_label_map),
        )

    def make_session(self) -> MonalisaLiveSession:
        """
        Create a fresh streaming session for live reconstruction.
        
        Returns:
            MonalisaLiveSession instance.
        """
        return MonalisaLiveSession()

    def _process_fast_gauss_offline(
        self,
        name: str,
        data: np.ndarray,
        params: dict,
        scan_params: dict,
        data_attrs: dict | None = None,
    ) -> MonalisaProcessingResult:
        """
        Run the fast-Gauss MoNaLISA path on a complete offline stack.

        Dispatches on the pattern geometry: axis-aligned rectangular grids go
        through the exact live-session pipeline (one output pixel per sample,
        no interpolation), while any other Bravais lattice — rotated square
        ("diamond"), hexagonal — goes through the general scatter-and-grid
        path. The default ``Auto`` mode detects the pattern and only reroutes
        when it is measurably non-rectangular, so rectangular data keeps its
        historical output bit for bit.
        """
        geometry = self._resolve_fast_gauss_geometry(data, scan_params, data_attrs)

        mode = self._resolve_pattern_geometry_mode(params)
        lattice = None
        if mode != 'rectangular':
            lattice = self._detect_offline_lattice(
                data, params, required=(mode == 'general')
            )
        use_general = mode == 'general' or (
            mode == 'auto'
            and lattice is not None
            and not lattice.is_axis_aligned_rectangular(tol=0.05)
        )
        if use_general:
            return self._process_fast_gauss_general(
                name, data, params, geometry, lattice
            )
        return self._process_fast_gauss_rectangular(name, data, params, geometry)

    def _resolve_fast_gauss_geometry(
        self,
        data: np.ndarray,
        scan_params: dict,
        data_attrs: dict | None,
    ) -> dict:
        """Scan geometry from the dialog values, falling back to file attrs."""
        geometry = self._fast_gauss_geometry_from_scan_params(scan_params)
        expected_frames = geometry['frames_per_stack'] * geometry['num_timepoints']
        if data.shape[0] != expected_frames:
            metadata_geometry = self._fast_gauss_geometry_from_attrs(
                data_attrs or {}, data.shape[0]
            )
            if metadata_geometry is None:
                raise ValueError(
                    'Fast Gauss MoNaLISA expected '
                    f'{expected_frames} frames ({geometry["frames_per_stack"]} '
                    f'per timepoint x {geometry["num_timepoints"]} timepoints), '
                    f'got {data.shape[0]}'
                )
            geometry = metadata_geometry
        return geometry

    @staticmethod
    def _resolve_pattern_geometry_mode(params: dict) -> str:
        """Normalize the pattern-geometry choice to auto/rectangular/general."""
        text = str(params.get('fast_gauss_pattern_geometry', 'auto')).strip().lower()
        if not text or 'auto' in text:
            return 'auto'
        if 'general' in text or 'lattice' in text:
            return 'general'
        if 'rect' in text:
            return 'rectangular'
        raise ValueError(
            f'Unknown pattern geometry {params.get("fast_gauss_pattern_geometry")!r}; '
            "use 'Auto', 'Rectangular grid' or 'General lattice'"
        )

    def _detect_offline_lattice(
        self, data: np.ndarray, params: dict, required: bool
    ) -> Lattice | None:
        """Detect the illumination lattice on the frame sum.

        The widget's pattern periods set the spectral search band (the exact
        values need not be right — they bound the scale so the detection is
        not captured by low-frequency sample structure).
        """
        summed = np.asarray(data, dtype=np.float64).sum(axis=0)
        band = detection_band(params.get('col_period'), params.get('row_period'))
        try:
            lattice = detect_lattice(summed, **band)
        except ValueError as exc:
            if required:
                raise ValueError(
                    'General-lattice reconstruction was requested but no '
                    f'illumination lattice was detected: {exc}'
                ) from exc
            self._logger.info(
                f'No illumination lattice detected ({exc}); '
                'using the rectangular pipeline'
            )
            return None
        self._logger.info(f'Detected illumination lattice: {lattice.describe()}')
        return lattice

    def _process_fast_gauss_rectangular(
        self,
        name: str,
        data: np.ndarray,
        params: dict,
        geometry: dict,
    ) -> MonalisaProcessingResult:
        """Axis-aligned grid path: the live session run over the whole stack.

        The fast-Gauss implementation is a 2D X/Y reassignment path. It can
        process multiple timepoints, but not Z stacks or scan orders where X/Y
        are not the two scan axes.
        """
        frames_per_stack = geometry['frames_per_stack']
        session = self.make_session()
        try:
            first_stack = data[:frames_per_stack]
            session_params = self._fast_gauss_session_params(params)
            init_obj = StreamInit(
                name=name,
                dataset_name='offline',
                data=first_stack,
                attrs=geometry['attrs'],
            )
            session.begin(init_obj, session_params)

            for time_index in range(1, geometry['num_timepoints']):
                start = time_index * frames_per_stack
                end = start + frames_per_stack
                session.push(data[start:end], start, end)

            live_result = session.finish()
            # Reconstructed pitch == scan step size (the ny_c/nx_c foci tile
            # whole illumination periods; they do not subdivide a scan step).
            out_px = (geometry['step_y_nm'], geometry['step_x_nm'])
        finally:
            session.close()
        finite_data = live_result.data[np.isfinite(live_result.data)]
        display_levels = None
        if finite_data.size:
            display_levels = (
                float(np.percentile(finite_data, 1)),
                float(np.percentile(finite_data, 99.9)),
            )

        result = MonalisaProcessingResult(
            name=name,
            data=live_result.data,
            scan_params=geometry['scan_params'],
            display_levels=display_levels,
            output_pixel_size_nm=out_px,
            axis_label_map=self._axis_labels,
        )
        self._logger.info(f'Fast Gauss reconstruction complete: shape {result.data.shape}')
        return result

    def _process_fast_gauss_general(
        self,
        name: str,
        data: np.ndarray,
        params: dict,
        geometry: dict,
        lattice: Lattice,
    ) -> MonalisaProcessingResult:
        """Scatter-and-grid reassignment for non-axis-aligned lattices.

        Amplitudes are extracted per detected focus (exact-pixel per-focus
        weights — arbitrary centers have no shared-weight shortcut), assigned
        their sample-space positions ``focus + scan offset``, and gridded
        onto a square output raster of pitch equal to the scan step by
        bilinear splatting with weight normalization. The scan orientation is
        resolved by total variation exactly as in the rectangular path;
        output pixels the scan never covered are NaN. The pre-gridding spot
        cloud is retained on the result (with per-focus flat-fielding already
        applied when that option is on — the cloud always holds the values
        the image was gridded from).
        """
        if geometry['n_linesteps'] != 1:
            raise ValueError(
                'General-lattice fast Gauss currently supports a single line '
                f'step (got n_linesteps={geometry["n_linesteps"]})'
            )
        try:
            pixel_size_nm = float(params.get('pixel_size_nm'))
        except (TypeError, ValueError):
            pixel_size_nm = 0.0
        if pixel_size_nm <= 0:
            raise ValueError(
                'General-lattice reconstruction needs the camera pixel size '
                "(the widget's 'Pixel size', in nm) to place scan offsets in "
                'camera pixels'
            )

        nx_s, ny_s = geometry['nx_s'], geometry['ny_s']
        frames_per_stack = geometry['frames_per_stack']
        num_timepoints = geometry['num_timepoints']
        step_x_px = geometry['step_x_nm'] / pixel_size_nm
        step_y_px = geometry['step_y_nm'] / pixel_size_nm
        num_rows, num_cols = data.shape[-2:]

        foci_x, foci_y = lattice.points_in_frame(num_rows, num_cols)
        if foci_x.size < 3:
            raise ValueError(
                'Fewer than three lattice foci fall inside the frame '
                f'({lattice.describe()})'
            )
        foci_xy = np.column_stack([foci_x, foci_y])

        session = self.make_session()
        gaussian_sigma_px = session._resolve_gaussian_sigma_px(params)
        pinhole_radius_px = session._resolve_pinhole_radius_px(
            params, gaussian_sigma_px
        )
        fit_background = session._resolve_fit_background(params)
        num_rects, gaussian_sigma_px, pinhole_radius_px = (
            validate_gaussian_fit_options(
                params.get('fast_gauss_footprint_num_rects'),
                gaussian_sigma_px,
                pinhole_radius_px,
            )
        )
        footprint = _build_footprint(num_rects, pinhole_radius_px)

        self._logger.info(
            f'General-lattice fast Gauss: {foci_xy.shape[0]} foci, '
            f'{lattice.describe()}; scan {nx_s}x{ny_s} steps of '
            f'({step_x_px:.3f}, {step_y_px:.3f}) px '
            f'(pixel size {pixel_size_nm:g} nm)'
        )
        scan_cell_ratio = (
            nx_s * step_x_px * ny_s * step_y_px / lattice.cell_area
        )
        self._logger.info(
            f'Scan area covers {scan_cell_ratio:.2f}x the lattice unit cell '
            '(1.0 = every sample position visited once)'
        )

        amplitudes = extract_lattice_amplitudes(
            data, foci_x, foci_y, footprint, gaussian_sigma_px, fit_background
        ).reshape(num_timepoints, frames_per_stack, -1)

        orientation = choose_orientation(
            foci_xy, amplitudes[0], nx_s, ny_s, (step_x_px, step_y_px)
        )
        self._logger.info(
            f'Scan orientation (total-variation pick): fast axis '
            f'{orientation[0]}, signs ({orientation[1]:+d}, {orientation[2]:+d})'
        )
        offsets = scan_offsets_px(nx_s, ny_s, step_x_px, step_y_px, orientation)
        positions = sample_positions(foci_xy, offsets)
        frame_indices = np.repeat(np.arange(frames_per_stack), foci_xy.shape[0])
        focus_indices = np.tile(np.arange(foci_xy.shape[0]), frames_per_stack)

        flat_field_stats = None
        if params.get('fast_gauss_flat_field'):
            focus_offsets = solve_per_focus_offsets(
                positions,
                amplitudes.reshape(num_timepoints, -1),
                focus_indices,
                foci_xy,
                lattice.nearest_spacing(),
                (step_x_px, step_y_px),
            )
            amplitudes = amplitudes - focus_offsets[:, np.newaxis, :]
            flat_field_stats = {
                'offset_mad': float(np.median(np.abs(focus_offsets))),
                'offset_min': float(focus_offsets.min()),
                'offset_max': float(focus_offsets.max()),
            }
            self._logger.info(
                'Per-focus flat-field: offset MAD '
                f'{flat_field_stats["offset_mad"]:.2f}, range '
                f'[{flat_field_stats["offset_min"]:.1f}, '
                f'{flat_field_stats["offset_max"]:.1f}]'
            )

        first = assemble_image(
            positions, amplitudes[0].reshape(-1), pitch=(step_x_px, step_y_px)
        )
        images = [first.image]
        for time_index in range(1, num_timepoints):
            images.append(
                assemble_image(
                    positions,
                    amplitudes[time_index].reshape(-1),
                    pitch=first.pitch_px,
                    origin=first.origin_px,
                    shape=first.image.shape,
                ).image
            )
        stacked = np.stack(images).astype(np.float32)
        data_6d = stacked[np.newaxis, np.newaxis, :, np.newaxis, :, :]
        self._logger.info(
            f'Gridded {positions.shape[0]} samples/timepoint onto '
            f'{first.image.shape} px; coverage {first.coverage:.1%}'
        )

        spots = MonalisaSpotCloud(
            positions_px=positions.astype(np.float64),
            intensities=amplitudes.reshape(num_timepoints, -1),
            frame_indices=frame_indices,
            focus_indices=focus_indices,
            pixel_size_nm=pixel_size_nm,
        )

        finite = stacked[np.isfinite(stacked)]
        display_levels = None
        if finite.size:
            display_levels = (
                float(np.percentile(finite, 1)),
                float(np.percentile(finite, 99.9)),
            )

        result = MonalisaProcessingResult(
            name=name,
            data=data_6d,
            scan_params=geometry['scan_params'],
            display_levels=display_levels,
            output_pixel_size_nm=(geometry['step_y_nm'], geometry['step_x_nm']),
            axis_label_map=self._axis_labels,
            spots=spots,
            recon_diagnostics={
                'pattern_geometry': 'general',
                'lattice': lattice.describe(),
                'lattice_spacing_px': lattice.nearest_spacing(),
                'num_foci': int(foci_xy.shape[0]),
                'pixel_size_nm': pixel_size_nm,
                'step_px': (step_x_px, step_y_px),
                'scan_orientation': orientation,
                'output_origin_px': first.origin_px,
                'coverage': first.coverage,
                'scan_cell_ratio': scan_cell_ratio,
                'flat_field': flat_field_stats,
            },
        )
        self._logger.info(
            f'General-lattice reconstruction complete: shape {result.data.shape}'
        )
        return result

    def _process_fast_gauss_sweep(
        self,
        name: str,
        data: np.ndarray,
        params: dict,
        scan_params: dict,
        data_attrs: dict | None = None,
    ) -> MonalisaSweepResult:
        """Run the offline fast-Gauss path once per sweep value.

        Optional advanced mode: the chosen parameter (pinhole radius or fit
        sigma) is overridden per run and the per-value reconstructions are
        stacked along a leading Sweep axis, so the viewer exposes a slider to
        find the best setting empirically.
        """
        parameter_key = resolve_sweep_parameter(params.get('sweep_parameter'))
        values = parse_sweep_values(params.get('sweep_values_text'))
        params_key, ui_label = SWEEPABLE_PARAMETERS[parameter_key]

        per_value_results = []
        for index, value in enumerate(values):
            run_params = dict(params)
            run_params['sweep_enabled'] = False
            run_params[params_key] = value
            if parameter_key == 'pinhole_radius_sigma':
                # Sweeping a pinhole radius the shell footprint would ignore
                # is meaningless; force the mode that uses it.
                run_params['fast_gauss_footprint_mode'] = 'Circular pinhole'
            self._logger.info(
                f'Sweep {index + 1}/{len(values)}: {ui_label} = {value:g}'
            )
            per_value_results.append(
                self._process_fast_gauss_offline(
                    name, data, run_params, scan_params, data_attrs
                )
            )

        first = per_value_results[0]
        for result in per_value_results[1:]:
            if result.data.shape != first.data.shape:
                raise ValueError(
                    'Sweep runs produced differing shapes '
                    f'({result.data.shape} vs {first.data.shape}); cannot stack'
                )

        stacked = np.stack([result.data for result in per_value_results])
        finite = stacked[np.isfinite(stacked)]
        display_levels = None
        if finite.size:
            display_levels = (
                float(np.percentile(finite, 1)),
                float(np.percentile(finite, 99.9)),
            )
        sweep_result = MonalisaSweepResult(
            name=name,
            data=stacked,
            scan_params=first.scan_params,
            sweep_parameter_label=ui_label,
            sweep_values=values,
            display_levels=display_levels,
            output_pixel_size_nm=first.output_pixel_size_nm,
            axis_label_map=self._axis_labels,
        )
        self._logger.info(
            f'Fast Gauss sweep complete: {len(values)} x {ui_label}, '
            f'shape {sweep_result.data.shape}'
        )
        return sweep_result

    @staticmethod
    def _fast_gauss_session_params(params: dict) -> dict:
        """Pass widget pattern params into offline fast-Gauss localization."""
        session_params = dict(params)
        required = ("row_offset", "col_offset", "row_period", "col_period")
        if all(key in params for key in required):
            session_params["_monalisa_pattern_params"] = {
                key: float(params[key]) for key in required
            }
        return session_params

    def _fast_gauss_geometry_from_scan_params(self, scan_params: dict) -> dict:
        """Convert MoNaLISA scan params to the attrs expected by MonalisaLiveSession."""
        dimensions = list(scan_params['dimensions'])
        steps = [int(v) for v in scan_params['steps']]
        step_sizes = [float(v) for v in scan_params['step_sizes']]

        rl_label = self._axis_labels['r_l_text']
        ud_label = self._axis_labels['u_d_text']
        bf_label = self._axis_labels['b_f_text']
        time_label = self._axis_labels['timepoints_text']
        aliases = {
            rl_label: (rl_label, 'Right/Left', 'RightLeft', 'RL', 'X'),
            ud_label: (ud_label, 'Up/Down', 'UpDown', 'UD', 'Y'),
            bf_label: (bf_label, 'Back/Forth', 'Back/Front', 'BackForth', 'BF', 'Z'),
            time_label: (time_label, 'Time', 'T'),
        }

        def _label_key(label: object) -> str:
            return ''.join(ch for ch in str(label).lower() if ch.isalnum())

        def _find_dimension_index(canonical_label: str) -> int:
            wanted = {_label_key(alias) for alias in aliases[canonical_label]}
            for index, dimension in enumerate(dimensions):
                if _label_key(dimension) in wanted:
                    return index
            raise ValueError(canonical_label)

        try:
            x_index = _find_dimension_index(rl_label)
            y_index = _find_dimension_index(ud_label)
            z_index = _find_dimension_index(bf_label)
            time_index = _find_dimension_index(time_label)
        except ValueError as exc:
            raise ValueError(
                'Fast Gauss MoNaLISA requires Right-Left, Up-Down, '
                'Back-Front and Timepoints scan dimensions; got '
                f'{dimensions!r}'
            ) from exc

        if {x_index, y_index} != {0, 1}:
            raise ValueError(
                'Fast Gauss MoNaLISA requires Right-Left and Up-Down as the '
                'first two scan dimensions'
            )
        if steps[z_index] != 1:
            raise ValueError('Fast Gauss MoNaLISA offline mode currently supports one Z slice')

        nx_s = steps[x_index]
        ny_s = steps[y_index]
        num_timepoints = steps[time_index]
        num_linesteps = int(scan_params.get('n_linesteps', 1))
        if num_linesteps < 1:
            raise ValueError('Fast Gauss MoNaLISA n_linesteps must be at least 1')
        if num_timepoints % num_linesteps != 0:
            raise ValueError(
                'Fast Gauss MoNaLISA time/condition steps must be divisible by '
                f'n_linesteps ({num_timepoints} vs {num_linesteps})'
            )
        num_timepoints //= num_linesteps
        step_x_nm = step_sizes[x_index]
        step_y_nm = step_sizes[y_index]
        normalized_dimensions = list(dimensions)
        normalized_dimensions[x_index] = rl_label
        normalized_dimensions[y_index] = ud_label
        normalized_dimensions[z_index] = bf_label
        normalized_dimensions[time_index] = time_label
        normalized_scan_params = dict(scan_params)
        normalized_scan_params['dimensions'] = normalized_dimensions
        normalized_scan_params['n_linesteps'] = num_linesteps
        attrs = {
            'ScanStage:axis_startpos': [0.0, 0.0, 0.0],
            'ScanStage:axis_length': [
                (nx_s - 1) * step_x_nm,
                (ny_s - 1) * step_y_nm,
                1.0,
            ],
            'ScanStage:axis_step_size': [step_x_nm, step_y_nm, 1.0],
            'ScanStage:axis_step_size_unit': 'nm',
            'ScanTTL:n_linesteps': num_linesteps,
            'recording:frames_per_stack': nx_s * ny_s * num_linesteps,
            'recording:num_timepoints': num_timepoints,
        }
        return {
            'attrs': attrs,
            'nx_s': nx_s,
            'ny_s': ny_s,
            'n_linesteps': num_linesteps,
            'frames_per_stack': nx_s * ny_s * num_linesteps,
            'num_timepoints': num_timepoints,
            'step_x_nm': step_x_nm,
            'step_y_nm': step_y_nm,
            'scan_params': normalized_scan_params,
        }

    def _fast_gauss_geometry_from_attrs(
        self, attrs: dict, num_frames: int
    ) -> dict | None:
        """Derive fast-Gauss geometry from file metadata when UI params mismatch."""
        required = (
            'ScanStage:axis_startpos',
            'ScanStage:axis_length',
            'ScanStage:axis_step_size',
        )
        if not attrs or any(key not in attrs for key in required):
            return None

        try:
            axis_startpos = np.asarray(attrs['ScanStage:axis_startpos'], dtype=float).flatten()
            axis_length = np.asarray(attrs['ScanStage:axis_length'], dtype=float).flatten()
            axis_step_size = np.asarray(attrs['ScanStage:axis_step_size'], dtype=float).flatten()
            if axis_startpos.size < 2 or axis_length.size < 2 or axis_step_size.size < 2:
                return None

            session = self.make_session()
            ttl_steps = session._scan_steps_from_ttl(attrs)
            candidates = []
            if ttl_steps is not None:
                candidates.append(ttl_steps)

            size_steps = session._scan_steps_from_size(axis_length, axis_step_size)
            if size_steps is not None:
                candidates.append(size_steps)

            endpoint_steps = session._scan_steps_from_endpoint(
                axis_startpos, axis_length, axis_step_size
            )
            if endpoint_steps is not None:
                candidates.append(endpoint_steps)

            unique_candidates = []
            for candidate in candidates:
                if candidate not in unique_candidates:
                    unique_candidates.append(candidate)

            frame_hint = session._coerce_positive_int(
                attrs.get('recording:frames_per_stack')
            )
            num_linesteps = (
                session._coerce_positive_int(attrs.get('ScanTTL:n_linesteps'))
                or 1
            )
            if frame_hint is not None:
                for nx_s, ny_s in unique_candidates:
                    if nx_s * ny_s * num_linesteps == frame_hint:
                        return self._fast_gauss_geometry_from_counts(
                            attrs, nx_s, ny_s, num_frames
                        )

            for nx_s, ny_s in unique_candidates:
                frames_per_stack = nx_s * ny_s * num_linesteps
                if frames_per_stack > 0 and num_frames % frames_per_stack == 0:
                    return self._fast_gauss_geometry_from_counts(
                        attrs, nx_s, ny_s, num_frames
                    )
        except (TypeError, ValueError):
            return None

        return None

    def _fast_gauss_geometry_from_counts(
        self,
        attrs: dict,
        nx_s: int,
        ny_s: int,
        num_frames: int,
    ) -> dict | None:
        num_linesteps = (
            MonalisaLiveSession._coerce_positive_int(
                attrs.get('ScanTTL:n_linesteps')
            )
            or 1
        )
        frames_per_stack = nx_s * ny_s * num_linesteps
        if frames_per_stack <= 0 or num_frames % frames_per_stack != 0:
            return None

        num_timepoints = max(1, num_frames // frames_per_stack)
        step_sizes = np.asarray(attrs['ScanStage:axis_step_size'], dtype=float).flatten()
        step_x_nm, step_y_nm = MonalisaLiveSession.scan_stage_step_size_nm(
            attrs, step_sizes
        )

        geometry_attrs = dict(attrs)
        geometry_attrs['ScanTTL:n_linesteps'] = num_linesteps
        geometry_attrs['recording:frames_per_stack'] = frames_per_stack
        geometry_attrs['recording:num_timepoints'] = num_timepoints

        scan_params = {
            'dimensions': [
                self._axis_labels['r_l_text'],
                self._axis_labels['u_d_text'],
                self._axis_labels['b_f_text'],
                self._axis_labels['timepoints_text'],
            ],
            'directions': ['+', '+', '+', '+'],
            'steps': [nx_s, ny_s, 1, num_timepoints * num_linesteps],
            'step_sizes': [step_x_nm, step_y_nm, 1.0, 1.0],
            'n_linesteps': num_linesteps,
            'unidirectional': False,
        }
        return {
            'attrs': geometry_attrs,
            'nx_s': nx_s,
            'ny_s': ny_s,
            'n_linesteps': num_linesteps,
            'frames_per_stack': frames_per_stack,
            'num_timepoints': num_timepoints,
            'step_x_nm': step_x_nm,
            'step_y_nm': step_y_nm,
            'scan_params': scan_params,
        }
    
    def _apply_bleaching_correction(self, data: np.ndarray) -> np.ndarray:
        """
        Apply photobleaching correction to raw data.

        Rescales each frame by ``E_0 / E_i`` so every frame carries the first
        frame's total energy. Fluorescence intensity is linear in the
        remaining fluorophore population, so the linear energy ratio is the
        correct compensation; the 4th power a legacy version applied
        overcorrected bleaching by the cube of the energy loss. This matches
        the live fast-Gauss path
        (:meth:`MonalisaLiveSession._apply_bleaching_correction`).

        Args:
            data: 3D array (frames, rows, cols)

        Returns:
            Corrected 3D array (input dtype preserved — the SignalExtractor
            DLL reads the raw buffer, so the dtype must not change here)
        """
        corrected_data = data.copy()
        energy = np.sum(data, axis=(1, 2), dtype=np.float64)
        for i in range(data.shape[0]):
            if energy[i] <= 0:
                continue
            c = energy[0] / energy[i]
            corrected_data[i, :, :] = data[i, :, :] * c
        return corrected_data


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
