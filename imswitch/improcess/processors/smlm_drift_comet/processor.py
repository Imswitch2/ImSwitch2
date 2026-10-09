"""Drift correction through COMET (``comet-smlm`` on PyPI).

COMET (Cost-function Optimized Maximal Overlap Drift EsTimation,
https://github.com/gpufit/Comet) estimates one drift vector per time window
by maximising the overlap of every pair of localizations within
``max_drift_nm`` of each other across windows, refining the kernel width
from coarse to fine, and interpolating the window drifts to a per-frame
curve. It corrects x, y and z. It runs on numba-cuda, PyTorch or a
numba-compiled CPU kernel; COMET picks the fastest it finds unless told.

The package is optional. This processor imports it only when a run starts,
so the panel opens without it and says how to install it; a run without it
fails with the same message.

Every parameter default here is COMET's own (``comet_run_kd`` in
``comet-smlm`` 1.2.0) and is labelled as such. The one value COMET has no
default for, the window size, is left unset and the run is refused until
it is chosen: COMET's command line requires it and its examples disagree
(50 and 60 frames per window), because the right value depends on how many
localizations a dataset has per frame and how fast it drifts.
"""

from __future__ import annotations

import importlib.util
from typing import Callable

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model.cancellation import checkpoint
from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.model.param_spec import ParamField
from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.base import Processor
from imswitch.improcess.processors.smlm_drift.result import (
    DriftCorrectedLocalizationResult,
)

#: Import name of the ``comet-smlm`` distribution.
_PACKAGE = "comet"

INSTALL_HINT = 'pip install "imswitch2[comet]"'

#: Window (segmentation) modes, as ``comet_run_kd(segmentation_mode=...)``
#: numbers them. COMET's command line defaults to frames per window.
WINDOW_MODES: dict[str, int] = {"frames": 2, "localizations": 1, "windows": 0}

_WINDOW_MODE_LABELS: dict[str, str] = {
    "frames": "Frames per window",
    "localizations": "Localizations per window",
    "windows": "Number of windows",
}

#: ``comet_run_kd(mode=...)`` options plus ``auto`` (COMET's own choice,
#: :func:`comet.best_backend`). The ``_qc`` variants flag and drop windows
#: whose optimisation looks flawed.
BACKENDS: tuple[str, ...] = ("auto", "cuda", "cuda_qc", "torch", "torch_qc", "cpu")

INTERPOLATIONS: tuple[str, ...] = ("cubic", "catmull-rom")

_WINDOW_SIZE_UNSET = (
    "COMET has no default window size: set 'Window size' (frames per window, "
    "localizations per window or the number of windows). Enough windows to "
    "resolve the drift, enough localizations per window to constrain it."
)


def comet_installed() -> bool:
    """Whether ``comet-smlm`` can be imported, without importing it."""
    try:
        return importlib.util.find_spec(_PACKAGE) is not None
    except (ImportError, ValueError):
        return False


def _require_comet():
    try:
        import comet
    except ImportError as exc:
        raise RuntimeError(
            "COMET drift correction needs the optional 'comet-smlm' package "
            f"({INSTALL_HINT})"
        ) from exc
    return comet


def _optional_positive(value, cast):
    """``None`` (or anything non-positive) means "not set"."""
    if value is None:
        return None
    number = cast(value)
    return number if number > 0 else None


class SmlmCometDriftProcessor(Processor):
    """All-pairs drift correction through the optional COMET package."""

    name = "COMET drift correction"
    id = "smlm-drift-comet"
    category = "Localization"
    kinds = ("localization",)

    @classmethod
    def default_params(cls) -> dict:
        return {
            "window_mode": "frames",
            "window_size": None,
            "max_drift_nm": 300.0,
            "backend": "auto",
            "target_sigma_nm": 1.0,
            "initial_sigma_nm": None,
            "boxcar_width": 1,
            "interpolation": "cubic",
            "max_locs_per_window": None,
            "seed": None,
        }

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField(
                "window_mode", "select", "frames", label="Window mode",
                options=tuple(WINDOW_MODES),
                help="What 'Window size' counts (COMET segmentation_mode; "
                     "COMET's command line defaults to frames per window).",
            ),
            ParamField(
                "window_size", "int", None, label="Window size", min=0, max=None,
                nullable=True,
                help="Frames per window, localizations per window or the number of "
                     "windows, by 'Window mode'. COMET has no default: the run is "
                     "refused until it is set.",
            ),
            ParamField(
                "max_drift_nm", "float", 300.0, label="Max drift", min=1, max=1_000_000,
                decimals=1, suffix="nm",
                help="Largest drift expected over the acquisition; also the "
                     "neighbour-search radius (COMET default 300 nm).",
            ),
            ParamField(
                "backend", "select", "auto", label="Backend", options=BACKENDS,
                help="auto = the fastest COMET finds (numba-cuda, torch with CUDA, "
                     "else the CPU kernel). _qc variants drop windows whose fit "
                     "looks flawed.",
            ),
            ParamField(
                "target_sigma_nm", "float", 1.0, label="Target sigma", min=0.01,
                max=100_000, decimals=2, suffix="nm", advanced=True,
                help="Kernel width the refinement stops at (COMET default 1 nm).",
            ),
            ParamField(
                "initial_sigma_nm", "float", None, label="Initial sigma", min=0,
                max=1_000_000, decimals=1, suffix="nm", nullable=True, advanced=True,
                help="Kernel width the refinement starts from; unset = COMET's "
                     "rule, max drift / 3.",
            ),
            ParamField(
                "boxcar_width", "int", 1, label="Boxcar width", min=1, max=10_000,
                advanced=True,
                help="Temporal smoothing of the window drifts between optimizer "
                     "steps, in windows (COMET default 1 = none).",
            ),
            ParamField(
                "interpolation", "select", "cubic", label="Interpolation",
                options=INTERPOLATIONS, advanced=True,
                help="Spline from window drifts to per-frame drift (COMET default cubic).",
            ),
            ParamField(
                "max_locs_per_window", "int", None, label="Max localizations per window",
                min=0, max=None, nullable=True, advanced=True,
                help="Random cap per window to bound memory and time; unset = all "
                     "(COMET default). COMET lowers it by itself if the pair "
                     "search runs out of memory.",
            ),
            ParamField(
                "seed", "int", None, label="Seed", min=-1, max=None,
                nullable=True, advanced=True,
                help="Seed for the random cap, so a capped run repeats exactly; "
                     "unset = COMET's global random state.",
            ),
        )

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: isinstance(result, LocalizationResult)

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QFormLayout(widget)

        if not comet_installed():
            # Said in the panel rather than only at run time: a panel that
            # looks ready and then fails reads as a bug.
            note = QtWidgets.QLabel(
                "Needs the optional 'comet-smlm' package "
                f"({INSTALL_HINT}). The run will fail until it is installed."
            )
            note.setWordWrap(True)
            layout.addRow(note)

        mode_combo = QtWidgets.QComboBox()
        for key, label in _WINDOW_MODE_LABELS.items():
            mode_combo.addItem(label, key)
        mode_combo.setToolTip("What 'Window size' counts")
        layout.addRow("Window mode:", mode_combo)

        size_spin = QtWidgets.QSpinBox()
        size_spin.setRange(0, 1_000_000_000)
        size_spin.setSpecialValueText("not set")
        size_spin.setToolTip(_WINDOW_SIZE_UNSET)
        layout.addRow("Window size:", size_spin)

        drift_spin = QtWidgets.QDoubleSpinBox()
        drift_spin.setRange(1.0, 1_000_000.0)
        drift_spin.setDecimals(1)
        drift_spin.setValue(300.0)
        drift_spin.setSuffix(" nm")
        drift_spin.setToolTip(
            "Largest drift expected over the acquisition; also the "
            "neighbour-search radius (COMET default 300 nm)"
        )
        layout.addRow("Max drift:", drift_spin)

        backend_combo = QtWidgets.QComboBox()
        backend_combo.addItems(list(BACKENDS))
        backend_combo.setToolTip(
            "auto = the fastest COMET finds on this machine; a backend that is "
            "not available is refused at run time"
        )
        layout.addRow("Backend:", backend_combo)

        advanced = QtWidgets.QGroupBox("Advanced (COMET defaults)")
        advanced_layout = QtWidgets.QFormLayout(advanced)

        target_spin = QtWidgets.QDoubleSpinBox()
        target_spin.setRange(0.01, 100_000.0)
        target_spin.setDecimals(2)
        target_spin.setValue(1.0)
        target_spin.setSuffix(" nm")
        target_spin.setToolTip("Kernel width the refinement stops at (COMET default 1 nm)")
        advanced_layout.addRow("Target sigma:", target_spin)

        initial_spin = QtWidgets.QDoubleSpinBox()
        initial_spin.setRange(0.0, 1_000_000.0)
        initial_spin.setDecimals(1)
        initial_spin.setSuffix(" nm")
        initial_spin.setSpecialValueText("max drift / 3")
        initial_spin.setToolTip(
            "Kernel width the refinement starts from; 'max drift / 3' is COMET's rule"
        )
        advanced_layout.addRow("Initial sigma:", initial_spin)

        boxcar_spin = QtWidgets.QSpinBox()
        boxcar_spin.setRange(1, 10_000)
        boxcar_spin.setValue(1)
        boxcar_spin.setToolTip(
            "Temporal smoothing of the window drifts, in windows (1 = none)"
        )
        advanced_layout.addRow("Boxcar width:", boxcar_spin)

        interp_combo = QtWidgets.QComboBox()
        interp_combo.addItems(list(INTERPOLATIONS))
        advanced_layout.addRow("Interpolation:", interp_combo)

        cap_spin = QtWidgets.QSpinBox()
        cap_spin.setRange(0, 1_000_000_000)
        cap_spin.setSpecialValueText("all")
        cap_spin.setToolTip(
            "Random cap on localizations per window; COMET lowers it by itself "
            "if the pair search runs out of memory"
        )
        advanced_layout.addRow("Max localizations per window:", cap_spin)

        seed_spin = QtWidgets.QSpinBox()
        seed_spin.setRange(-1, 2_147_483_647)
        seed_spin.setValue(-1)
        seed_spin.setSpecialValueText("unseeded")
        seed_spin.setToolTip("Seed for the random cap, so a capped run repeats exactly")
        advanced_layout.addRow("Seed:", seed_spin)

        layout.addRow(advanced)

        status = QtWidgets.QLabel("")
        status.setWordWrap(True)
        layout.addRow(status)

        def get_values():
            size = int(size_spin.value())
            initial = float(initial_spin.value())
            cap = int(cap_spin.value())
            seed = int(seed_spin.value())
            return {
                "window_mode": str(mode_combo.currentData()),
                "window_size": size if size > 0 else None,
                "max_drift_nm": float(drift_spin.value()),
                "backend": str(backend_combo.currentText()),
                "target_sigma_nm": float(target_spin.value()),
                "initial_sigma_nm": initial if initial > 0 else None,
                "boxcar_width": int(boxcar_spin.value()),
                "interpolation": str(interp_combo.currentText()),
                "max_locs_per_window": cap if cap > 0 else None,
                "seed": seed if seed >= 0 else None,
            }

        def before_run():
            status.setText(
                "Running COMET… (the first CPU run also compiles its kernel)"
            )

        def after_run(results, failures):
            if failures:
                status.setText(f"Failed: {failures[0][1]}")
                return
            if not results:
                status.setText("")
                return
            status.setText(summarize_run(getattr(results[0], "metadata", {}) or {}))

        widget.get_values = get_values
        widget.before_run = before_run
        widget.after_run = after_run
        return widget

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        if not isinstance(result, LocalizationResult):
            raise TypeError("COMET drift correction requires a LocalizationResult input")

        window_mode = str(params.get("window_mode", "frames"))
        if window_mode not in WINDOW_MODES:
            raise ValueError(
                f"Unknown window mode {window_mode!r}; expected one of {list(WINDOW_MODES)}"
            )
        window_size = _optional_positive(params.get("window_size"), int)
        if window_size is None:
            raise ValueError(_WINDOW_SIZE_UNSET)
        max_drift_nm = float(params.get("max_drift_nm", 300.0))
        if max_drift_nm <= 0:
            raise ValueError("max_drift_nm must be positive")
        backend = str(params.get("backend", "auto"))
        if backend not in BACKENDS:
            raise ValueError(f"Unknown backend {backend!r}; expected one of {BACKENDS}")
        interpolation = str(params.get("interpolation", "cubic"))
        if interpolation not in INTERPOLATIONS:
            raise ValueError(
                f"Unknown interpolation {interpolation!r}; expected one of {INTERPOLATIONS}"
            )

        locs = result.locs
        if len(locs) == 0:
            raise ValueError("Cannot estimate drift from an empty localization table")

        comet = _require_comet()
        mode = None if backend == "auto" else backend
        if mode is not None:
            available = list(comet.available_backends())
            if mode.removesuffix("_qc") not in available:
                raise RuntimeError(
                    f"COMET backend {mode!r} is not available on this machine; "
                    f"available: {', '.join(available)}"
                )

        # COMET corrects its input array in place, so it gets a copy: the
        # input result must stay what it was.
        dataset = np.column_stack(
            [locs.x_nm, locs.y_nm, locs.z_nm, locs.frame]
        ).astype(np.float64)

        def progress(stage, info):
            # COMET reports after every cost-function evaluation, which is
            # where a long run spends its time, so Cancel lands within one.
            checkpoint()

        drift, corrected, details = comet.comet_run_kd(
            dataset,
            segmentation_mode=WINDOW_MODES[window_mode],
            segmentation_var=int(window_size),
            max_drift_nm=max_drift_nm,
            initial_sigma_nm=_optional_positive(params.get("initial_sigma_nm"), float),
            target_sigma_nm=float(params.get("target_sigma_nm", 1.0)),
            boxcar_width=int(params.get("boxcar_width", 1)),
            interpolation_method=interpolation,
            max_locs_per_segment=_optional_positive(params.get("max_locs_per_window"), int),
            mode=mode,
            return_corrected_locs=True,
            return_details=True,
            progress=progress,
            random_state=(None if params.get("seed") is None else int(params["seed"])),
        )

        table = locs.copy().view(np.recarray)
        table.x_nm = corrected[:, 0].astype(np.float32)
        table.y_nm = corrected[:, 1].astype(np.float32)
        table.z_nm = corrected[:, 2].astype(np.float32)

        # COMET returns one row per frame from 0 to the last frame; the trace
        # starts where the data does, like smlm-drift's. Unlike smlm-drift's,
        # it is referenced to the acquisition's centre, not its first window.
        frame_min, frame_max = details.frame_range
        rows = (drift[:, 3] >= frame_min) & (drift[:, 3] <= frame_max)
        frames = drift[rows, 3].astype(np.int64)
        drift_x = drift[rows, 0]
        drift_y = drift[rows, 1]
        drift_z = drift[rows, 2]
        is_3d = result.dims == "3D"
        extremes = [abs(drift_x).max(), abs(drift_y).max()]
        if is_3d:
            extremes.append(abs(drift_z).max())

        segmentation = getattr(details, "segmentation", None)
        metadata = {
            **result.metadata,
            "drift_method": "comet",
            "drift_params": dict(params),
            "drift_backend": str(details.backend),
            "drift_windows": int(getattr(segmentation, "n_segments", len(details.knot_frames))),
            "drift_pairs": int(details.n_pairs),
            "drift_sigma_accepted_nm": (
                None if details.sigma_accepted_nm is None else float(details.sigma_accepted_nm)
            ),
            "drift_auto_downsampled": bool(details.auto_downsampled),
            "drift_timings_s": {str(k): float(v) for k, v in details.timings_s.items()},
            "drift_max_nm": float(max(extremes)),
            "comet_version": str(comet.__version__),
        }
        return DriftCorrectedLocalizationResult(
            name=f"{result.name} (COMET drift-corrected)",
            locs=table,
            drift_frames=frames,
            drift_x_nm=drift_x,
            drift_y_nm=drift_y,
            drift_z_nm=drift_z if is_3d else None,
            pixel_size_nm=result.pixel_size_nm,
            z_step_nm=result.z_step_nm,
            dims=result.dims,
            source_name=result.source_name,
            source_shape=result.source_shape,
            metadata=metadata,
        )


def summarize_run(metadata: dict) -> str:
    """One line for the panel: what backend ran, over what, how long."""
    if metadata.get("drift_method") != "comet":
        return ""
    seconds = sum(metadata.get("drift_timings_s", {}).values())
    parts = [
        f"{metadata.get('drift_backend', '?')} backend",
        f"{metadata.get('drift_windows', '?')} windows",
        f"{metadata.get('drift_pairs', '?')} pairs",
        f"max drift {metadata.get('drift_max_nm', float('nan')):.1f} nm",
        f"{seconds:.1f} s",
    ]
    if metadata.get("drift_auto_downsampled"):
        parts.append("windows were capped to fit in memory")
    return ", ".join(parts)


__all__ = [
    "BACKENDS",
    "INSTALL_HINT",
    "INTERPOLATIONS",
    "WINDOW_MODES",
    "SmlmCometDriftProcessor",
    "comet_installed",
    "summarize_run",
]
