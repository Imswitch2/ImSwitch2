"""Drift correction through COMET (``comet-smlm`` on PyPI).

COMET (Cost-function Optimized Maximal Overlap Drift EsTimation,
https://github.com/gpufit/Comet) estimates one drift vector per time window
by maximising the overlap of every pair of localizations within
``max_drift_nm`` of each other across windows, refining the kernel width
from coarse to fine, and interpolating the window drifts to a per-frame
curve. It corrects x, y and z. It runs on numba-cuda, PyTorch or a
numba-compiled CPU kernel; COMET picks the fastest it finds unless told.

The package is optional and is looked for the moment the panel is built:
without it the panel says how to install it, keeps its controls and the
Run button off, and offers **Check again** for after a ``pip install`` in
the running session. The processor's ``check_inputs`` gate says the same, so
a workflow is refused before it starts; a headless ``apply`` without the
package fails with the same message.

The panel asks for four things: how the acquisition is cut into windows
(500 localizations per window by default), the largest drift expected, and
the backend. The kernel width the refinement stops at is fixed at
:data:`TARGET_SIGMA_NM`. Everything else -- the initial kernel width (a
third of the maximum drift), no boxcar smoothing, cubic interpolation -- is
COMET's own default (``comet_run_kd`` in ``comet-smlm`` 1.2.0) and is not
offered. A headless caller may additionally cap the localizations per window
and seed that cap (:attr:`SmlmCometDriftProcessor.extra_param_keys`).
"""

from __future__ import annotations

import importlib
from typing import Callable

import numpy as np
from qtpy import QtCore, QtWidgets

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

#: Kernel width (nm) the coarse-to-fine refinement stops at, for every run
#: from this processor. Fixed for now; COMET's own default is 1 nm.
TARGET_SIGMA_NM = 10.0

#: Window (segmentation) modes, as ``comet_run_kd(segmentation_mode=...)``
#: numbers them. The panel defaults to localizations per window.
WINDOW_MODES: dict[str, int] = {"localizations": 1, "frames": 2, "windows": 0}

_WINDOW_MODE_LABELS: dict[str, str] = {
    "localizations": "Localizations per window",
    "frames": "Frames per window",
    "windows": "Number of windows",
}

DEFAULT_WINDOW_MODE = "localizations"
DEFAULT_WINDOW_SIZE = 500

#: ``comet_run_kd(mode=...)`` options plus ``auto`` (COMET's own choice,
#: :func:`comet.best_backend`). The ``_qc`` variants flag and drop windows
#: whose optimisation looks flawed.
BACKENDS: tuple[str, ...] = ("auto", "cuda", "cuda_qc", "torch", "torch_qc", "cpu")


def comet_problem() -> str:
    """Why COMET cannot run here, or ``""`` when it can.

    The package is imported for real rather than merely looked up, so an
    installation that is present but broken (a numba that does not load,
    say) is reported as that and not as ready. Once imported it sits in
    ``sys.modules``, so asking again is free.
    """
    try:
        importlib.import_module(_PACKAGE)
    except ImportError:
        return (
            "COMET drift correction needs the optional 'comet-smlm' package "
            f"({INSTALL_HINT})."
        )
    except Exception as exc:  # noqa: BLE001 - a broken install is reported, not raised
        return (
            "The optional 'comet-smlm' package is installed but cannot be "
            f"imported ({exc!r}); reinstall it with {INSTALL_HINT}."
        )
    return ""


def recheck_comet() -> str:
    """:func:`comet_problem` after the import system forgets what it cached.

    A package installed while ImSwitch2 was running is otherwise invisible
    to it until a restart.
    """
    importlib.invalidate_caches()
    return comet_problem()


def comet_installed() -> bool:
    """Whether ``comet-smlm`` imports."""
    return not comet_problem()


def _require_comet():
    problem = comet_problem()
    if problem:
        raise RuntimeError(problem)
    return importlib.import_module(_PACKAGE)


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
    #: Taken from a workflow or a script, not offered in the panel: a random
    #: cap on the localizations per window, to bound memory and time (COMET
    #: lowers it by itself if the pair search runs out of memory), and a seed
    #: that makes a capped run repeat exactly.
    extra_param_keys = ("max_locs_per_window", "seed")

    @classmethod
    def default_params(cls) -> dict:
        return {
            "window_mode": DEFAULT_WINDOW_MODE,
            "window_size": DEFAULT_WINDOW_SIZE,
            "max_drift_nm": 300.0,
            "backend": "auto",
        }

    @classmethod
    def param_spec(cls) -> tuple:
        return (
            ParamField(
                "window_mode", "select", DEFAULT_WINDOW_MODE, label="Window mode",
                options=tuple(WINDOW_MODES),
                help="What 'Window size' counts (COMET segmentation_mode).",
            ),
            ParamField(
                "window_size", "int", DEFAULT_WINDOW_SIZE, label="Window size", min=1,
                max=None,
                help="Localizations per window, frames per window or the number of "
                     "windows, by 'Window mode'. Enough windows to resolve the drift, "
                     "enough localizations per window to constrain it.",
            ),
            ParamField(
                "max_drift_nm", "float", 300.0, label="Max drift", min=1, max=1_000_000,
                decimals=1, suffix="nm",
                help="Largest drift expected over the acquisition; also the "
                     "neighbour-search radius (COMET default 300 nm). The refinement "
                     "starts from a kernel a third as wide.",
            ),
            ParamField(
                "backend", "select", "auto", label="Backend", options=BACKENDS,
                help="auto = the fastest COMET finds (numba-cuda, torch with CUDA, "
                     "else the CPU kernel). _qc variants drop windows whose fit "
                     "looks flawed.",
            ),
        )

    @property
    def applies_to(self) -> Callable[[ProcessingResult], bool]:
        return lambda result: isinstance(result, LocalizationResult)

    def check_inputs(self, results) -> tuple[bool, str]:
        """The usual gate, and then whether COMET is here to run at all."""
        ok, reason = super().check_inputs(results)
        if not ok:
            return ok, reason
        problem = comet_problem()
        if problem:
            return False, problem
        return True, ""

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return CometParamWidget(parent)

    def apply(self, result: ProcessingResult, params: dict) -> ProcessingResult:
        if not isinstance(result, LocalizationResult):
            raise TypeError("COMET drift correction requires a LocalizationResult input")

        window_mode = str(params.get("window_mode", DEFAULT_WINDOW_MODE))
        if window_mode not in WINDOW_MODES:
            raise ValueError(
                f"Unknown window mode {window_mode!r}; expected one of {list(WINDOW_MODES)}"
            )
        window_size = _optional_positive(params.get("window_size", DEFAULT_WINDOW_SIZE), int)
        if window_size is None:
            raise ValueError(
                "window_size must be a positive count: localizations per window, "
                "frames per window or the number of windows, by window_mode"
            )
        max_drift_nm = float(params.get("max_drift_nm", 300.0))
        if max_drift_nm <= 0:
            raise ValueError("max_drift_nm must be positive")
        backend = str(params.get("backend", "auto"))
        if backend not in BACKENDS:
            raise ValueError(f"Unknown backend {backend!r}; expected one of {BACKENDS}")

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

        # What is not passed is COMET's own default: the initial kernel width
        # (max drift / 3), boxcar_width 1 (no smoothing), cubic interpolation.
        drift, corrected, details = comet.comet_run_kd(
            dataset,
            segmentation_mode=WINDOW_MODES[window_mode],
            segmentation_var=int(window_size),
            max_drift_nm=max_drift_nm,
            target_sigma_nm=TARGET_SIGMA_NM,
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
            "drift_target_sigma_nm": TARGET_SIGMA_NM,
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


class CometParamWidget(QtWidgets.QWidget):
    """The COMET panel: whether it can run at all, then its four settings.

    Built with the package looked for, so a missing or broken install is
    said on the spot, with the install line ready to copy, and the controls
    are off until **Check again** finds it.
    """

    sigRunStateChanged = QtCore.Signal()
    """Whether a run is possible changed; the panel re-decides its Run button."""

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.availabilityBox = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(self.availabilityBox)
        box.setContentsMargins(0, 0, 0, 0)
        self.problemLabel = QtWidgets.QLabel("")
        self.problemLabel.setWordWrap(True)
        self.problemLabel.setStyleSheet("font-weight: bold;")
        self.installEdit = QtWidgets.QLineEdit(INSTALL_HINT)
        self.installEdit.setReadOnly(True)
        self.installEdit.setToolTip(
            "Run this in a terminal with the ImSwitch2 environment active, "
            "then press Check again"
        )
        self.recheckButton = QtWidgets.QPushButton("Check again")
        self.recheckButton.setToolTip("Look for the package again without restarting")
        self.recheckButton.clicked.connect(self.recheck)
        box.addWidget(self.problemLabel)
        box.addWidget(self.installEdit)
        box.addWidget(self.recheckButton)
        layout.addWidget(self.availabilityBox)

        self.form = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(self.form)
        form.setContentsMargins(0, 0, 0, 0)

        self.modeCombo = QtWidgets.QComboBox()
        for key, label in _WINDOW_MODE_LABELS.items():
            self.modeCombo.addItem(label, key)
        self.modeCombo.setToolTip("What 'Window size' counts")
        form.addRow("Window mode:", self.modeCombo)

        self.sizeSpin = QtWidgets.QSpinBox()
        self.sizeSpin.setRange(1, 1_000_000_000)
        self.sizeSpin.setValue(DEFAULT_WINDOW_SIZE)
        self.sizeSpin.setToolTip(
            "Localizations per window, frames per window or the number of windows, "
            "by 'Window mode'. Enough windows to resolve the drift, enough "
            "localizations per window to constrain it."
        )
        form.addRow("Window size:", self.sizeSpin)

        self.driftSpin = QtWidgets.QDoubleSpinBox()
        self.driftSpin.setRange(1.0, 1_000_000.0)
        self.driftSpin.setDecimals(1)
        self.driftSpin.setValue(300.0)
        self.driftSpin.setSuffix(" nm")
        self.driftSpin.setToolTip(
            "Largest drift expected over the acquisition; also the "
            "neighbour-search radius (COMET default 300 nm). The refinement "
            "starts from a kernel a third as wide."
        )
        form.addRow("Max drift:", self.driftSpin)

        self.backendCombo = QtWidgets.QComboBox()
        self.backendCombo.addItems(list(BACKENDS))
        self.backendCombo.setToolTip(
            "auto = the fastest COMET finds on this machine; a backend that is "
            "not available is refused at run time"
        )
        form.addRow("Backend:", self.backendCombo)
        layout.addWidget(self.form)

        self.statusLabel = QtWidgets.QLabel("")
        self.statusLabel.setWordWrap(True)
        layout.addWidget(self.statusLabel)

        self._problem = ""
        self._showProblem(comet_problem())

    @property
    def problem(self) -> str:
        """Why COMET cannot run, as last checked, or ``""``."""
        return self._problem

    def _showProblem(self, problem: str) -> None:
        self._problem = problem
        self.problemLabel.setText(problem)
        self.availabilityBox.setVisible(bool(problem))
        self.form.setEnabled(not problem)

    def recheck(self) -> None:
        """Look for the package again; tell the panel if the answer changed."""
        before = self._problem
        self._showProblem(recheck_comet())
        if self._problem:
            self.statusLabel.setText("Checked again: still not importable.")
        elif before:
            self.statusLabel.setText("COMET found.")
        if bool(before) != bool(self._problem):
            self.sigRunStateChanged.emit()

    def get_values(self) -> dict:
        return {
            "window_mode": str(self.modeCombo.currentData()),
            "window_size": int(self.sizeSpin.value()),
            "max_drift_nm": float(self.driftSpin.value()),
            "backend": str(self.backendCombo.currentText()),
        }

    def before_run(self) -> None:
        self.statusLabel.setText(
            "Running COMET… (the first CPU run also compiles its kernel)"
        )

    def after_run(self, results, failures) -> None:
        if failures:
            self.statusLabel.setText(f"Failed: {failures[0][1]}")
            return
        if not results:
            self.statusLabel.setText("")
            return
        self.statusLabel.setText(summarize_run(getattr(results[0], "metadata", {}) or {}))


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
    "DEFAULT_WINDOW_MODE",
    "DEFAULT_WINDOW_SIZE",
    "INSTALL_HINT",
    "TARGET_SIGMA_NM",
    "WINDOW_MODES",
    "CometParamWidget",
    "SmlmCometDriftProcessor",
    "comet_installed",
    "comet_problem",
    "recheck_comet",
    "summarize_run",
]
