"""MoNaLISA SIM reconstructor plugin."""

import copy
from typing import TYPE_CHECKING

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.imcommon.model.acquisition_layout import UnconsumedLoopError
from imswitch.improcess.model.acquisition_layout_resolver import (
    ResolvedAcquisitionLayout,
)
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.reconstructors.base import DetectionPreview, StreamInit, StreamingReconstructor
from .coeffs_to_image import (
    linestep_conditions_interleave_per_line,
    placement_from_layout,
)
from .calibration import PatternLocalization, ScanOrientation
from .calibration_frames import most_modulated_indices
from .live_session import MonalisaLiveSession
from .localization_settings import LocalizationSettings
from .params import (
    LOCALIZATION_AUTO, LOCALIZATION_LOCKED, LOCALIZATION_MANUAL,
    LOCALIZATION_REFINEMENT_FAST, LOCALIZATION_REFINEMENT_FULL,
    ORIENTATION_AUTO, ORIENTATION_LOCKED, ORIENTATION_MANUAL,
    PLACEMENT_AUTO, PLACEMENT_DIRECT, PLACEMENT_INTERPOLATE, PRESETS,
    SPOT_AUTO, SPOT_MANUAL, default_params as unified_default_params,
)
from .pipeline import PipelineParams, find_localization, needs_modulation_frame
from .spot_settings import fwhm_to_sigma_px, sigma_px_to_fwhm
from .orientation import auto_detect_scan_orientation
from .scan_frame import ORIENTATIONS
from .params_widget import MonalisaParamsWidget
from .pattern_finder import PatternFinder
from .result import MonalisaProcessingResult
from .signal_extractor import SignalExtractor
from .session_registry import MonalisaSessionRegistry, detector_identity

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj

# Frames are extracted in blocks of about this many bytes: a block of a
# 464x464 uint16 camera is some 600 frames, one per cell of the recording.
FAST_GAUSS_PLACEMENT_CHUNK_BYTES = 256 * 1024 * 1024


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
    file_extensions = ["hdf5", "h5", "zarr", "tiff"]
    # One timepoint must be a whole nx*ny scan: the pattern reassignment has
    # nothing to reassemble from a single frame. Independent of being a
    # timelapse -- a camera lapse is many timepoints of one frame each.
    requires_frame_stacks = True
    description = "Unified Gaussian MoNaLISA reconstruction for rectangular, staggered and general lattices"
    execution_policy = "worker"
    supports_streaming = True
    supports_consolidation = True
    # The fixed unified Gaussian parameter contract replaces the former
    # internal Fast/Legacy method selector. Old v1 dictionaries remain
    # accepted by process() during the migration window.
    params_version = 4

    @classmethod
    def default_params(cls) -> dict:
        return unified_default_params()

    @classmethod
    def param_spec(cls) -> tuple:
        defaults = cls.default_params()
        return (
            ParamField('preset', 'select', defaults['preset'], label='Mode', options=tuple(PRESETS)),
            ParamField('device', 'select', defaults['device'], label='CPU/GPU', options=('GPU', 'CPU'), group='Advanced', advanced=True),
            ParamField('pixel_size_nm', 'float', defaults['pixel_size_nm'], label='Camera pixel', suffix='nm', min=0.0, group='Advanced', advanced=True),
            ParamField('localization_mode', 'select', defaults['localization_mode'], label='Mode',
                       options=(LOCALIZATION_AUTO, LOCALIZATION_LOCKED, LOCALIZATION_MANUAL), group='Localization'),
            ParamField('auto_localization', 'json', defaults['auto_localization'],
                       label='Resolved Auto localization', group='Localization', advanced=True),
            ParamField('locked_localization_id', 'str', defaults['locked_localization_id'],
                       label='Locked localization id', group='Localization', advanced=True),
            ParamField('locked_localization_name', 'str', defaults['locked_localization_name'],
                       label='Locked localization name', group='Localization', advanced=True),
            ParamField('locked_localization', 'json', defaults['locked_localization'],
                       label='Resolved Locked localization', group='Localization', advanced=True),
            ParamField('localization_refinement', 'select', defaults['localization_refinement'],
                       label='Localization refinement',
                       options=(LOCALIZATION_REFINEMENT_FAST, LOCALIZATION_REFINEMENT_FULL),
                       group='Localization', advanced=True),
            ParamField('manual_offset_x_px', 'float', defaults['manual_offset_x_px'], label='Offset X', suffix='px', group='Localization'),
            ParamField('manual_offset_y_px', 'float', defaults['manual_offset_y_px'], label='Offset Y', suffix='px', group='Localization'),
            ParamField('manual_period_x_px', 'float', defaults['manual_period_x_px'], label='Period X', suffix='px', min=0.001, group='Localization'),
            ParamField('manual_period_y_px', 'float', defaults['manual_period_y_px'], label='Period Y', suffix='px', min=0.001, group='Localization'),
            ParamField('stagger_axis', 'select', defaults['stagger_axis'], label='Stagger axis', options=('None', 'X', 'Y'), group='Localization'),
            ParamField('stagger', 'float', defaults['stagger'], label='Stagger', group='Localization',
                       help='Signed fraction of the period along the stagger axis'),
            ParamField('localization_settings', 'json', defaults['localization_settings'],
                       label='Localization settings', group='Localization', advanced=True),
            ParamField('orientation_mode', 'select', defaults['orientation_mode'], label='Mode',
                       options=(ORIENTATION_AUTO, ORIENTATION_LOCKED, ORIENTATION_MANUAL), group='Scanning'),
            ParamField('orientation', 'select', defaults['orientation'], label='Orientation',
                       options=tuple(ORIENTATIONS), group='Scanning'),
            ParamField('locked_orientation', 'str', defaults['locked_orientation'],
                       label='Locked orientation', group='Scanning', advanced=True),
            ParamField('spot_width_mode', 'select', defaults['spot_width_mode'], label='Mode',
                       options=(SPOT_MANUAL, SPOT_AUTO), group='Spot model'),
            ParamField('spot_fwhm', 'float', defaults['spot_fwhm'], label='FWHM', min=0.001, group='Spot model'),
            ParamField('spot_fwhm_unit', 'select', defaults['spot_fwhm_unit'], label='FWHM unit', options=('nm', 'px'), group='Spot model'),
            ParamField('background', 'select', defaults['background'], label='Background', options=('constant', 'none', 'auto'), group='Spot model'),
            ParamField('intensity_correction', 'bool', defaults['intensity_correction'], label='Intensity correction', group='Corrections',
                       help='Correct smooth frame-to-frame intensity variations, e.g. bleaching or illumination drift'),
            ParamField('cell_offsets', 'bool', defaults['cell_offsets'], label='Cell offsets', group='Corrections'),
            ParamField('reassignment', 'select', defaults['reassignment'], label='Reassignment', options=('off', 'auto', 'fixed'), group='ISM'),
            ParamField('shift_factor', 'float', defaults['shift_factor'], label='Shift factor', min=0.0, max=1.0, group='ISM'),
            ParamField('reassignment_asymmetric', 'bool', defaults['reassignment_asymmetric'], label='Asymmetric', group='ISM'),
            ParamField('shift_factor_x', 'float', defaults['shift_factor_x'], label='Shift factor X', min=0.0, max=1.0, group='ISM'),
            ParamField('shift_factor_y', 'float', defaults['shift_factor_y'], label='Shift factor Y', min=0.0, max=1.0, group='ISM'),
            ParamField('placement_mode', 'select', defaults['placement_mode'], label='Mode', options=(PLACEMENT_AUTO, PLACEMENT_DIRECT, PLACEMENT_INTERPOLATE), group='Advanced', advanced=True),
            ParamField('commensurate_tolerance', 'float', defaults['commensurate_tolerance'], label='Direct tolerance', suffix='px', min=0.0, max=1.0, group='Advanced', advanced=True),
            ParamField('variance_weighting', 'bool', defaults['variance_weighting'], label='Variance weighting', group='Advanced', advanced=True),
            ParamField('sharpen', 'bool', defaults['sharpen'], label='Sharpened image', group='Output'),
            ParamField('sharpen_sigma_px', 'float', defaults['sharpen_sigma_px'], label='Sharpening blur', suffix='px', min=0.1, group='Advanced', advanced=True),
            ParamField('sharpen_regularization', 'float', defaults['sharpen_regularization'], label='Sharpening regularization', min=0.001, group='Advanced', advanced=True),
            ParamField('pinhole_stack', 'bool', defaults['pinhole_stack'], label='Pinhole stack', group='ISM'),
            ParamField('scan_steps_fast', 'int', defaults['scan_steps_fast'], label='Steps, fast axis', min=0, group='Scanning', advanced=True),
            ParamField('scan_steps_slow', 'int', defaults['scan_steps_slow'], label='Steps, slow axis', min=0, group='Scanning', advanced=True),
            ParamField('scan_step_nm', 'float', defaults['scan_step_nm'], label='Step', suffix='nm', min=0.0, group='Scanning', advanced=True),
        )

    def __init__(self):
        self._logger = initLogger('MonalisaReconstructor')
        self.session_registry = MonalisaSessionRegistry()
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
        return MonalisaParamsWidget(parent, session_registry=self.session_registry)
    
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
        
        # Find pattern
        row_offset, col_offset, row_period, col_period = self._pattern_finder.find(test_frame)
        
        # Update widget
        param_widget.set_pattern_params(row_offset, col_offset, row_period, col_period)
        
        self._logger.info(f'Pattern found: row_offset={row_offset:.2f}, col_offset={col_offset:.2f}, '
                         f'row_period={row_period:.2f}, col_period={col_period:.2f}')
    
    #: ``scan_params`` comes from the file (or an explicit override), not a widget.
    extra_param_keys = ("scan_params",)

    def prepare_params(self, data_obj, params: dict | None) -> dict:
        """Fill ``scan_params`` from the acquisition attributes when absent.

        The GUI derives the scan geometry from the file the moment it is
        opened and hands it to ``process`` inside ``params``; a headless run
        does the same here, through the same pure function.
        """
        from .scan_params import DEFAULT_LABELS, default_scan_params, scan_params_for_source

        params = dict(params or {})
        if params.get('scan_params') is None:
            params['scan_params'] = scan_params_for_source(
                default_scan_params(DEFAULT_LABELS), data_obj, DEFAULT_LABELS,
            )
        return params

    @staticmethod
    def _manual_localization(params: dict) -> PatternLocalization | None:
        mode = str(params.get("localization_mode", LOCALIZATION_AUTO))
        if mode == LOCALIZATION_AUTO:
            return None
        if mode != LOCALIZATION_MANUAL:
            raise ValueError(f"Localization mode {mode!r} is not Manual")
        axis = str(params.get("stagger_axis", "None"))
        return PatternLocalization.manual_grid(
            period_x=float(params["manual_period_x_px"]),
            period_y=float(params["manual_period_y_px"]),
            offset_x=float(params["manual_offset_x_px"]),
            offset_y=float(params["manual_offset_y_px"]),
            stagger_axis=None if axis.lower() == "none" else axis.lower(),
            stagger=float(params.get("stagger", 0.0)),
            source="manual",
        )

    @classmethod
    def _resolved_localization(cls, params: dict) -> PatternLocalization | None:
        """The explicit localization carried by the current settings, if any."""
        mode = str(params.get("localization_mode", LOCALIZATION_AUTO))
        if mode == LOCALIZATION_MANUAL:
            return cls._manual_localization(params)
        if mode == LOCALIZATION_AUTO:
            return PatternLocalization.from_dict(params.get("auto_localization"))
        if mode == LOCALIZATION_LOCKED:
            localization = PatternLocalization.from_dict(params.get("locked_localization"))
            if localization is None:
                raise ValueError("Localization is Locked but no locked localization is selected")
            return localization
        raise ValueError(f"Unknown localization mode {mode!r}")

    @staticmethod
    def _spot_sigma_for_params(params: dict) -> float | None:
        if str(params.get("spot_width_mode", SPOT_MANUAL)) != SPOT_MANUAL:
            return None
        return fwhm_to_sigma_px(
            params["spot_fwhm"], params["spot_fwhm_unit"], params.get("pixel_size_nm")
        )

    @classmethod
    def _localization_pipeline_params(cls, params: dict) -> PipelineParams:
        return PipelineParams(
            spot_sigma_px=cls._spot_sigma_for_params(params),
            localization_refinement=str(
                params.get("localization_refinement", LOCALIZATION_REFINEMENT_FAST)
            ),
            localization_settings=LocalizationSettings.from_dict(
                params.get("localization_settings")
            ),
        )

    @staticmethod
    def _calibration_frames(data_obj, frames: np.ndarray, params: dict) -> np.ndarray:
        """One representative in-plane scan for Auto localization/calibration.

        A recording may contain Z planes, timepoints or conditions.  Their
        intensity/defocus differences are not scan modulation and must not be
        folded into the temporal variance used to estimate the Gaussian spot.
        Prefer the resolved acquisition layout; older recordings fall back to
        the legacy scan metadata.
        """
        from imswitch.improcess.reconstructors.monalisa_lattice.scan import (
            layout_scan, recorded_scan,
        )

        num_frames = int(np.asarray(frames).shape[0])
        groups = None
        try:
            scan = layout_scan(getattr(data_obj, "acquisition_layout", None), num_frames)
        except ValueError:
            # A declared incompatible layout should keep its normal explicit
            # failure during reconstruction; localization itself can still be
            # attempted from the frames rather than mixing arbitrary stacks.
            scan = None
        if scan is not None:
            groups = [scan.frames_of(stack) for stack in range(scan.num_stacks)]
        else:
            attrs = dict(getattr(data_obj, "attrs", None) or {})
            try:
                legacy = recorded_scan(
                    attrs, num_frames,
                    num_fast=int(params.get("scan_steps_fast", 0) or 0),
                    num_slow=int(params.get("scan_steps_slow", 0) or 0),
                    step_nm=float(params.get("scan_step_nm", 0.0) or 0.0),
                )
            except ValueError:
                legacy = None
            if legacy is not None and legacy.num_stacks > 1:
                size = legacy.frames_per_stack
                groups = [
                    np.arange(stack * size, (stack + 1) * size, dtype=int)
                    for stack in range(legacy.num_stacks)
                ]

        if not groups or len(groups) == 1:
            return np.asarray(frames)
        chosen = most_modulated_indices(frames, groups)
        return np.asarray(frames)[chosen]

    def localize(self, data_obj, params: dict | None = None) -> tuple[PatternLocalization, str, dict]:
        """Explicit Auto-localization action used by the parameter widget.

        Loading/viewing data never calls this.  Reconstruction may still run
        Auto localization when no explicit result has been prepared.
        """
        merged = unified_default_params()
        merged.update(dict(params or {}))
        preloaded = bool(getattr(data_obj, "dataLoaded", True))
        try:
            data_obj.checkAndLoadData()
            frames = np.asarray(data_obj.data)
        finally:
            if not preloaded and hasattr(data_obj, "checkAndUnloadData"):
                data_obj.checkAndUnloadData()
        if frames.ndim != 3:
            raise ValueError(f"Expected (frames, rows, cols), got {frames.shape}")
        pipeline_params = self._localization_pipeline_params(merged)
        calibration_frames = self._calibration_frames(data_obj, frames, merged)
        finding = find_localization(
            calibration_frames.mean(axis=0, dtype=np.float64),
            calibration_frames.var(axis=0, dtype=np.float64)
            if needs_modulation_frame(pipeline_params) else None,
            pipeline_params,
        )
        source = (
            "auto-fast" if finding.calibration.name.endswith("-fast") else "auto"
        )
        localization = PatternLocalization.from_lattice(
            finding.lattice, source=source, dataset=getattr(data_obj, "name", "")
        )
        self.session_registry.set_current_localization(
            detector_identity(data_obj), localization, source=source,
            source_dataset=str(getattr(data_obj, "name", "") or ""),
            source_path=str(getattr(data_obj, "dataPath", "") or ""),
            diagnostics={"description": finding.describe()},
        )
        return localization, finding.describe(), {
            "spot_sigma_px": float(finding.spot.shared_sigma),
        }

    @staticmethod
    def _orientation_literal(params: dict):
        mode = str(params.get("orientation_mode", ORIENTATION_AUTO))
        if mode == ORIENTATION_AUTO:
            return None
        if mode == ORIENTATION_MANUAL:
            value = str(params.get("orientation", "+x+y"))
        elif mode == ORIENTATION_LOCKED:
            value = str(params.get("locked_orientation") or "")
            if not value:
                raise ValueError("Orientation is Locked but no detector orientation is locked")
        else:
            raise ValueError(f"Unknown orientation mode {mode!r}")
        return ScanOrientation.from_literal(value).to_literal()

    @classmethod
    def _fast_update_signature(
        cls, params: dict, *, localization: PatternLocalization | None = None,
        spot_sigma_px: float | None = None,
    ) -> dict:
        """Canonical state that must stay unchanged for placement-only update."""
        if localization is None:
            localization = cls._resolved_localization(params)
        if spot_sigma_px is None:
            spot_sigma_px = cls._spot_sigma_for_params(params)
        return {
            "localization": None if localization is None else localization.to_dict(),
            "spot_sigma_px": None if spot_sigma_px is None else float(spot_sigma_px),
            "pixel_size_nm": float(params.get("pixel_size_nm", 0.0) or 0.0),
            "background": str(params.get("background", "constant")),
            "scan_steps_fast": int(params.get("scan_steps_fast", 0) or 0),
            "scan_steps_slow": int(params.get("scan_steps_slow", 0) or 0),
            "scan_step_nm": float(params.get("scan_step_nm", 0.0) or 0.0),
            "placement_mode": str(params.get("placement_mode", PLACEMENT_AUTO)),
            "commensurate_tolerance": float(params.get("commensurate_tolerance", 0.05)),
            "variance_weighting": bool(params.get("variance_weighting", False)),
            "intensity_correction": bool(params.get("intensity_correction", False)),
            "cell_offsets": bool(params.get("cell_offsets", False)),
            "reassignment": str(params.get("reassignment", "off")),
            "pinhole_stack": bool(params.get("pinhole_stack", False)),
            "sharpen": bool(params.get("sharpen", False)),
        }

    def update_reconstruction(self, result, params: dict):
        """Fast placement-only refresh, primarily for visual orientation checks."""
        from imswitch.improcess.reconstructors.monalisa_lattice.reconstructor import (
            MonalisaLatticeReconstructor,
        )

        merged = unified_default_params()
        merged.update(dict(params or {}))
        if str(merged.get("preset", "Fast")) != "Fast":
            raise ValueError(
                "Update reconstruction is currently available only in Fast mode."
            )
        expected = getattr(result, "_monalisa_fast_update_signature", None)
        if expected is None or not hasattr(result, "_fast_placement_cache"):
            raise ValueError(
                "This result has no retained Fast extraction. Run Fast reconstruction first."
            )
        current = self._fast_update_signature(merged)
        if current != expected:
            raise ValueError(
                "Localization, spot model, scan or extraction settings changed. "
                "Run a full reconstruction before using Update reconstruction."
            )

        orientation = self._orientation_literal(merged)
        if orientation is None:
            orientation = str(
                (getattr(result, "diagnostics", {}) or {}).get("resolved_orientation")
                or (getattr(result, "diagnostics", {}) or {}).get("orientation")
                or merged.get("orientation", "+x+y")
            )
        updated = MonalisaLatticeReconstructor.update_fast_placement(
            result, orientation
        )
        updated.name = result.name
        updated.params = dict(merged)
        updated._monalisa_fast_update_signature = copy.deepcopy(expected)

        snapshot = dict(
            ((getattr(result, "metadata", {}) or {}).get("monalisa_calibration") or {})
        )
        snapshot.update(
            orientation_source=str(merged.get("orientation_mode", ORIENTATION_AUTO)),
            resolved_orientation=str(orientation),
            placement_mode_used=str(updated.diagnostics.get("placement_method") or ""),
            placement_residual_px=updated.diagnostics.get("placement_residual_px"),
        )
        updated.params["calibration_snapshot"] = copy.deepcopy(snapshot)
        metadata = dict(getattr(result, "metadata", {}) or {})
        metadata["monalisa_calibration"] = copy.deepcopy(snapshot)
        updated.metadata = metadata
        return updated

    def _process_unified_gaussian(self, data_obj, params: dict, context=None):
        """Run the experimental/general lattice orchestration as the one Gaussian plugin.

        ``MonalisaLatticeReconstructor`` is retained as a compatibility wrapper
        during Patch 2; the mathematical pipeline it calls already lives under
        ``reconstructors.monalisa``.  Patch 4 removes the wrapper after stored
        settings have a migration path.
        """
        from imswitch.improcess.reconstructors.monalisa_lattice.params_widget import AUTO
        from imswitch.improcess.reconstructors.monalisa_lattice.reconstructor import (
            MonalisaLatticeReconstructor,
        )

        merged = unified_default_params()
        merged.update(dict(params or {}))
        localization = self._resolved_localization(merged)
        orientation = self._orientation_literal(merged)

        spot_sigma_px = self._spot_sigma_for_params(merged)

        placement = str(merged.get("placement_mode", PLACEMENT_AUTO)).lower()
        if placement == "interpolate":
            placement = "interpolate"
        elif placement == "direct":
            placement = "direct"
        else:
            placement = "auto"

        lattice_params = {
            **MonalisaLatticeReconstructor.default_params(),
            "scan_steps_fast": int(merged.get("scan_steps_fast", 0)),
            "scan_steps_slow": int(merged.get("scan_steps_slow", 0)),
            "scan_step_nm": float(merged.get("scan_step_nm", 0.0)),
            "pixel_size_nm": float(merged.get("pixel_size_nm", 0.0)),
            "orientation": AUTO if orientation is None else orientation,
            "reuse_localization": False,
            # Unified MoNaLISA owns reuse through the session registry; do not
            # populate the experimental plugin's temporary single-entry cache.
            "_cache_localization_reference": False,
            "background": str(merged.get("background", "constant")),
            "frame_gain": bool(merged.get("intensity_correction", False)),
            "cell_offsets": bool(merged.get("cell_offsets", False)),
            "reassignment": str(merged.get("reassignment", "off")),
            "shift_factor": float(merged.get("shift_factor", 0.0)),
            "reassignment_asymmetric": bool(merged.get("reassignment_asymmetric", False)),
            "shift_factor_x": float(merged.get("shift_factor_x", merged.get("shift_factor", 0.0))),
            "shift_factor_y": float(merged.get("shift_factor_y", merged.get("shift_factor", 0.0))),
            "sharpen": bool(merged.get("sharpen", False)),
            "sharpen_sigma_px": float(merged.get("sharpen_sigma_px", 1.5)),
            "sharpen_regularization": float(merged.get("sharpen_regularization", 0.1)),
            "pinhole_stack": bool(merged.get("pinhole_stack", False)),
            "variance_weighting": bool(merged.get("variance_weighting", False)),
            "commensurate_tolerance": float(merged.get("commensurate_tolerance", 0.05)),
            "placement_mode": placement,
            "device": str(merged.get("device", "GPU")),
            "spot_sigma_px": spot_sigma_px,
            "localization_refinement": str(
                merged.get("localization_refinement", LOCALIZATION_REFINEMENT_FAST)
            ),
            "localization_settings": LocalizationSettings.from_dict(
                merged.get("localization_settings")
            ),
            "_pattern_localization": (
                None if localization is None else localization.to_dict()
            ),
        }
        result = MonalisaLatticeReconstructor().process(data_obj, lattice_params, context=context)
        result.name = f"{data_obj.name}_monalisa"

        diagnostics = dict(getattr(result, "diagnostics", {}) or {})
        resolved_localization = PatternLocalization.from_dict(
            diagnostics.get("resolved_localization")
        ) or localization
        resolved_orientation = str(
            diagnostics.get("resolved_orientation") or orientation or ""
        )
        detector = detector_identity(data_obj)
        localization_mode = str(merged.get("localization_mode", LOCALIZATION_AUTO))
        orientation_mode = str(merged.get("orientation_mode", ORIENTATION_AUTO))

        if resolved_localization is not None and localization_mode == LOCALIZATION_AUTO:
            self.session_registry.set_current_localization(
                detector, resolved_localization, source="auto",
                source_dataset=str(getattr(data_obj, "name", "") or ""),
                source_path=str(getattr(data_obj, "dataPath", "") or ""),
                diagnostics={"status": "Resolved during reconstruction"},
            )
        if resolved_orientation and orientation_mode == ORIENTATION_AUTO:
            self.session_registry.set_current_orientation(
                detector, resolved_orientation, source="auto",
                source_dataset=str(getattr(data_obj, "name", "") or ""),
                source_path=str(getattr(data_obj, "dataPath", "") or ""),
            )

        effective_sigma_px = (
            float(diagnostics["resolved_spot_sigma_px"])
            if diagnostics.get("resolved_spot_sigma_px") is not None
            else (float(spot_sigma_px) if spot_sigma_px is not None else None)
        )
        resolved_fwhm_px = (
            sigma_px_to_fwhm(effective_sigma_px, "px", None)
            if effective_sigma_px is not None else None
        )
        pixel_nm = float(merged.get("pixel_size_nm", 0.0) or 0.0)
        resolved_fwhm_nm = (
            sigma_px_to_fwhm(effective_sigma_px, "nm", pixel_nm)
            if effective_sigma_px is not None and pixel_nm > 0 else None
        )

        calibration_snapshot = {
            "detector": detector,
            "localization_source": localization_mode,
            "resolved_localization": (
                resolved_localization.to_dict() if resolved_localization is not None else {}
            ),
            "locked_localization_id": str(merged.get("locked_localization_id") or ""),
            "locked_localization_name": str(merged.get("locked_localization_name") or ""),
            "orientation_source": orientation_mode,
            "resolved_orientation": resolved_orientation,
            "spot_width_source": str(merged.get("spot_width_mode", SPOT_MANUAL)),
            "spot_fwhm": float(merged.get("spot_fwhm", 0.0)),
            "spot_fwhm_unit": str(merged.get("spot_fwhm_unit", "nm")),
            "requested_spot_fwhm": float(merged.get("spot_fwhm", 0.0)),
            "requested_spot_fwhm_unit": str(merged.get("spot_fwhm_unit", "nm")),
            "resolved_spot_fwhm_px": resolved_fwhm_px,
            "resolved_spot_fwhm_nm": resolved_fwhm_nm,
            "effective_sigma_px": effective_sigma_px,
            "resolved_shift_factor": diagnostics.get("resolved_shift_factor"),
            "resolved_shift_factor_xy": diagnostics.get("resolved_shift_factor_xy"),
            "preset": str(merged.get("preset", "")),
            "placement_mode_requested": str(merged.get("placement_mode", PLACEMENT_AUTO)),
            "placement_mode_used": str(diagnostics.get("placement_method") or ""),
            "placement_residual_px": diagnostics.get("placement_residual_px"),
            "placement_tolerance_px": float(merged.get("commensurate_tolerance", 0.05)),
            "corrections": {
                "intensity_correction": bool(merged.get("intensity_correction", False)),
                "cell_offsets": bool(merged.get("cell_offsets", False)),
                "reassignment": str(merged.get("reassignment", "off")),
                "shift_factor": float(merged.get("shift_factor", 0.0)),
                "reassignment_asymmetric": bool(merged.get("reassignment_asymmetric", False)),
                "shift_factor_x": float(merged.get("shift_factor_x", merged.get("shift_factor", 0.0))),
                "shift_factor_y": float(merged.get("shift_factor_y", merged.get("shift_factor", 0.0))),
                "variance_weighting": bool(merged.get("variance_weighting", False)),
                "sharpen": bool(merged.get("sharpen", False)),
            },
        }

        # Keep the user-facing unified settings and an immutable calibration
        # snapshot with the result.  The metadata copy is what generic result
        # writers persist beside the pixels, so later profile renames/deletes
        # cannot make an old reconstruction ambiguous.
        result.params = dict(merged)
        result.params["calibration_snapshot"] = copy.deepcopy(calibration_snapshot)
        metadata = dict(getattr(result, "metadata", {}) or {})
        metadata["monalisa_calibration"] = copy.deepcopy(calibration_snapshot)
        result.metadata = metadata
        if str(merged.get("preset", "Fast")) == "Fast" and hasattr(
            result, "_fast_placement_cache"
        ):
            result._monalisa_fast_update_signature = self._fast_update_signature(
                merged, localization=resolved_localization,
                spot_sigma_px=effective_sigma_px,
            )
        return result

    def detection_preview(self, data_obj, displayed_image, params: dict):
        """Overlay the exact localization the unified reconstructor will use."""
        if data_obj is None:
            return None
        merged = unified_default_params()
        merged.update(dict(params or {}))
        image = np.asarray(displayed_image)
        rows, cols = image.shape[-2:]
        localization = self._resolved_localization(merged)
        if localization is not None:
            x, y = localization.points_in_frame(rows, cols)
            if str(merged.get("localization_mode")) == LOCALIZATION_MANUAL:
                stagger = str(merged.get("stagger_axis", "None"))
                status = (
                    f"Manual: {x.size} foci; X {merged['manual_period_x_px']:.3g} px, "
                    f"Y {merged['manual_period_y_px']:.3g} px"
                    + ("" if stagger == "None" else f", stagger {stagger} {merged.get('stagger', 0.0):+.3g}")
                )
            else:
                status = str(localization.provenance.get("status") or "Auto localization")
            return DetectionPreview(x, y, status, marker="point")
        # Auto preview is intentionally passive.  Expensive localization only
        # happens when the user presses Localize, or when reconstruction itself
        # needs an unresolved Auto localization.
        return DetectionPreview(
            np.array([], dtype=float), np.array([], dtype=float),
            "Auto: press Localize", marker="point",
        )

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
            Processing result containing the reconstructed image.
        """
        # New unified Gaussian parameter dictionaries have no internal
        # ``reconstruction_method`` selector.  Keep accepting that key for the
        # migration window so old Fast/Legacy saved settings still run until
        # Patch 4 translates them explicitly.
        if "reconstruction_method" not in params:
            return self._process_unified_gaussian(data_obj, params, context=context)

        # Extract scan parameters (required for the compatibility path)
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
            return self._process_fast_gauss_offline(
                data_obj.name,
                data,
                params,
                scan_params,
                data_attrs,
                resolved=getattr(data_obj, 'acquisition_layout', None),
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
        placement = self._placement_for(data_obj, coeffs.shape[1])
        if placement is not None:
            self._logger.info(
                f'Using the recorded acquisition layout: '
                f'{placement.rows}x{placement.cols} scan positions, '
                f'{placement.n_conditions} condition(s), {placement.n_time} timepoint(s).'
            )
        elif params.get('auto_scan_orientation', True):
            # Only guess the orientation when the recording cannot state it.
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
            placement=placement,
        )

        self._logger.info(f'Reconstruction complete: shape {result.data.shape}')
        return result

    def _placement_for(self, data_obj, frames: int):
        """Recorded output coordinates for this source, or ``None``.

        The scan-parameter dialog is consulted only when the recording cannot
        describe itself. A layout that disagrees with the stored frames is an
        error, not a reason to fall back to arithmetic over the frame count --
        that arithmetic is what read an 18x18 two-line-step scan as two
        contiguous 324-frame time blocks.
        """
        resolved = getattr(data_obj, 'acquisition_layout', None)
        if not isinstance(resolved, ResolvedAcquisitionLayout):
            return None
        if not resolved.is_usable:
            self._logger.info(
                'Acquisition layout is low-confidence; using the scan dialog.'
            )
            return None
        try:
            placement = placement_from_layout(resolved.layout)
        except UnconsumedLoopError as error:
            if resolved.is_authoritative:
                raise ValueError(str(error)) from error
            self._logger.info(
                'Acquisition layout is inferred and has a loop MoNaLISA cannot '
                f'place; using the scan dialog. {error}'
            )
            return None
        if placement is None:
            return None
        if len(placement.slots) != frames:
            raise ValueError(
                f'The acquisition layout records {len(placement.slots)} frames '
                f'but signal extraction produced {frames}. Frames are never '
                f'dropped or padded to fit a scan shape.'
            )
        return placement

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
        if len(results) == 1:
            return results[0]

        first = results[0]
        # During the compatibility phase the unified Gaussian plugin reuses
        # the lattice result container.  Consolidate it with the same general
        # geometry-aware rules instead of forcing it through the old fixed-6D
        # Fast-Gauss result contract.
        try:
            from imswitch.improcess.reconstructors.monalisa_lattice.result import (
                MonalisaLatticeResult,
            )
        except Exception:
            MonalisaLatticeResult = ()
        if isinstance(first, MonalisaLatticeResult):
            from imswitch.improcess.reconstructors.monalisa_lattice.reconstructor import (
                MonalisaLatticeReconstructor,
            )
            merged = MonalisaLatticeReconstructor().consolidate(results)
            snapshots = [
                dict((getattr(result, "metadata", {}) or {}).get("monalisa_calibration") or {})
                for result in results
            ]
            snapshots = [snapshot for snapshot in snapshots if snapshot]
            if snapshots:
                payload = (
                    snapshots[0]
                    if all(snapshot == snapshots[0] for snapshot in snapshots[1:])
                    else {"per_data_item": snapshots}
                )
                metadata = dict(getattr(merged, "metadata", {}) or {})
                metadata["monalisa_calibration"] = copy.deepcopy(payload)
                merged.metadata = metadata
                merged.params = dict(getattr(merged, "params", {}) or {})
                merged.params["calibration_snapshot"] = copy.deepcopy(payload)
            return merged
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
        resolved=None,
    ) -> MonalisaProcessingResult:
        """
        Run the fast-Gauss MoNaLISA path on a complete offline stack.

        A recording of contiguous X/Y stacks, one per timepoint, goes through
        the live session, as it would have live. Whatever else the recorded
        layout describes -- Z planes, an axis the scan does not step, a
        serpentine or gated scan, conditions not interleaved per line -- is
        reconstructed by :meth:`_fast_gauss_by_placement`: the same Gaussian
        extraction, with every frame put where the layout records it.
        """
        # The recording outranks the dialog and the shape arithmetic: it either
        # supplies the geometry or says the live path cannot represent it.
        recorded_geometry = self._fast_gauss_geometry_from_layout(
            resolved, data_attrs or {}, data.shape[0]
        )
        if recorded_geometry is not None and 'placement' in recorded_geometry:
            return self._fast_gauss_by_placement(
                name, data, params, recorded_geometry['placement'], resolved,
                recorded_geometry['reasons'],
            )
        if recorded_geometry is not None:
            geometry = recorded_geometry
            frames_per_stack = geometry['frames_per_stack']
        else:
            geometry = self._fast_gauss_geometry_from_scan_params(scan_params)
            frames_per_stack = geometry['frames_per_stack']
            expected_frames = frames_per_stack * geometry['num_timepoints']
            if data.shape[0] != expected_frames:
                metadata_geometry = self._fast_gauss_geometry_from_attrs(
                    data_attrs or {}, data.shape[0]
                )
                if metadata_geometry is None:
                    raise ValueError(
                        'Fast Gauss MoNaLISA expected '
                        f'{expected_frames} frames ({frames_per_stack} per timepoint x '
                        f'{geometry["num_timepoints"]} timepoints), got {data.shape[0]}'
                    )
                geometry = metadata_geometry
                frames_per_stack = geometry['frames_per_stack']

        session = self.make_session()
        try:
            first_stack = data[:frames_per_stack]
            session_params = self._fast_gauss_session_params(params)
            stack_info = None
            if recorded_geometry is not None:
                # Same resolved contract the live path consumes, so the session
                # sizes itself from the layout and cross-checks its detected
                # orientation against it.
                from imswitch.improcess.reconstructors.base import StackInfo

                stack_info = StackInfo(
                    frame_shape=tuple(int(v) for v in data.shape[-2:]),
                    dtype=np.dtype(data.dtype),
                    acquisition_layout=resolved,
                )
            init_obj = StreamInit(
                name=name,
                dataset_name='offline',
                data=first_stack,
                attrs=geometry['attrs'],
                stack_info=stack_info,
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

        # The image was assembled with the orientation the session detected;
        # say so instead of carrying the geometry's '+' placeholders.
        result_scan_params = dict(geometry.get('scan_params', scan_params))
        if getattr(session, 'scan_params', None):
            result_scan_params['directions'] = list(session.scan_params['directions'])
            result_scan_params['unidirectional'] = session.scan_params['unidirectional']
        result = MonalisaProcessingResult(
            name=name,
            data=live_result.data,
            scan_params=result_scan_params,
            display_levels=display_levels,
            output_pixel_size_nm=out_px,
            axis_label_map=self._axis_labels,
        )
        self._logger.info(f'Fast Gauss reconstruction complete: shape {result.data.shape}')
        return result

    def _fast_gauss_by_placement(
        self, name, data, params, placement, resolved, reasons
    ) -> MonalisaProcessingResult:
        """Fast-Gauss extraction with every frame placed by the recorded layout.

        The live session scatters contiguous X/Y stacks through a precomputed
        index map and reads the scan's orientation off the data. This path
        keeps its extraction -- the same pattern, footprint and Gaussian
        least squares -- and assembles the image the way the classic path
        does: ``coeffs_to_image_from_placement`` puts the foci of every frame
        at the ``(t, z, y, x)`` the acquisition layout records, so Z planes
        become the Z axis and an axis the scan does not step is one pixel
        per lattice period. Directions come from the layout, applied by the
        one helper in imcommon that interprets them, as for the classic path.
        """
        from .coeffs_to_image import coeffs_to_image_from_placement
        from .gauss_processor import make_fast_gauss_extractor
        from .live_session import expected_orientation_signs
        from .scan_geometry import scan_params_from_layout

        self._logger.info(
            f'{name}: placing every frame by the acquisition layout '
            f'({", ".join(reasons)}): {placement.rows}x{placement.cols} scan '
            f'positions, {placement.slices} Z plane(s), {placement.n_time} '
            f'timepoint(s), {placement.n_conditions} condition(s).'
        )
        session = self.make_session()
        session_params = self._fast_gauss_session_params(params)
        working = data
        if params.get('bleaching_correction', False):
            working = session._apply_bleaching_correction(data)
        localization = MonalisaLiveSession._resolve_localization(working, session_params)
        processor = make_fast_gauss_extractor(
            localization.xp, localization.xo, localization.yp, localization.yo,
            localization.nx_c, localization.ny_c,
            placement.cols, placement.rows,
            localization.num_rows, localization.num_cols,
            gaussian_sigma_px=session._resolve_gaussian_sigma_px(params, working, localization),
            fit_background=session._resolve_fit_background(params),
            use_gpu=str(params.get('device', '')).lower() == 'gpu',
        )
        self._logger.info(
            f'{name}: localized xp={localization.xp:.2f}, xo={localization.xo:.2f}, '
            f'yp={localization.yp:.2f}, yo={localization.yo:.2f}, '
            f'nx_c={localization.nx_c}, ny_c={localization.ny_c}'
        )
        num_frames = working.shape[0]
        coeffs = np.empty(
            (num_frames, localization.ny_c, localization.nx_c), dtype=np.float32
        )
        chunk = max(1, int(FAST_GAUSS_PLACEMENT_CHUNK_BYTES // max(working[0].nbytes, 1)))
        for start in range(0, num_frames, chunk):
            amplitudes = processor.process_chunk(working[start:start + chunk])
            coeffs[start:start + chunk] = np.asarray(amplitudes).reshape(
                -1, localization.ny_c, localization.nx_c
            )
        # The layout places every frame along the stage axes; whether each
        # axis runs up or down the camera is read from the frames, as the
        # live session reads it, so that the same recording comes out the
        # same way whether it has one plane or many.
        placement, flips = self._placement_orientation(coeffs, placement, name)
        expected = expected_orientation_signs(resolved.layout) or {}
        if any(flips.values()):
            disagreeing = [axis for axis, flipped in flips.items() if flipped]
            self._logger.warning(
                f'{name}: the frames assemble smoother with the {" and ".join(disagreeing)} '
                f'axis mirrored against the recorded layout (layout says {expected}); '
                'using the mirrored placement. If the layout is right, check the '
                'frames; if the detection is right, check isPositiveDirection in '
                'the setup file.'
            )
        image = coeffs_to_image_from_placement(coeffs, placement)
        data6 = image[np.newaxis, np.newaxis]        # (Dataset, Base, T, Z, Y, X)

        layout = resolved.layout
        pixel_size_nm = float(params.get('pixel_size_nm') or 0.0)

        def axis_step_nm(kind: str, count: int, period_px: float) -> float:
            loop = next((loop for loop in layout.event_loops if loop.kind == kind), None)
            if count > 1 and loop is not None and loop.step:
                return float(loop.step) * 1000.0       # the layout holds micrometres
            # An axis the scan does not step: one output pixel per period.
            return float(period_px * pixel_size_nm) if pixel_size_nm > 0 else 1.0

        step_x_nm = axis_step_nm('scan_x', placement.cols, localization.xp)
        step_y_nm = axis_step_nm('scan_y', placement.rows, localization.yp)
        scan_params = scan_params_from_layout(resolved, self._axis_labels)
        if scan_params is None:
            scan_params = {
                'dimensions': [
                    self._axis_labels['r_l_text'], self._axis_labels['u_d_text'],
                    self._axis_labels['b_f_text'], self._axis_labels['timepoints_text'],
                ],
                'directions': ['pos', 'pos', 'pos'],
                'steps': [placement.cols, placement.rows, placement.slices,
                          placement.timepoints],
                'step_sizes': [step_x_nm, step_y_nm, 1.0, 1.0],
                'n_linesteps': placement.n_conditions,
                'unidirectional': True,
            }
        else:
            scan_params = dict(scan_params)
            step_sizes = list(scan_params['step_sizes'])
            directions = list(scan_params['directions'])
            for label, size, axis in (
                (self._axis_labels['r_l_text'], step_x_nm, 'x'),
                (self._axis_labels['u_d_text'], step_y_nm, 'y'),
            ):
                index = scan_params['dimensions'].index(label)
                step_sizes[index] = str(size)
                if flips.get(axis) and index < len(directions):
                    # What was used to assemble the image, not what was recorded.
                    directions[index] = (
                        self._axis_labels['n_text']
                        if directions[index] == self._axis_labels['p_text']
                        else self._axis_labels['p_text']
                    )
            scan_params['step_sizes'] = step_sizes
            scan_params['directions'] = directions
        finite = data6[np.isfinite(data6)]
        display_levels = None
        if finite.size:
            display_levels = (
                float(np.percentile(finite, 1)), float(np.percentile(finite, 99.9))
            )
        result = MonalisaProcessingResult(
            name=name,
            data=data6,
            scan_params=scan_params,
            display_levels=display_levels,
            output_pixel_size_nm=(step_y_nm, step_x_nm),
            axis_label_map=self._axis_labels,
            placement=placement,
        )
        self._logger.info(
            f'Fast Gauss reconstruction by placement complete: shape {result.data.shape}'
        )
        return result

    @staticmethod
    def _placement_orientation(coeffs: np.ndarray, placement, name: str = ''):
        """The placement, mirrored along the axes the frames say run the other way.

        Scored on the plane with the most signal, by the contrast of the
        cell borders along the scanned axes (the whole-image roughness the
        live session uses cannot tell the mirrors of a one-axis scan apart).
        Returns ``(placement, {'x': flipped, 'y': flipped})``.
        """
        from .coeffs_to_image import (
            coeffs_to_image_from_placement,
            flip_placement,
            placement_seam_contrast,
        )

        slots = np.asarray(placement.slots, dtype=int)
        if slots.size == 0:
            return placement, {'x': False, 'y': False}
        # The (t, z) plane whose frames vary the most: the one with signal.
        plane_of = slots[:, 0] * placement.slices + slots[:, 1]
        spread = np.zeros(placement.timepoints * placement.slices)
        for plane in np.unique(plane_of):
            members = coeffs[plane_of == plane]
            spread[plane] = float(members.var(axis=0, dtype=np.float64).mean())
        plane = int(np.argmax(spread))
        members = plane_of == plane
        one_plane = type(placement)(
            slots=tuple((0, 0, int(y), int(x)) for _t, _z, y, x in slots[members]),
            timepoints=1, slices=1, rows=placement.rows, cols=placement.cols,
            condition_labels=(), n_conditions=1, n_time=1,
        )
        axes = {'x': placement.cols > 1, 'y': placement.rows > 1}
        scores = {}
        for flip_x in ((False, True) if axes['x'] else (False,)):
            for flip_y in ((False, True) if axes['y'] else (False,)):
                candidate = flip_placement(one_plane, flip_x, flip_y)
                image = coeffs_to_image_from_placement(coeffs[members], candidate)[0, 0]
                scores[(flip_x, flip_y)] = placement_seam_contrast(
                    image, placement.rows, placement.cols
                )
        finite = {key: value for key, value in scores.items() if np.isfinite(value)}
        if not finite:
            return placement, {'x': False, 'y': False}
        flip_x, flip_y = min(finite, key=finite.get)
        return (
            flip_placement(placement, flip_x, flip_y),
            {'x': bool(flip_x), 'y': bool(flip_y)},
        )

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

    def _fast_gauss_geometry_from_layout(
        self, resolved, attrs: dict, num_frames: int
    ) -> dict | None:
        """Derive fast-Gauss geometry from the recorded layout, or ``None``.

        The fast path assembles one contiguous stack per timepoint. Line-step
        conditions are part of that stack when they are interleaved per line
        -- ``[line 0 / A][line 0 / B][line 1 / A]...`` -- which is the order
        the Advanced producer and its legacy adapter record; the session
        de-interleaves by ``n_linesteps``. Anything else the recording says
        the path cannot honour -- conditions laid out any other way,
        serpentine traversal, a gated detector, a Z loop -- is rejected here.
        Reassembling those as if they were timepoints is what turned an 18x18
        two-condition scan into two 324-frame time blocks.

        A layout with conditions used to be declined outright, which sent
        every legacy line-step file to the attribute ladder; the ladder then
        sized stacks as ``nx * ny`` and reproduced exactly that bug.
        """
        if not isinstance(resolved, ResolvedAcquisitionLayout):
            return None
        if not resolved.is_usable:
            return None
        layout = resolved.layout
        try:
            placement = placement_from_layout(layout)
        except UnconsumedLoopError as error:
            # A loop this path cannot place: refuse a declared layout, decline
            # an inferred one so the older ladder still gets its chance.
            if resolved.is_authoritative:
                raise ValueError(str(error)) from error
            return None
        if placement is None:
            return None

        unsupported = []
        if (
            placement.n_conditions > 1
            and not linestep_conditions_interleave_per_line(layout)
        ):
            unsupported.append(
                f'{placement.n_conditions} line-step conditions that are not '
                f'interleaved per line'
            )
        if placement.slices > 1:
            unsupported.append(f'{placement.slices} Z slices')
        if any(rule.order != 'forward' for rule in layout.traversal):
            unsupported.append('a reversed or serpentine fast axis')
        if layout.recorded_event_spans is not None:
            unsupported.append('a detector gated to part of the scan')
        if placement.cols == 1 or placement.rows == 1:
            unsupported.append('a scan of one axis only')
        if len(placement.slots) != num_frames:
            raise ValueError(
                f'The acquisition layout records {len(placement.slots)} frames '
                f'but this source has {num_frames}.'
            )
        if unsupported:
            if not resolved.is_authoritative:
                # An inferred shape the live path cannot represent is declined,
                # so the older metadata ladder still gets its chance; a
                # declared one is placed frame by frame instead.
                return None
            return {'placement': placement, 'reasons': unsupported}
        return self._fast_gauss_geometry_from_counts(
            attrs,
            placement.cols,
            placement.rows,
            num_frames,
            num_linesteps=placement.n_conditions,
        )

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
        num_linesteps: int | None = None,
    ) -> dict | None:
        # The layout's condition count outranks the ScanTTL attribute it may
        # have been derived from; the attribute is only for sources with no
        # usable layout.
        if num_linesteps is None:
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
        
        Uses 4th-power energy normalization: c = (E_0 / E_i)^4
        
        Args:
            data: 3D array (frames, rows, cols)
        
        Returns:
            Corrected 3D array
        """
        corrected_data = data.copy()
        energy = np.sum(data, axis=(1, 2))
        for i in range(data.shape[0]):
            c = (energy[0] / energy[i]) ** 4
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
