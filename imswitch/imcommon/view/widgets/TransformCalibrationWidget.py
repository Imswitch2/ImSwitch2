"""General UI for creating, inspecting and applying a spatial calibration.

The widget deliberately owns only transform-calibration concepts: two images,
a correspondence/localization strategy, a transform model, persistence and an
opaque apply target supplied by its host. Hardware acquisition and viewer
lifecycle stay outside :mod:`imcommon`; ImControl wires those through the
signals and registration seams exposed here.

Calibration *methods* share one flow -- choose a Moving and a Reference
source, a method, a model; fit; judge the result; save it; verify and
predict:

* the image-localizing strategies (Foci grid, Beads, and whatever a host
  registers, e.g. ImControl's uLens grid), run in one step or tuned per image
  in the Inspect / tune workspace;
* **Manual point pairs**: click a feature in one image, the same feature in
  the other (Pick points…); the fit is redone after every pair, with each
  pair's residual and leave-one-out residual
  (:mod:`imswitch.imcommon.algorithms.transforms.correspondence.manual`).

A source may come with a pixel -> world mapping (a point-scan recording:
pixel -> scan position in µm, :class:`CalibrationSource`): the Reference
points are then mapped into world coordinates before the fit, so the
transform goes from Moving pixels to the Reference's world coordinates (the
camera -> scan calibration of a camera-triggered scan). Hosts provide such
sources (``setRecordingReader``), warnings that need both
(``setPairWarningProvider``), a calibrations store to save under an id
(``setCalibrationStore``) and background execution
(``setBackgroundExecution``); without them the widget works on image files.

It can still be run standalone with file-loaded images::

    python -m imswitch.imcommon.view.widgets.TransformCalibrationWidget
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import numpy as np
from qtpy import QtCore, QtGui, QtWidgets

from imswitch.imcommon.algorithms.transforms import (
    AcquisitionContext,
    SpatialTransform,
    estimator_specs,
    fit_parameter_defaults,
    fit_parameter_specs,
    normalize_fit_parameters,
    ParameterSpec,
    ModelFit,
    fit_transform,
    load,
    save,
)
from imswitch.imcommon.algorithms.transforms import calibration_report
from imswitch.imcommon.algorithms.transforms.correspondence import (
    CorrespondenceResult,
    CorrespondenceStrategySpec,
    DegeneratePointsError,
    ImageLocalizationResult,
    LocalizationDiagnostics,
    PairFit,
    PointPairEditor,
    builtin_correspondence_specs,
    fit_point_pairs,
    manual_correspondence,
    manual_strategy_spec,
    minimum_pairs,
    snap_to_local_maximum,
)
from imswitch.imcommon.view import guitools

__all__ = ["CalibrationSource", "TransformCalibrationWidget", "load_image_file", "main"]

#: The method whose pairs are clicked rather than localized.
MANUAL = "manual"

_WARN_STYLE = "background-color: #fff4e5; color: #663c00; padding: 3px; border-radius: 3px;"
_OK_STYLE = "background-color: #e8f5e9; color: #1b5e20; padding: 4px; border-radius: 3px; font-weight: bold;"
_BAD_STYLE = "background-color: #fdecea; color: #611a15; padding: 4px; border-radius: 3px; font-weight: bold;"


@dataclass
class CalibrationSource:
    """One image to calibrate from, and what is known about it.

    ``image`` 2-D; ``name`` shown to the user; ``context`` the acquisition
    context recorded with a transform made from it; ``pixel_to_world`` a
    model mapping this image's ``(row, col)`` pixels to world coordinates
    (``world_axes``, in ``world_units``, frame ``world_frame``) when the
    image has them -- a point-scan image's scan positions -- else ``None``;
    ``lines`` ``(label, value)`` facts and ``warnings`` shown under the slot;
    ``detectors``/``detector`` the detectors of a multi-detector file;
    ``choices`` ``{name: ([(label, value), ...], current)}`` options a host
    re-reads the file with (e.g. the scan's fast axis); ``path`` the file;
    ``payload`` anything the host needs back (its own file summary)."""

    image: np.ndarray
    name: str = "in memory"
    context: AcquisitionContext = field(default_factory=AcquisitionContext)
    pixel_to_world: Any = None
    world_axes: tuple = ()
    world_units: str = "px"
    world_frame: str | None = None
    lines: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    detectors: list = field(default_factory=list)
    detector: str | None = None
    choices: dict = field(default_factory=dict)
    path: Path | None = None
    kind: str = "image"
    payload: Any = None

    @property
    def has_world(self) -> bool:
        return self.pixel_to_world is not None

    def world_pixel_sizes(self) -> tuple[float, float]:
        """World length of one pixel step along rows and along columns."""
        if self.pixel_to_world is None:
            return 1.0, 1.0
        matrix = np.asarray(self.pixel_to_world.as_matrix(), dtype=np.float64)
        return float(np.linalg.norm(matrix[:2, 0])), float(np.linalg.norm(matrix[:2, 1]))


class _JobRunner(QtCore.QObject):
    """Runs one callable off the GUI thread and reports back on it."""

    sigDone = QtCore.Signal(int, object, object)

    def submit(self, job_id: int, fn: Callable[[], Any]) -> None:
        threading.Thread(target=self._run, args=(job_id, fn), daemon=True).start()

    def _run(self, job_id: int, fn: Callable[[], Any]) -> None:
        try:
            result = fn()
        except Exception as error:  # noqa: BLE001 - every failure is shown
            self.sigDone.emit(job_id, None, error)
            return
        self.sigDone.emit(job_id, result, None)


# Kept as a public-ish compatibility constant for the Step-0 tests and any
# early callers that edited the foci parameters directly.
DEFAULT_PARAMETERS: dict[str, Any] = {
    "n_rows": 10,
    "n_cols": 10,
    "min_distance": 10,
    "threshold_rel": 0.2,
    "refine_radius": 5,
    "gaussian_sigma": 1.0,
    "max_relative_tilt_deg": 30.0,
}
DEFAULT_TRANSFORM_PARAMETERS: dict[str, Any] = {
    "residual_threshold": 2.0,
    "max_trials": 500,
}

IMAGE_FILTER = "Images (*.tif *.tiff *.npy *.png *.jpg *.jpeg *.h5 *.hdf5);;All files (*)"
TRANSFORM_FILTER = "Transforms (*.json *.h5 *.hdf5);;All files (*)"
RECORDING_FILTER = ("ImSwitch recordings and images (*.h5 *.hdf5 *.tif *.tiff *.npy *.png *.jpg *.jpeg);;"
                    "All files (*)")


def load_image_file(path: str | Path) -> np.ndarray:
    """Read one 2-D calibration plane from a likely recording format."""
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".npy":
        array = np.load(path)
    elif suffix in (".tif", ".tiff"):
        import tifffile

        array = tifffile.imread(str(path))
    elif suffix in (".h5", ".hdf5"):
        array = _first_h5_image(path)
    else:
        from PIL import Image

        with Image.open(path) as handle:
            array = np.asarray(handle.convert("F"))

    array = np.squeeze(np.asarray(array))
    if array.ndim < 2:
        raise ValueError(f"{path.name} is not an image (shape {array.shape})")
    while array.ndim > 2:
        array = array[array.shape[0] // 2]
    return np.ascontiguousarray(array)


def _first_h5_image(path: Path) -> np.ndarray:
    import h5py

    found: list[np.ndarray] = []

    def visit(_name: str, node: Any) -> Any:
        if found:
            return True
        if isinstance(node, h5py.Dataset) and node.ndim >= 2:
            found.append(np.asarray(node[()]))
            return True
        return None

    with h5py.File(str(path), "r") as handle:
        handle.visititems(visit)

    if not found:
        raise ValueError(f"{path.name} contains no dataset with 2 or more dimensions")
    return found[0]


class _ParameterEditorDialog(QtWidgets.QDialog):
    """Small typed editor driven by transform-backend ``ParameterSpec`` values."""

    def __init__(
        self,
        specs: dict[str, ParameterSpec],
        values: dict[str, Any],
        *,
        title: str,
        intro: str,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(620, 620)
        self.setMinimumSize(500, 420)
        self._specs = dict(specs)
        self._editors: dict[str, QtWidgets.QWidget] = {}

        root = QtWidgets.QVBoxLayout(self)
        intro_label = QtWidgets.QLabel(intro)
        intro_label.setWordWrap(True)
        root.addWidget(intro_label)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        content = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(content)
        content_layout.setContentsMargins(4, 4, 4, 4)
        content_layout.setSpacing(8)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        basic = [(name, spec) for name, spec in self._specs.items() if not spec.advanced]
        advanced = [(name, spec) for name, spec in self._specs.items() if spec.advanced]
        for name, spec in basic:
            content_layout.addWidget(self._parameterCard(name, spec, values))

        self._advancedButton = None
        self._advancedWidget = None
        if advanced:
            self._advancedButton = QtWidgets.QToolButton()
            self._advancedButton.setText("Advanced parameters")
            self._advancedButton.setCheckable(True)
            self._advancedButton.setChecked(False)
            self._advancedButton.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
            self._advancedButton.setArrowType(QtCore.Qt.RightArrow)
            content_layout.addWidget(self._advancedButton)

            self._advancedWidget = QtWidgets.QWidget()
            advanced_layout = QtWidgets.QVBoxLayout(self._advancedWidget)
            advanced_layout.setContentsMargins(12, 0, 0, 0)
            advanced_layout.setSpacing(8)
            for name, spec in advanced:
                advanced_layout.addWidget(self._parameterCard(name, spec, values))
            self._advancedWidget.setVisible(False)
            content_layout.addWidget(self._advancedWidget)
            self._advancedButton.toggled.connect(self._toggleAdvanced)

        content_layout.addStretch(1)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        defaults_button = buttons.addButton(
            "Restore defaults", QtWidgets.QDialogButtonBox.ResetRole
        )
        defaults_button.clicked.connect(self._restoreDefaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @classmethod
    def edit(
        cls,
        parent: QtWidgets.QWidget,
        specs: dict[str, ParameterSpec],
        values: dict[str, Any],
        *,
        title: str,
        intro: str,
    ) -> dict[str, Any] | None:
        dialog = cls(specs, values, title=title, intro=intro, parent=parent)
        result = dialog.exec_() if hasattr(dialog, "exec_") else dialog.exec()
        if result != QtWidgets.QDialog.Accepted:
            return None
        return dialog.values()

    def _parameterCard(
        self,
        name: str,
        spec: ParameterSpec,
        values: dict[str, Any],
    ) -> QtWidgets.QWidget:
        card = QtWidgets.QFrame()
        layout = QtWidgets.QVBoxLayout(card)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(3)

        top = QtWidgets.QHBoxLayout()
        label = QtWidgets.QLabel(spec.label)
        label.setToolTip(spec.description)
        top.addWidget(label)
        top.addStretch(1)
        editor = self._makeEditor(spec, values.get(name, spec.default))
        editor.setToolTip(spec.description)
        top.addWidget(editor)
        layout.addLayout(top)

        if spec.description:
            description = QtWidgets.QLabel(spec.description)
            description.setWordWrap(True)
            description.setEnabled(False)
            layout.addWidget(description)

        self._editors[name] = editor
        return card

    def _makeEditor(self, spec: ParameterSpec, value: Any) -> QtWidgets.QWidget:
        if spec.choices is not None:
            editor = QtWidgets.QComboBox()
            for choice in spec.choices:
                editor.addItem(str(choice), choice)
            index = editor.findData(value)
            if index >= 0:
                editor.setCurrentIndex(index)
            return editor
        if spec.value_type is bool:
            editor = QtWidgets.QCheckBox()
            editor.setChecked(bool(value))
            return editor
        if spec.value_type is int:
            editor = QtWidgets.QSpinBox()
            editor.setRange(
                int(spec.minimum) if spec.minimum is not None else -2_000_000_000,
                int(spec.maximum) if spec.maximum is not None else 2_000_000_000,
            )
            editor.setSingleStep(int(spec.step) if spec.step is not None else 1)
            editor.setValue(int(value))
            return editor
        if spec.value_type is float:
            editor = QtWidgets.QDoubleSpinBox()
            editor.setDecimals(spec.decimals if spec.decimals is not None else 6)
            editor.setRange(
                float(spec.minimum) if spec.minimum is not None else -1e12,
                float(spec.maximum) if spec.maximum is not None else 1e12,
            )
            editor.setSingleStep(float(spec.step) if spec.step is not None else 0.1)
            editor.setValue(float(value))
            return editor
        editor = QtWidgets.QLineEdit(str(value))
        return editor

    def _editorValue(self, editor: QtWidgets.QWidget, spec: ParameterSpec) -> Any:
        if isinstance(editor, QtWidgets.QComboBox):
            return editor.currentData()
        if isinstance(editor, QtWidgets.QCheckBox):
            return editor.isChecked()
        if isinstance(editor, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
            return editor.value()
        if isinstance(editor, QtWidgets.QLineEdit):
            return editor.text()
        raise TypeError(f"unsupported parameter editor {type(editor).__name__}")

    def values(self) -> dict[str, Any]:
        return {
            name: spec.normalize(self._editorValue(self._editors[name], spec))
            for name, spec in self._specs.items()
        }

    def _restoreDefaults(self) -> None:
        for name, spec in self._specs.items():
            editor = self._editors[name]
            value = spec.default
            if isinstance(editor, QtWidgets.QComboBox):
                index = editor.findData(value)
                if index >= 0:
                    editor.setCurrentIndex(index)
            elif isinstance(editor, QtWidgets.QCheckBox):
                editor.setChecked(bool(value))
            elif isinstance(editor, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                editor.setValue(value)
            elif isinstance(editor, QtWidgets.QLineEdit):
                editor.setText(str(value))

    def _toggleAdvanced(self, checked: bool) -> None:
        if self._advancedWidget is not None:
            self._advancedWidget.setVisible(bool(checked))
        if self._advancedButton is not None:
            self._advancedButton.setArrowType(
                QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow
            )


class TransformCalibrationWidget(QtWidgets.QWidget):
    """Fit a selectable transform model from selectable image correspondence."""

    sigCalibrated = QtCore.Signal(object)
    sigLocalizationChanged = QtCore.Signal(object)
    sigTransformChanged = QtCore.Signal(object)
    sigSnapSourceRequested = QtCore.Signal()
    sigSnapTargetRequested = QtCore.Signal()
    sigApplyToggled = QtCore.Signal(bool)
    sigApplyTargetChanged = QtCore.Signal(object)
    sigSetStartupRequested = QtCore.Signal()
    sigStateChanged = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Transform calibration")

        self._source_image: np.ndarray | None = None
        self._target_image: np.ndarray | None = None
        self._source_path: Path | None = None
        self._target_path: Path | None = None
        self._source_context = AcquisitionContext()
        self._target_context = AcquisitionContext()
        self._correspondence_result: CorrespondenceResult | None = None
        self._localization_results: dict[str, ImageLocalizationResult | None] = {
            "source": None, "target": None
        }
        self._localization_stale = {"source": False, "target": False}
        self._localization_errors: dict[str, str | None] = {"source": None, "target": None}
        self._localization_failure_diagnostics: dict[str, LocalizationDiagnostics | None] = {
            "source": None, "target": None
        }
        self._correspondence_error: str | None = None
        self._transform: SpatialTransform | None = None
        self._fit_result: ModelFit | None = None
        self._transform_path: Path | None = None
        self._source_points: np.ndarray | None = None
        self._target_points: np.ndarray | None = None
        self._workspace: QtWidgets.QWidget | None = None

        self._correspondence_specs: dict[str, CorrespondenceStrategySpec] = {}
        self._strategy_parameters: dict[str, dict[str, dict[str, Any]]] = {}
        self._strategy_pairing_parameters: dict[str, dict[str, Any]] = {}
        self._parameters: dict[str, Any] = {}  # compatibility alias: Moving params
        self._model_specs = {spec.name: spec for spec in estimator_specs()}
        self._model_parameters = {
            name: fit_parameter_defaults(name) for name in self._model_specs
        }
        self._fit_parameters: dict[str, Any] = {}

        # Sources, host seams, methods beyond localization, results.
        self._sources: dict[str, CalibrationSource | None] = {"source": None, "target": None}
        self._recording_reader: Callable[..., CalibrationSource] | None = None
        self._pair_warning_provider: Callable[..., list] | None = None
        self._store: Any = None
        self._default_folder: str | None = None
        self._pair_warnings: list[str] = []
        self._background = False
        self._runner = _JobRunner(self)
        self._runner.sigDone.connect(self._jobDone)
        self._job_id = 0
        self._job: int | None = None
        self._job_handlers: dict[int, Callable[[Any, Any], None]] = {}
        self._fit_after_localization = False
        self._pair_editor = PointPairEditor()
        self._pair_fit: PairFit | None = None
        self._manual_problem: str | None = None
        self._snap_enabled = True
        self._snap_radius = 3.0
        self._refusal: str | None = None
        self._fit_warnings: list[str] = []
        self._saved_paths: dict[str, Any] = {}
        self._qc_path: Path | None = None
        self._verification: dict | None = None
        self._picker: QtWidgets.QWidget | None = None
        self._id_edited = False

        self._buildUi()
        for spec in builtin_correspondence_specs():
            self.registerCorrespondenceStrategy(spec)
        self.registerCorrespondenceStrategy(manual_strategy_spec())
        self.setSelectedCorrespondence("foci_grid")
        self._modelChanged()
        self.setHostIntegrationEnabled(False)
        self._updateEnabledState()

    # -- construction ----------------------------------------------------

    def _buildUi(self) -> None:
        # -- sources --------------------------------------------------------
        self.loadSourceButton = guitools.BetterPushButton("Load moving image...")
        self.loadTargetButton = guitools.BetterPushButton("Load reference image...")
        self.snapSourceButton = guitools.BetterPushButton("Snap moving")
        self.snapTargetButton = guitools.BetterPushButton("Snap reference")
        self.sourceLabel = QtWidgets.QLabel("Moving: not loaded")
        self.targetLabel = QtWidgets.QLabel("Reference: not loaded")
        self._slots = {
            "target": self._buildSlot("target", self.loadTargetButton, self.snapTargetButton, self.targetLabel),
            "source": self._buildSlot("source", self.loadSourceButton, self.snapSourceButton, self.sourceLabel),
        }
        self.pairWarningsLabel = QtWidgets.QLabel("")
        self.pairWarningsLabel.setWordWrap(True)
        self.pairWarningsLabel.setStyleSheet(_WARN_STYLE)
        self.pairWarningsLabel.setVisible(False)

        # -- method -----------------------------------------------------------
        self.correspondenceCombo = guitools.BetterComboBox()
        self.correspondenceCombo.setToolTip("How corresponding points are found in the two images.")
        self.correspondenceHintLabel = QtWidgets.QLabel()
        self.correspondenceHintLabel.setWordWrap(True)
        self.correspondenceHintLabel.setVisible(False)
        self.localizationParametersButton = guitools.BetterPushButton("Inspect / tune…")
        self.editParametersButton = self.localizationParametersButton

        self.runLocalizationButton = guitools.BetterPushButton("Run localization")
        self.runLocalizationButton.setToolTip(
            "Localize Moving and Reference with the chosen method and pair them.")
        self.calibrateButton = self.runLocalizationButton
        self.runAllButton = guitools.BetterPushButton("Localize + fit")
        self.runAllButton.setToolTip("Localize, pair and fit the chosen model in one go.")
        self.sourceLocalizationStatusLabel = QtWidgets.QLabel("Moving      ○ Not localized")
        self.targetLocalizationStatusLabel = QtWidgets.QLabel("Reference   ○ Not localized")
        self.sourceLocalizationStatusLabel.setWordWrap(True)
        self.targetLocalizationStatusLabel.setWordWrap(True)
        # Compatibility-only aggregate label retained for existing callers/tests.
        self.localizationStatusLabel = QtWidgets.QLabel("Localization: not run")
        self.localizationStatusLabel.setVisible(False)

        self.pickPointsButton = guitools.BetterPushButton("Pick points…")
        self.pickPointsButton.setToolTip(
            "Open the point picker: click a feature in one image, then the same feature in the other.")
        self.manualStatusLabel = QtWidgets.QLabel("No pairs yet")
        self.manualStatusLabel.setWordWrap(True)

        # -- transform ------------------------------------------------------
        self.modelCombo = guitools.BetterComboBox()
        for spec in estimator_specs():
            self.modelCombo.addItem(spec.label, spec.name)
            if spec.name == "polynomial":
                requirement = (
                    "Needs at least 6 / 10 / 15 point pairs for degree 2 / 3 / 4."
                )
            else:
                requirement = f"Needs at least {spec.min_samples} point pair(s)."
            self.modelCombo.setItemData(
                self.modelCombo.count() - 1,
                f"{spec.description}\n\n{requirement}",
                QtCore.Qt.ToolTipRole,
            )
        self.modelCombo.setCurrentIndex(0)
        self.modelHintLabel = QtWidgets.QLabel()
        self.modelHintLabel.setVisible(False)
        self.transformParametersButton = guitools.BetterPushButton("Inspect / tune…")
        self.fitTransformButton = guitools.BetterPushButton("Fit transform")
        self.residualLimitSpin = QtWidgets.QDoubleSpinBox()
        self.residualLimitSpin.setRange(0.0, 1e6)
        self.residualLimitSpin.setDecimals(3)
        self.residualLimitSpin.setSingleStep(0.05)
        self.residualLimitSpin.setSpecialValueText("none")
        self.residualLimitSpin.setToolTip(
            "Refuse a fit whose largest residual (inliers) is above this, in the Reference's units. "
            "0 = no limit. Set to 0.25 µm when the Reference is a scan.")

        # Compatibility alias: the old inspector action now opens the shared workspace.
        self.visualizeButton = self.localizationParametersButton
        self.loadTransformButton = guitools.BetterPushButton("Load…")
        self.loadTransformButton.setToolTip("Load a saved transform record (JSON or HDF5).")
        self.saveTransformButton = guitools.BetterPushButton("Save as file…")
        self.saveTransformButton.setToolTip("Save the transform record to a file of your choice.")
        self.clearButton = guitools.BetterPushButton("Clear")

        self.statusLabel = QtWidgets.QLabel("○ No transform")
        self.statusLabel.setWordWrap(True)
        self.verdictLabel = QtWidgets.QLabel("")
        self.verdictLabel.setWordWrap(True)
        self.verdictLabel.setVisible(False)
        self.hostStatusLabel = QtWidgets.QLabel("")
        self.hostStatusLabel.setWordWrap(True)
        self.hostStatusLabel.setVisible(False)
        self.residualLabel = QtWidgets.QLabel("Residuals: n/a")
        self.residualLabel.setVisible(False)
        self.matrixView = QtWidgets.QPlainTextEdit()
        self.matrixView.setReadOnly(True)
        self.matrixView.setMinimumHeight(70)
        self.matrixView.setMaximumHeight(170)
        self.matrixView.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.matrixView.setPlaceholderText(
            "The fitted transform appears here: model, pairs, residuals, rotation, scale, "
            "mirror and the matrix, moving -> reference, in (row, col)."
        )

        # -- result actions --------------------------------------------------
        self.qcFigureButton = guitools.BetterPushButton("QC figure")
        self.qcFigureButton.setToolTip("Both images with the pairs and the residual vectors (PNG).")
        self.copyReportButton = guitools.BetterPushButton("Copy report")
        self.verifyButton = guitools.BetterPushButton("Verify…")
        self.verifyButton.setToolTip(
            "Check this transform on another pair of images of the same kind: no fitting, the localized "
            "Reference points are compared with the mapped Moving ones.")
        self.predictRowSpin = self._coordinateSpin()
        self.predictColSpin = self._coordinateSpin()
        self.predictButton = guitools.BetterPushButton("Predict")
        self.predictButton.setToolTip("Where a Moving pixel lands in the Reference's coordinates.")
        self.predictResultLabel = QtWidgets.QLabel("")
        self.predictResultLabel.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.predictResultLabel.setWordWrap(True)

        # -- save under an id (host store) --------------------------------------
        self.idEdit = QtWidgets.QLineEdit()
        self.idEdit.setPlaceholderText("calibration id")
        self.idEdit.setToolTip(
            "The id a smart pipeline's transform step names. Saved as transforms/<id>.json in the "
            "calibrations folder, with a pipeline entry <id>.json.")
        self.idEdit.textEdited.connect(lambda *_: setattr(self, "_id_edited", True))
        self.saveIdButton = guitools.BetterPushButton("Save")
        self.saveIdButton.setToolTip("Save under this id in the calibrations folder (asks before replacing).")
        self.openFolderButton = guitools.BetterPushButton("Open folder")
        self.storeControls = QtWidgets.QWidget()
        store_layout = QtWidgets.QHBoxLayout(self.storeControls)
        store_layout.setContentsMargins(0, 0, 0, 0)
        store_layout.addWidget(QtWidgets.QLabel("Id:"))
        store_layout.addWidget(self.idEdit, 1)
        store_layout.addWidget(self.saveIdButton)
        store_layout.addWidget(self.openFolderButton)
        self.storeControls.setVisible(False)

        # -- busy -------------------------------------------------------------
        self.busyBar = QtWidgets.QProgressBar()
        self.busyBar.setRange(0, 0)
        self.busyBar.setMaximumHeight(12)
        self.busyLabel = QtWidgets.QLabel("")
        self.cancelButton = guitools.BetterPushButton("Cancel")
        self.busyControls = QtWidgets.QWidget()
        busy_layout = QtWidgets.QHBoxLayout(self.busyControls)
        busy_layout.setContentsMargins(0, 0, 0, 0)
        busy_layout.addWidget(self.busyBar, 1)
        busy_layout.addWidget(self.busyLabel)
        busy_layout.addWidget(self.cancelButton)
        self.busyControls.setVisible(False)

        # -- layout -----------------------------------------------------------
        images_box = QtWidgets.QGroupBox("Images")
        images = QtWidgets.QVBoxLayout(images_box)
        images.setContentsMargins(6, 4, 6, 4)
        images.addWidget(self._slots["target"]["frame"])
        images.addWidget(self._slots["source"]["frame"])
        images.addWidget(self.pairWarningsLabel)

        method_box = QtWidgets.QGroupBox("Method")
        method = QtWidgets.QVBoxLayout(method_box)
        method.setContentsMargins(6, 4, 6, 4)
        method.addWidget(self.correspondenceCombo)
        self.methodStack = QtWidgets.QStackedWidget()
        localization_page = QtWidgets.QWidget()
        localization_layout = QtWidgets.QVBoxLayout(localization_page)
        localization_layout.setContentsMargins(0, 0, 0, 0)
        localization_actions = QtWidgets.QHBoxLayout()
        localization_actions.addWidget(self.runLocalizationButton)
        localization_actions.addWidget(self.runAllButton)
        localization_actions.addWidget(self.localizationParametersButton)
        localization_actions.addStretch(1)
        localization_layout.addLayout(localization_actions)
        localization_layout.addWidget(self.sourceLocalizationStatusLabel)
        localization_layout.addWidget(self.targetLocalizationStatusLabel)
        manual_page = QtWidgets.QWidget()
        manual_layout = QtWidgets.QVBoxLayout(manual_page)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_actions = QtWidgets.QHBoxLayout()
        manual_actions.addWidget(self.pickPointsButton)
        manual_actions.addStretch(1)
        manual_layout.addLayout(manual_actions)
        manual_layout.addWidget(self.manualStatusLabel)
        self.methodStack.addWidget(localization_page)
        self.methodStack.addWidget(manual_page)
        method.addWidget(self.methodStack)
        method.addWidget(self.busyControls)

        transform_box = QtWidgets.QGroupBox("Transform")
        transform = QtWidgets.QVBoxLayout(transform_box)
        transform.setContentsMargins(6, 4, 6, 4)
        model = QtWidgets.QHBoxLayout()
        model.addWidget(QtWidgets.QLabel("Model:"))
        model.addWidget(self.modelCombo, 1)
        transform.addLayout(model)
        transform_actions = QtWidgets.QHBoxLayout()
        transform_actions.addWidget(self.fitTransformButton)
        transform_actions.addWidget(self.transformParametersButton)
        transform_actions.addStretch(1)
        transform_actions.addWidget(QtWidgets.QLabel("Residual limit:"))
        transform_actions.addWidget(self.residualLimitSpin)
        transform.addLayout(transform_actions)
        transform.addWidget(self.statusLabel)
        transform.addWidget(self.verdictLabel)
        transform.addWidget(self.matrixView)
        result_actions = QtWidgets.QHBoxLayout()
        result_actions.addWidget(self.qcFigureButton)
        result_actions.addWidget(self.copyReportButton)
        result_actions.addWidget(self.verifyButton)
        result_actions.addStretch(1)
        transform.addLayout(result_actions)
        predict = QtWidgets.QHBoxLayout()
        predict.addWidget(QtWidgets.QLabel("Moving px row"))
        predict.addWidget(self.predictRowSpin)
        predict.addWidget(QtWidgets.QLabel("col"))
        predict.addWidget(self.predictColSpin)
        predict.addWidget(self.predictButton)
        predict.addStretch(1)
        transform.addLayout(predict)
        transform.addWidget(self.predictResultLabel)

        persistence = QtWidgets.QHBoxLayout()
        persistence.addWidget(self.loadTransformButton)
        persistence.addWidget(self.saveTransformButton)
        persistence.addWidget(self.clearButton)
        persistence.addStretch(1)

        self.applyControls = QtWidgets.QWidget()
        apply_layout = QtWidgets.QHBoxLayout(self.applyControls)
        apply_layout.setContentsMargins(0, 0, 0, 0)
        self.applyCheck = QtWidgets.QCheckBox("Apply")
        self.applyCapabilityLabel = QtWidgets.QLabel("")
        self.applyCapabilityLabel.setWordWrap(True)
        self.applyCapabilityLabel.setVisible(False)
        self.applyTargetCombo = guitools.BetterComboBox()
        self.applyTargetCombo.setMinimumWidth(170)
        self.setStartupButton = guitools.BetterPushButton("Set as startup")
        self.setStartupButton.setToolTip(
            "Save/use this named transform as the calibration loaded at startup."
        )
        apply_layout.addWidget(self.applyCheck)
        apply_layout.addWidget(QtWidgets.QLabel("Target:"))
        apply_layout.addWidget(self.applyTargetCombo, 1)
        apply_layout.addWidget(self.setStartupButton)

        save_box = QtWidgets.QGroupBox("Save / use")
        save = QtWidgets.QVBoxLayout(save_box)
        save.setContentsMargins(6, 4, 6, 4)
        save.addWidget(self.storeControls)
        save.addLayout(persistence)
        save.addWidget(self.applyControls)
        save.addWidget(self.applyCapabilityLabel)
        save.addWidget(self.hostStatusLabel)

        content = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(content)
        column.setContentsMargins(4, 4, 4, 4)
        column.setSpacing(6)
        column.addWidget(images_box)
        column.addWidget(method_box)
        column.addWidget(transform_box)
        column.addWidget(save_box)
        column.addStretch(1)
        self.scrollArea = QtWidgets.QScrollArea()
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scrollArea.setWidget(content)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scrollArea)

        self.loadSourceButton.clicked.connect(lambda: self._loadImage("source"))
        self.loadTargetButton.clicked.connect(lambda: self._loadImage("target"))
        self.snapSourceButton.clicked.connect(lambda *_: self.sigSnapSourceRequested.emit())
        self.snapTargetButton.clicked.connect(lambda *_: self.sigSnapTargetRequested.emit())
        self.correspondenceCombo.currentIndexChanged.connect(self._correspondenceChanged)
        self.modelCombo.currentIndexChanged.connect(self._modelChanged)
        self.localizationParametersButton.clicked.connect(
            lambda *_: self.openWorkspace("localization")
        )
        self.transformParametersButton.clicked.connect(
            lambda *_: self.openWorkspace("transform")
        )
        self.runLocalizationButton.clicked.connect(self.runLocalization)
        self.runAllButton.clicked.connect(self.calibrate)
        self.fitTransformButton.clicked.connect(self.fitTransform)
        self.pickPointsButton.clicked.connect(self.openPointPicker)
        self.residualLimitSpin.valueChanged.connect(lambda *_: self._invalidateTransform(emit=True))
        self.loadTransformButton.clicked.connect(self.loadTransform)
        self.saveTransformButton.clicked.connect(self.saveTransform)
        self.clearButton.clicked.connect(self.clear)
        self.qcFigureButton.clicked.connect(self.openQcFigure)
        self.copyReportButton.clicked.connect(
            lambda *_: self.copyText(self.matrixView.toPlainText()))
        self.verifyButton.clicked.connect(lambda *_: self.verifyWithFiles())
        self.predictButton.clicked.connect(
            lambda *_: self.predict(self.predictRowSpin.value(), self.predictColSpin.value()))
        self.saveIdButton.clicked.connect(lambda *_: self.saveToStore())
        self.openFolderButton.clicked.connect(lambda *_: self._openStoreFolder())
        self.cancelButton.clicked.connect(self.cancel)
        self.applyCheck.toggled.connect(self.sigApplyToggled)
        self.applyTargetCombo.currentIndexChanged.connect(
            lambda *_: self.sigApplyTargetChanged.emit(self.selectedApplyTarget())
        )
        self.setStartupButton.clicked.connect(lambda *_: self.sigSetStartupRequested.emit())

    def _buildSlot(self, role: str, load: QtWidgets.QPushButton, snap: QtWidgets.QPushButton,
                   label: QtWidgets.QLabel) -> dict:
        """One image slot: load/snap, name, thumbnail, file facts, warnings."""
        frame = QtWidgets.QFrame()
        frame.setFrameShape(QtWidgets.QFrame.StyledPanel)
        grid = QtWidgets.QGridLayout(frame)
        grid.setContentsMargins(4, 3, 4, 3)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(2)
        thumbnail = QtWidgets.QLabel()
        thumbnail.setFixedSize(64, 64)
        thumbnail.setAlignment(QtCore.Qt.AlignCenter)
        thumbnail.setStyleSheet("background-color: #202020; color: #888888; font-size: 8pt;")
        thumbnail.setText("—")
        label.setWordWrap(True)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(load)
        buttons.addWidget(snap)
        buttons.addStretch(1)
        detector = QtWidgets.QComboBox()
        detector.setToolTip("The detector of this file to use")
        detector.setVisible(False)
        detector.activated.connect(lambda *_: self._sourceChoiceChanged(role))
        buttons.addWidget(detector)
        choices = QtWidgets.QHBoxLayout()
        choices.setContentsMargins(0, 0, 0, 0)
        details = QtWidgets.QToolButton()
        details.setText("Details ▸")
        details.setCheckable(True)
        details.setAutoRaise(True)
        details.setVisible(False)
        info = QtWidgets.QLabel("")
        info.setTextFormat(QtCore.Qt.RichText)
        info.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        info.setWordWrap(True)
        info.setVisible(False)
        warnings = QtWidgets.QLabel("")
        warnings.setWordWrap(True)
        warnings.setStyleSheet(_WARN_STYLE)
        warnings.setVisible(False)
        coordinates = QtWidgets.QLabel("")
        coordinates.setWordWrap(True)
        coordinates.setEnabled(False)

        def toggle(checked: bool) -> None:
            info.setVisible(bool(checked) and bool(info.text()))
            details.setText("Details ▾" if checked else "Details ▸")

        details.toggled.connect(toggle)
        grid.addWidget(thumbnail, 0, 0, 3, 1, QtCore.Qt.AlignTop)
        grid.addLayout(buttons, 0, 1)
        grid.addWidget(label, 1, 1)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(coordinates, 1)
        row.addLayout(choices)
        row.addWidget(details)
        grid.addLayout(row, 2, 1)
        grid.addWidget(info, 3, 0, 1, 2)
        grid.addWidget(warnings, 4, 0, 1, 2)
        grid.setColumnStretch(1, 1)
        return {"frame": frame, "thumbnail": thumbnail, "detector": detector, "choices": choices,
                "choiceWidgets": {}, "details": details, "info": info, "warnings": warnings,
                "coordinates": coordinates}

    @staticmethod
    def _coordinateSpin() -> QtWidgets.QDoubleSpinBox:
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(-1e6, 1e6)
        spin.setDecimals(1)
        spin.setSingleStep(1.0)
        return spin

    # -- correspondence/model choices -----------------------------------

    def registerCorrespondenceStrategy(
        self,
        spec: CorrespondenceStrategySpec,
        *,
        select: bool = False,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        """Add or replace a correspondence strategy offered by the widget."""
        if not isinstance(spec, CorrespondenceStrategySpec):
            raise TypeError("spec must be a CorrespondenceStrategySpec")

        values = dict(spec.default_parameters)
        if parameters:
            values.update(parameters)
        values = spec.normalize(values)

        existing = self.correspondenceCombo.findData(spec.name)
        if existing >= 0:
            self.correspondenceCombo.setItemText(existing, spec.label)
            self.correspondenceCombo.setItemData(
                existing, spec.description, QtCore.Qt.ToolTipRole
            )
        else:
            self.correspondenceCombo.addItem(spec.label, spec.name)
            existing = self.correspondenceCombo.count() - 1
            self.correspondenceCombo.setItemData(
                existing, spec.description, QtCore.Qt.ToolTipRole
            )
        self._correspondence_specs[spec.name] = spec
        # Start shared so legacy direct mutation of ``_parameters`` still updates
        # both images.  As soon as one side is edited explicitly it gets its own dict.
        shared = dict(values)
        self._strategy_parameters[spec.name] = {
            "source": shared,
            "target": shared,
        }
        self._strategy_pairing_parameters[spec.name] = spec.normalize_pairing(None)
        if select:
            self.correspondenceCombo.setCurrentIndex(existing)
            self._correspondenceChanged()

    def selectedCorrespondence(self) -> str:
        return str(self.correspondenceCombo.currentData())

    def setSelectedCorrespondence(self, name: str) -> None:
        index = self.correspondenceCombo.findData(str(name))
        if index < 0:
            raise ValueError(
                f"unknown correspondence strategy {name!r}; choose one of "
                f"{list(self._correspondence_specs)}"
            )
        if self.correspondenceCombo.currentIndex() == index:
            self._correspondenceChanged()
            return
        self.correspondenceCombo.setCurrentIndex(index)

    def correspondenceSpec(self) -> CorrespondenceStrategySpec:
        return self._correspondence_specs[self.selectedCorrespondence()]

    def correspondenceSpecs(self) -> tuple[CorrespondenceStrategySpec, ...]:
        return tuple(self._correspondence_specs.values())

    def _correspondenceChanged(self) -> None:
        name = self.selectedCorrespondence()
        spec = self._correspondence_specs.get(name)
        if spec is None:
            return
        self._parameters = self._strategy_parameters[name]["source"]
        self.correspondenceHintLabel.setText(spec.description)
        self.correspondenceCombo.setToolTip(spec.description)
        self.localizationParametersButton.setEnabled(True)
        self.methodStack.setCurrentIndex(1 if name == MANUAL else 0)
        self._applyWorldPixelSizes()
        self._clearAllLocalization(
            aggregate=f"Localization: not run for {spec.label}", emit=True
        )
        if name == MANUAL:
            self._manualPairsChanged()
        self._emitStateChanged()

    def selectedModel(self) -> str:
        return str(self.modelCombo.currentData())

    def setSelectedModel(self, name: str) -> None:
        index = self.modelCombo.findData(str(name))
        if index < 0:
            raise ValueError(
                f"unknown model {name!r}; choose one of "
                f"{[spec.name for spec in estimator_specs()]}"
            )
        self.modelCombo.setCurrentIndex(index)

    def _modelChanged(self) -> None:
        name = self.selectedModel()
        spec = self._model_specs[name]
        self._fit_parameters = self._model_parameters[name]
        self.modelHintLabel.setText(spec.description)
        self.transformParametersButton.setEnabled(True)
        self._invalidateTransform(emit=True)
        self._emitStateChanged()

    # -- host-integration seams ------------------------------------------

    def setHostIntegrationEnabled(self, enabled: bool) -> None:
        """Show live-snap/apply controls when a host provides those services."""
        self._hostIntegrationEnabled = bool(enabled)
        self.snapSourceButton.setVisible(bool(enabled))
        self.snapTargetButton.setVisible(bool(enabled))
        self.applyControls.setVisible(bool(enabled))
        if not enabled:
            self.applyCapabilityLabel.setVisible(False)
        self._updateEnabledState()

    def setApplyTargets(
        self,
        targets: Iterable[tuple[str, Any]],
        *,
        preferred: Any = None,
    ) -> None:
        """Replace opaque host-provided apply targets while preserving selection."""
        current = preferred if preferred is not None else self.selectedApplyTarget()
        blocker = self.applyTargetCombo.blockSignals(True)
        try:
            self.applyTargetCombo.clear()
            selected_index = -1
            for label, target in targets:
                index = self.applyTargetCombo.count()
                self.applyTargetCombo.addItem(str(label), target)
                if current is not None and target == current:
                    selected_index = index
            if selected_index >= 0:
                self.applyTargetCombo.setCurrentIndex(selected_index)
            elif self.applyTargetCombo.count():
                self.applyTargetCombo.setCurrentIndex(0)
        finally:
            self.applyTargetCombo.blockSignals(blocker)
        self._updateEnabledState()

    def selectedApplyTarget(self) -> Any:
        if self.applyTargetCombo.currentIndex() < 0:
            return None
        return self.applyTargetCombo.currentData()

    def setApplyChecked(self, checked: bool) -> None:
        blocker = self.applyCheck.blockSignals(True)
        try:
            self.applyCheck.setChecked(bool(checked))
        finally:
            self.applyCheck.blockSignals(blocker)

    # -- state ------------------------------------------------------------

    @property
    def transform(self) -> SpatialTransform | None:
        return self._transform

    @property
    def fitResult(self) -> ModelFit | None:
        return self._fit_result

    @property
    def correspondenceResult(self) -> CorrespondenceResult | None:
        """Latest paired result, independent of any transform fit."""
        return self._correspondence_result

    @property
    def currentTransformPath(self) -> Path | None:
        return self._transform_path

    def image(self, role: str) -> np.ndarray | None:
        role = self._normalizeRole(role)
        return self._source_image if role == "source" else self._target_image

    def localizationResult(self, role: str) -> ImageLocalizationResult | None:
        return self._localization_results[self._normalizeRole(role)]

    def localizationIsStale(self, role: str) -> bool:
        return bool(self._localization_stale[self._normalizeRole(role)])

    def localizationError(self, role: str) -> str | None:
        return self._localization_errors[self._normalizeRole(role)]

    def localizationFailureDiagnostics(
        self, role: str
    ) -> LocalizationDiagnostics | None:
        return self._localization_failure_diagnostics[self._normalizeRole(role)]

    def localizationParameterSpecs(self) -> dict[str, ParameterSpec]:
        return dict(self.correspondenceSpec().parameter_specs)

    def pairingParameterSpecs(self) -> dict[str, ParameterSpec]:
        """Parameters that change cross-image pairing without rerunning localization."""
        return dict(self.correspondenceSpec().pairing_parameter_specs)

    def pairingParameters(self) -> dict[str, Any]:
        return dict(self._strategy_pairing_parameters[self.selectedCorrespondence()])

    def transformParameterSpecs(self) -> dict[str, ParameterSpec]:
        return dict(fit_parameter_specs(self.selectedModel()))

    def localizationParameters(self, role: str | None = None) -> dict[str, Any]:
        role = "source" if role is None else self._normalizeRole(role)
        return dict(self._strategy_parameters[self.selectedCorrespondence()][role])

    def transformParameters(self) -> dict[str, Any]:
        return dict(self._fit_parameters)

    def setLocalizationParameters(
        self, values: dict[str, Any], role: str | None = None
    ) -> None:
        """Update one image's localizer parameters without erasing its old overlay.

        ``role=None`` is the compatibility path and applies the values to both
        images. Existing results remain visible but are marked stale until rerun.
        """
        strategy = self.correspondenceSpec()
        normalized = strategy.normalize(values)
        roles = ("source", "target") if role is None else (self._normalizeRole(role),)
        changed = False
        store = self._strategy_parameters[self.selectedCorrespondence()]
        for current_role in roles:
            if normalized == store[current_role]:
                continue
            store[current_role] = dict(normalized)
            if current_role == "source":
                self._parameters = store[current_role]
            if self._localization_results[current_role] is not None:
                self._localization_stale[current_role] = True
            self._localization_errors[current_role] = None
            self._localization_failure_diagnostics[current_role] = None
            changed = True
        if not changed:
            return
        self._clearCorrespondence(emit=True)
        self._invalidateTransform(emit=True)
        self._setStatus("○ Waiting for current Moving and Reference localization")
        self._refreshLocalizationStatusLabels()
        self._updateEnabledState()
        self._emitStateChanged()

    def setPairingParameters(self, values: dict[str, Any]) -> None:
        """Change correspondence interpretation without rerunning either localizer."""
        strategy = self.correspondenceSpec()
        normalized = strategy.normalize_pairing(values)
        store = self._strategy_pairing_parameters[self.selectedCorrespondence()]
        if normalized == store:
            return
        self._strategy_pairing_parameters[self.selectedCorrespondence()] = normalized
        self._clearCorrespondence(emit=True)
        self._invalidateTransform(emit=True)
        # SLMcore correspondence is deliberately a post-localization operation.
        # Rebuild it immediately when both existing localizations are current so
        # manual Auto/flip/rotation choices are cheap and interactive.
        source = self._localization_results["source"]
        target = self._localization_results["target"]
        if (
            source is not None and target is not None
            and not self._localization_stale["source"]
            and not self._localization_stale["target"]
        ):
            self.buildCorrespondence()
        else:
            self._refreshLocalizationStatusLabels()
            self._updateEnabledState()
            self._emitStateChanged()

    def setTransformParameters(self, values: dict[str, Any]) -> None:
        """Validate fit parameters while preserving current localization."""
        model = self.selectedModel()
        normalized = normalize_fit_parameters(model, values)
        if normalized == self._fit_parameters:
            return
        self._fit_parameters = normalized
        self._model_parameters[model] = normalized
        self._invalidateTransform(emit=True)
        self._emitStateChanged()

    def _updateEnabledState(self) -> None:
        has_images = self._source_image is not None and self._target_image is not None
        has_correspondence = self._correspondence_result is not None
        has_transform = self._transform is not None
        busy = self._job is not None
        self.runLocalizationButton.setEnabled(has_images and not busy)
        self.runAllButton.setEnabled(has_images and not busy)
        self.pickPointsButton.setEnabled(has_images)
        self.fitTransformButton.setEnabled(has_correspondence and not busy)
        self.saveTransformButton.setEnabled(has_transform)
        self.saveIdButton.setEnabled(has_transform and not busy)
        self.openFolderButton.setEnabled(self._store is not None)
        self.qcFigureButton.setEnabled(
            has_transform and has_correspondence and self._fit_result is not None and has_images)
        self.copyReportButton.setEnabled(bool(self.matrixView.toPlainText()))
        self.verifyButton.setEnabled(has_transform and not busy)
        for widget in (self.predictButton, self.predictRowSpin, self.predictColSpin):
            widget.setEnabled(has_transform)
        for slot in self._slots.values():
            slot["detector"].setEnabled(not busy)
        # The diagnostic workspace is useful before success too.
        self.localizationParametersButton.setEnabled(True)
        self.transformParametersButton.setEnabled(True)
        has_target = self.applyTargetCombo.count() > 0
        # Live display correction is deliberately resample-free today: ImControl
        # writes a homogeneous matrix to Napari ``layer.affine``.  Nonlinear
        # models (currently Polynomial) are valid fitted/saved transforms but
        # need a separate live-frame resampling implementation before Apply can
        # be enabled.  Never approximate them with an affine matrix here.
        supports_live_apply = bool(
            has_transform and self._transform is not None
            and self._transform.as_matrix() is not None
        )
        live_apply_reason = (
            "Polynomial transforms can be fitted, saved, loaded and inspected, "
            "but live Apply is not available yet. The current live display path "
            "uses Napari's affine transform without resampling; nonlinear Apply "
            "will require an explicit live-frame resampling path."
        )
        enabled = has_transform and has_target and supports_live_apply
        self.applyCheck.setEnabled(enabled)
        self.applyTargetCombo.setEnabled(enabled)
        self.setStartupButton.setEnabled(has_transform and supports_live_apply)
        if (
            has_transform
            and not supports_live_apply
            and bool(getattr(self, "_hostIntegrationEnabled", False))
        ):
            self.applyCheck.setToolTip(live_apply_reason)
            self.applyTargetCombo.setToolTip(live_apply_reason)
            self.setStartupButton.setToolTip(live_apply_reason)
            self.applyCapabilityLabel.setText(live_apply_reason)
            self.applyCapabilityLabel.setVisible(True)
        else:
            self.applyCheck.setToolTip(
                "Apply this affine calibration to the selected display target."
            )
            self.applyTargetCombo.setToolTip("")
            self.setStartupButton.setToolTip(
                "Save/use this named transform as the calibration loaded at startup."
            )
            self.applyCapabilityLabel.clear()
            self.applyCapabilityLabel.setVisible(False)

    @staticmethod
    def _normalizeRole(role: str) -> str:
        value = str(role).lower().strip()
        aliases = {"moving": "source", "reference": "target"}
        value = aliases.get(value, value)
        if value not in ("source", "target"):
            raise ValueError("role must be source/moving or target/reference")
        return value

    def _roleLabel(self, role: str) -> str:
        return "Moving" if self._normalizeRole(role) == "source" else "Reference"

    def _emitStateChanged(self) -> None:
        self.sigStateChanged.emit()

    def _refreshLocalizationStatusLabels(self) -> None:
        for role, label in (
            ("source", self.sourceLocalizationStatusLabel),
            ("target", self.targetLocalizationStatusLabel),
        ):
            result = self._localization_results[role]
            error = self._localization_errors[role]
            if self.image(role) is None:
                text = "○ Not acquired"
            elif error:
                text = f"✕ {error}"
            elif result is None:
                text = "○ Not localized"
            elif self._localization_stale[role]:
                text = f"△ {result.summary()} — parameters changed"
            else:
                text = f"✓ {result.summary()}"
            label.setText(f"{self._roleLabel(role):<11} {text}")

        if self._correspondence_result is not None:
            self.localizationStatusLabel.setText(
                f"Localization: {self._correspondence_result.summary()}"
            )
        elif self._correspondence_error:
            self.localizationStatusLabel.setText(
                f"Localization correspondence failed: {self._correspondence_error}"
            )
        else:
            self.localizationStatusLabel.setText("Localization: not run")

    def _clearLocalizationRole(self, role: str) -> None:
        role = self._normalizeRole(role)
        self._localization_results[role] = None
        self._localization_stale[role] = False
        self._localization_errors[role] = None
        self._localization_failure_diagnostics[role] = None
        self._clearCorrespondence(emit=True)
        self._invalidateTransform(emit=True)
        self._setStatus("○ Waiting for current Moving and Reference localization")
        self._refreshLocalizationStatusLabels()
        self._updateEnabledState()
        self._emitStateChanged()

    def _clearAllLocalization(self, *, aggregate: str = "Localization: not run", emit: bool) -> None:
        had = self._correspondence_result is not None
        self._localization_results = {"source": None, "target": None}
        self._localization_stale = {"source": False, "target": False}
        self._localization_errors = {"source": None, "target": None}
        self._localization_failure_diagnostics = {"source": None, "target": None}
        self._correspondence_result = None
        self._correspondence_error = None
        self._source_points = None
        self._target_points = None
        self.localizationStatusLabel.setText(aggregate)
        self._refreshLocalizationStatusLabels()
        self._invalidateTransform(emit=emit)
        self._setStatus("○ Waiting for current Moving and Reference localization")
        self._updateEnabledState()
        if emit and had:
            self.sigLocalizationChanged.emit(None)

    def _clearCorrespondence(self, *, emit: bool) -> None:
        had = self._correspondence_result is not None
        self._correspondence_result = None
        self._correspondence_error = None
        self._source_points = None
        self._target_points = None
        if emit and had:
            self.sigLocalizationChanged.emit(None)

    def setSourceImage(
        self,
        image: Any,
        name: str | None = None,
        *,
        context: AcquisitionContext | None = None,
    ) -> None:
        self._setImage("source", image, name, context=context)

    def setTargetImage(
        self,
        image: Any,
        name: str | None = None,
        *,
        context: AcquisitionContext | None = None,
    ) -> None:
        self._setImage("target", image, name, context=context)

    def _setImage(self, role: str, image: Any, name: str | None, *,
                  context: AcquisitionContext | None) -> None:
        label = "moving image" if role == "source" else "reference image"
        array = _as_calibration_image(image, label)
        path = None if name is None or context is not None else Path(name)
        source = CalibrationSource(
            image=array, name=name or "in memory", context=_context_for_image(context, array, path),
            path=path, detector=(context.detector if context is not None else None),
            kind="live" if context is not None else "image")
        self.setSource(role, source)

    def setSource(self, role: str, source: CalibrationSource) -> None:
        """Install one calibration source (an image with what is known about
        it) as Moving (``source``) or Reference (``target``)."""
        role = self._normalizeRole(role)
        image = _as_calibration_image(source.image, f"{self._roleLabel(role).lower()} image")
        source.image = image
        context = _context_for_image(source.context, image, source.path)
        source.context = context
        self._sources[role] = source
        if role == "source":
            self._source_image, self._source_path, self._source_context = image, source.path, context
            self.sourceLabel.setText(f"Moving: {source.name}  {image.shape}")
        else:
            self._target_image, self._target_path, self._target_context = image, source.path, context
            self.targetLabel.setText(f"Reference: {source.name}  {image.shape}")
        self._showSourceDetails(role)
        self._applyWorldPixelSizes()
        self._suggestId()
        self._refreshPairWarnings()
        if self._pair_editor.pairs:
            # Clicks belong to the images they were made on.
            self._pair_editor.clear()
        self._clearLocalizationRole(role)
        if self.selectedCorrespondence() == MANUAL:
            self._manualPairsChanged()

    def source(self, role: str) -> CalibrationSource | None:
        """The calibration source installed as Moving or Reference."""
        return self._sources[self._normalizeRole(role)]

    def _showSourceDetails(self, role: str) -> None:
        source = self._sources[role]
        slot = self._slots[role]
        if source is None:
            return
        slot["thumbnail"].setPixmap(_thumbnail(source.image, slot["thumbnail"].size()))
        combo = slot["detector"]
        combo.blockSignals(True)
        combo.clear()
        combo.addItems([str(d) for d in source.detectors])
        if source.detector in source.detectors:
            combo.setCurrentText(str(source.detector))
        combo.blockSignals(False)
        combo.setVisible(len(source.detectors) > 1)
        layout: QtWidgets.QHBoxLayout = slot["choices"]
        for widget in slot["choiceWidgets"].values():
            layout.removeWidget(widget)
            widget.deleteLater()
        slot["choiceWidgets"] = {}
        for name, (options, current) in dict(source.choices).items():
            choice = QtWidgets.QComboBox()
            choice.setToolTip(str(name).replace("_", " ").capitalize())
            for text, value in options:
                choice.addItem(str(text), value)
            index = next((k for k, (_t, value) in enumerate(options) if value == current), 0)
            choice.setCurrentIndex(index)
            choice.activated.connect(lambda *_, r=role: self._sourceChoiceChanged(r))
            layout.addWidget(choice)
            slot["choiceWidgets"][name] = choice
        if source.has_world:
            axes = ", ".join(str(a) for a in source.world_axes) or "world"
            slot["coordinates"].setText(f"Coordinates: {axes} in {source.world_units} ({source.kind})")
        else:
            slot["coordinates"].setText(f"Coordinates: pixels ({source.kind})")
        lines = list(source.lines)
        slot["info"].setText(_facts_table(lines) if lines else "")
        slot["details"].setVisible(bool(lines))
        slot["info"].setVisible(bool(lines) and slot["details"].isChecked())
        warnings = list(source.warnings)
        slot["warnings"].setText("\n".join(f"⚠ {w}" for w in warnings))
        slot["warnings"].setVisible(bool(warnings))

    def _sourceChoiceChanged(self, role: str) -> None:
        """Re-read a host-provided file with another detector or option."""
        source = self._sources[role]
        if source is None or source.path is None or self._recording_reader is None:
            return
        slot = self._slots[role]
        detector = slot["detector"].currentText() or source.detector
        choices = {name: widget.currentData() for name, widget in slot["choiceWidgets"].items()}
        self.loadRecording(role, source.path, detector=detector, **choices)

    def _applyWorldPixelSizes(self) -> None:
        """A strategy that pairs in physical geometry (pixel sizes) is told the
        Reference's world pixel size, so a scan with different steps per axis
        is paired in its true geometry."""
        name = self.selectedCorrespondence()
        spec = self._correspondence_specs.get(name)
        if spec is None or "pixel_size_row" not in spec.parameter_specs:
            return
        store = self._strategy_parameters.get(name)
        if store is None:
            return
        for role in ("source", "target"):
            source = self._sources[role]
            rows, cols = source.world_pixel_sizes() if source is not None else (1.0, 1.0)
            current = store[role]
            if (current.get("pixel_size_row"), current.get("pixel_size_col")) == (rows, cols):
                continue
            values = dict(current)
            values.update(pixel_size_row=rows, pixel_size_col=cols)
            store[role] = spec.normalize(values)
            if role == "source":
                self._parameters = store[role]

    def _suggestId(self) -> None:
        if self._id_edited:
            return
        target = self._sources["target"]
        source = self._sources["source"]
        if target is not None and target.has_world:
            self.idEdit.setText("cam_to_scan")
        elif source is not None and target is not None and source.detector and target.detector:
            self.idEdit.setText(f"{source.detector}_to_{target.detector}")
        elif not self.idEdit.text():
            self.idEdit.setText("image_registration")

    def _refreshPairWarnings(self) -> None:
        warnings: list[str] = []
        if self._pair_warning_provider is not None:
            try:
                warnings = list(self._pair_warning_provider(self._sources["source"], self._sources["target"]))
            except Exception as error:  # noqa: BLE001 - a warning check never blocks
                warnings = [f"could not compare the two sources: {error}"]
        self._pair_warnings = warnings
        self.pairWarningsLabel.setText("\n".join(f"⚠ {w}" for w in warnings))
        self.pairWarningsLabel.setVisible(bool(warnings))

    def pairWarnings(self) -> list[str]:
        return list(self._pair_warnings)

    # -- actions ----------------------------------------------------------

    def _loadImage(self, role: str, kind: str = "file") -> None:
        title = f"Load {'moving' if role == 'source' else 'reference'} image"
        if kind == "zarr":
            path = self.askOpenFolder(f"{title} (Zarr store)")
        else:
            path = guitools.askForFilePath(
                self, title, defaultFolder=self._default_folder,
                nameFilter=RECORDING_FILTER if self._recording_reader is not None else IMAGE_FILTER,
            )
        if not path:
            return
        self._default_folder = str(Path(path).parent)
        self.loadPath(role, path)

    def loadPath(self, role: str, path: str | Path) -> bool:
        """Load ``path`` as Moving or Reference: an ImSwitch recording through
        the host's reader when there is one and it can read it, else as a
        plain image file."""
        role = self._normalizeRole(role)
        path = Path(path)
        if self._recording_reader is not None and (path.suffix.lower() in (".h5", ".hdf5", ".zarr")
                                                   or path.is_dir()):
            return self.loadRecording(role, path)
        try:
            image = load_image_file(path)
        except Exception as error:
            self._warn("Could not load image", f"{path.name}: {error}")
            return False
        setter = self.setSourceImage if role == "source" else self.setTargetImage
        setter(image, path.name)
        return True

    def loadRecording(self, role: str, path: str | Path, detector: str | None = None, **choices: Any) -> bool:
        """Read a recording through the host's reader (detector, file facts,
        warnings, world coordinates) and install it."""
        role = self._normalizeRole(role)
        if self._recording_reader is None:
            return False
        try:
            source = self._recording_reader(Path(path), role, detector, **choices)
        except Exception as error:  # noqa: BLE001 - shown in the slot
            slot = self._slots[role]
            slot["warnings"].setText(f"⚠ {Path(path).name} cannot be read: {error}")
            slot["warnings"].setVisible(True)
            self._emitStateChanged()
            return False
        self.setSource(role, source)
        return True

    def editParameters(self) -> None:
        """Compatibility alias for the localization inspector."""
        self.openWorkspace("localization")

    def editLocalizationParameters(self) -> None:
        self.openWorkspace("localization")

    def editTransformParameters(self) -> None:
        self.openWorkspace("transform")

    def runLocalizationForRole(self, role: str) -> bool:
        """Localize only Moving or Reference using that image's own parameters."""
        role = self._normalizeRole(role)
        image = self.image(role)
        if image is None:
            self._localization_errors[role] = "image not acquired"
            self._refreshLocalizationStatusLabels()
            self._emitStateChanged()
            return False
        strategy = self.correspondenceSpec()
        if not strategy.supports_independent_localization:
            self._localization_errors[role] = (
                f"{strategy.label} does not support independent localization"
            )
            self._refreshLocalizationStatusLabels()
            self._emitStateChanged()
            return False
        params = self.localizationParameters(role)
        try:
            normalized = strategy.normalize(params)
            result = strategy.localize(image, normalized)
        except Exception as error:
            self._localization_errors[role] = str(error)
            self._localization_failure_diagnostics[role] = getattr(
                error, "diagnostics", None
            )
            # Keep a previous result visible for diagnosis after a failed rerun,
            # but make it explicitly stale and unusable for correspondence.
            if self._localization_results[role] is not None:
                self._localization_stale[role] = True
            else:
                self._localization_stale[role] = False
            self._clearCorrespondence(emit=True)
            self._invalidateTransform(emit=True)
            self._setStatus("○ Waiting for valid Moving and Reference localization")
            self._refreshLocalizationStatusLabels()
            self._updateEnabledState()
            self._emitStateChanged()
            return False

        self._strategy_parameters[self.selectedCorrespondence()][role] = normalized
        if role == "source":
            self._parameters = self._strategy_parameters[self.selectedCorrespondence()][role]
        self._localization_results[role] = result
        self._localization_stale[role] = False
        self._localization_errors[role] = None
        self._localization_failure_diagnostics[role] = None
        self._clearCorrespondence(emit=True)
        self._invalidateTransform(emit=True)
        self._setStatus("○ Waiting for current Moving and Reference localization")
        self._refreshLocalizationStatusLabels()
        self._updateEnabledState()
        self._emitStateChanged()
        return True

    def recoverMissingForRole(self, role: str) -> bool:
        """Run a strategy-specific targeted search for currently missing sites."""
        role = self._normalizeRole(role)
        strategy = self.correspondenceSpec()
        image = self.image(role)
        current = self._localization_results[role]
        if (
            image is None or current is None
            or not strategy.supports_missing_recovery
            or self._localization_stale[role]
        ):
            return False
        try:
            recovered = strategy.recover(
                image, current, self.localizationParameters(role)
            )
        except Exception as error:
            self._localization_errors[role] = str(error)
            self._localization_failure_diagnostics[role] = getattr(
                error, "diagnostics", None
            )
            self._refreshLocalizationStatusLabels()
            self._emitStateChanged()
            return False

        self._localization_results[role] = recovered
        self._localization_errors[role] = None
        self._localization_failure_diagnostics[role] = None
        self._localization_stale[role] = False
        self._clearCorrespondence(emit=True)
        self._invalidateTransform(emit=True)
        self._refreshLocalizationStatusLabels()
        self._updateEnabledState()
        self._emitStateChanged()
        # Rebuild immediately when the other localization is still current.
        other = "target" if role == "source" else "source"
        if (
            self._localization_results[other] is not None
            and not self._localization_stale[other]
        ):
            self.buildCorrespondence()
        return True

    def buildCorrespondence(self) -> bool:
        """Pair the two already-localized images without rerunning either one."""
        strategy = self.correspondenceSpec()
        source = self._localization_results["source"]
        target = self._localization_results["target"]
        if (
            source is None or target is None
            or self._localization_stale["source"]
            or self._localization_stale["target"]
        ):
            self._clearCorrespondence(emit=True)
            self._refreshLocalizationStatusLabels()
            self._updateEnabledState()
            return False
        try:
            result = strategy.correspond(
                source,
                target,
                self.localizationParameters("source"),
                self.localizationParameters("target"),
                self.pairingParameters(),
            )
        except Exception as error:
            self._clearCorrespondence(emit=True)
            self._correspondence_error = str(error)
            self._setStatus(f"○ Waiting for valid correspondence — {error}")
            self._refreshLocalizationStatusLabels()
            self._updateEnabledState()
            self._emitStateChanged()
            return False

        self._correspondence_result = result
        self._correspondence_error = None
        self._source_points = result.source_points
        self._target_points = result.target_points
        self.localizationStatusLabel.setText(f"Localization: {result.summary()}")
        self._invalidateTransform(emit=True)
        self._refreshLocalizationStatusLabels()
        self._updateEnabledState()
        self.sigLocalizationChanged.emit(result)
        self._emitStateChanged()
        return True

    def runLocalization(self) -> bool:
        """Fast path: localize Moving and Reference, then pair them.

        With background execution on (a host's choice) the work runs off the
        GUI thread and this returns once it has started; the result arrives
        through the usual state signals."""
        if self._source_image is None or self._target_image is None:
            return False
        if self.selectedCorrespondence() == MANUAL:
            return self._manualPairsChanged(fit=False)
        strategy = self.correspondenceSpec()
        if self._background and strategy.supports_independent_localization:
            return self._startBackgroundLocalization(fit=False)
        if strategy.supports_independent_localization:
            source_ok = self.runLocalizationForRole("source")
            target_ok = self.runLocalizationForRole("target")
            if not (source_ok and target_ok):
                self._setStatus("○ Waiting for valid Moving and Reference localization")
                self._emitStateChanged()
                return False
            return self.buildCorrespondence()

        # Backward compatibility for external strategies that still expose only
        # the original two-image handler.
        try:
            result = strategy.run(
                self._source_image,
                self._target_image,
                self.localizationParameters("source"),
                self.localizationParameters("target"),
                self.pairingParameters(),
            )
        except Exception as error:
            self._clearCorrespondence(emit=True)
            self._localization_errors["source"] = str(error)
            self._localization_errors["target"] = str(error)
            diagnostics = getattr(error, "diagnostics", None)
            self._localization_failure_diagnostics = {
                "source": diagnostics, "target": diagnostics
            }
            self._setStatus(f"Localization failed: {error}")
            self._refreshLocalizationStatusLabels()
            self._invalidateTransform(emit=True)
            self._updateEnabledState()
            self.sigLocalizationChanged.emit(None)
            self._emitStateChanged()
            return False

        source_loc = result.source_localization or ImageLocalizationResult(
            result.source_points, metadata={"summary": "localized/matched"}
        )
        target_loc = result.target_localization or ImageLocalizationResult(
            result.target_points, metadata={"summary": "localized/matched"}
        )
        self._localization_results = {"source": source_loc, "target": target_loc}
        self._localization_stale = {"source": False, "target": False}
        self._localization_errors = {"source": None, "target": None}
        self._localization_failure_diagnostics = {"source": None, "target": None}
        self._correspondence_result = result
        self._source_points = result.source_points
        self._target_points = result.target_points
        self.localizationStatusLabel.setText(f"Localization: {result.summary()}")
        self._invalidateTransform(emit=True)
        self._refreshLocalizationStatusLabels()
        self._updateEnabledState()
        self.sigLocalizationChanged.emit(result)
        self._emitStateChanged()
        return True

    def fitTransform(self) -> bool:
        if self._correspondence_result is None or self._source_image is None:
            return False

        model = self.selectedModel()
        result = self._correspondence_result
        reference = self._sources["target"]
        world = reference if reference is not None and reference.has_world else None
        target_points = result.target_points
        units = "px"
        if world is not None:
            target_points = world.pixel_to_world.map_points(target_points)
            units = str(world.world_units)
        manual = self.selectedCorrespondence() == MANUAL
        try:
            fit_params = normalize_fit_parameters(model, self._fit_parameters)
            if manual:
                pair_fit = fit_point_pairs(
                    result.source_points, target_points, model,
                    degree=int(fit_params.get("degree", 2)),
                    input_shape=tuple(self._source_image.shape), unit=units)
                fit = pair_fit.fit
            else:
                pair_fit = None
                call = dict(fit_params)
                if world is not None and "residual_threshold" in call:
                    # The threshold is given in Reference pixels; the fit is in world units.
                    call["residual_threshold"] = float(call["residual_threshold"]) * float(
                        np.mean(world.world_pixel_sizes()))
                fit = fit_transform(
                    model,
                    result.source_points,
                    target_points,
                    input_shape=tuple(self._source_image.shape),
                    **call,
                )
                if units != "px":
                    fit.residuals = replace(fit.residuals, unit=units)
        except Exception as error:
            self._invalidateTransform(emit=True)
            if manual:
                self._pair_fit = None
            self._setStatus(f"✕ Transform fit failed — {error}")
            self._updateEnabledState()
            self._emitStateChanged()
            return False

        self._fit_parameters = fit_params
        self._model_parameters[model] = fit_params
        correspondence_metadata = dict(result.metadata)
        provenance = {
            "strategy": self.selectedCorrespondence(),
            "localization_parameters": {
                "moving": self.localizationParameters("source"),
                "reference": self.localizationParameters("target"),
            },
            "pairing_parameters": self.pairingParameters(),
            "correspondence": correspondence_metadata,
            "estimator": fit.estimator,
            "fit_parameters": dict(fit_params),
        }
        if model != fit.estimator:
            provenance["model_requested"] = model
        if fit.selection:
            provenance["model_selection"] = dict(fit.selection)
            provenance["model_rule"] = fit.selection.get("rule")
        if manual:
            provenance["fit"] = "least squares over every clicked pair"
            provenance["source_points"] = result.source_points.tolist()
            provenance["target_points_px"] = result.target_points.tolist()
        # Preserve compact Step-0 provenance keys for compatibility.
        provenance.update(correspondence_metadata)

        source_frame, target_frame = "moving", "reference"
        target_context = self._target_context
        inlier_norms = np.linalg.norm(fit.mapped_points - target_points, axis=1)[np.asarray(fit.inliers, bool)]
        if world is not None:
            detector = self._source_context.detector
            source_frame = f"detector:{detector}" if detector else "moving"
            target_frame = world.world_frame or "reference"
            extra = dict(target_context.extra)
            if world.world_axes:
                extra.setdefault("axes", [str(a) for a in world.world_axes])
            extra.setdefault("units", units)
            target_context = replace(target_context, extra=extra)
            provenance["reference_pixel_to_world"] = {
                "kind": world.pixel_to_world.kind, "params": world.pixel_to_world.to_params()}
            if len(inlier_norms) and units in ("um", "µm"):
                # The fit quality the pipeline log reports (SpatialTransformRef.quality).
                provenance.update(residual_rms_um=float(np.sqrt(np.mean(inlier_norms ** 2))),
                                  residual_max_um=float(inlier_norms.max()))
        warnings = self._fitWarnings(fit, world, units)
        if warnings:
            provenance["warnings"] = list(warnings)

        transform = SpatialTransform(
            model=fit.transform,
            source_frame=source_frame,
            target_frame=target_frame,
            units=units,
            source_context=self._source_context,
            target_context=target_context,
            residuals=fit.residuals,
            provenance=provenance,
        )
        self._fit_result = fit
        self._pair_fit = pair_fit
        self._fit_warnings = warnings
        self._transform_path = None
        self._saved_paths = {}
        self._qc_path = None
        limit = float(self.residualLimitSpin.value())
        worst = float(inlier_norms.max()) if len(inlier_norms) else 0.0
        if limit > 0 and worst > limit:
            self._transform = None
            self._refusal = (
                f"residuals too large: max {_length_text(worst, units)} above the "
                f"{_length_text(limit, units)} limit (drift between the two images, field distortion, "
                "wrong pairs or a bad click)")
            self._showRefusal(transform)
            self.sigTransformChanged.emit(None)
            self._emitStateChanged()
            return False
        self._refusal = None
        self._transform = transform

        self._showTransform("Fitted")
        self.sigCalibrated.emit(self._transform)
        self.sigTransformChanged.emit(self._transform)
        self._emitStateChanged()
        return True

    def _fitWarnings(self, fit: ModelFit, world: CalibrationSource | None, units: str) -> list[str]:
        warnings = []
        outliers = int(np.count_nonzero(~np.asarray(fit.inliers, bool)))
        if outliers:
            warnings.append(f"{outliers} paired points are fit outliers (left out)")
        matrix = fit.transform.as_matrix()
        pixel = self._source_context.pixel_size_um
        if world is not None and matrix is not None and pixel and units in ("um", "µm"):
            scale = math.sqrt(abs(np.linalg.det(np.asarray(matrix)[:2, :2])))
            configured = float(np.mean(pixel))
            if configured > 0 and abs(scale / configured - 1.0) > 0.2:
                warnings.append(
                    f"the fitted scale {scale:.4f} µm per moving px differs from the configured pixel size "
                    f"{configured:.4f} µm by {100 * (scale / configured - 1):+.0f} % -- binning, a wrong pixel "
                    "size, or a wrong galvo conversionFactor?")
        return warnings

    def calibrate(self) -> bool:
        """One-shot localization + transform fit (in the background when the
        host turned that on: then True means started)."""
        if self.selectedCorrespondence() == MANUAL:
            self._manualPairsChanged(fit=True)
            return self._transform is not None
        if (self._background and self._source_image is not None and self._target_image is not None
                and self.correspondenceSpec().supports_independent_localization):
            return self._startBackgroundLocalization(fit=True)
        if not self.runLocalization():
            return False
        return self.fitTransform()

    # -- background work -----------------------------------------------------

    def setBackgroundExecution(self, enabled: bool) -> None:
        """Run localization, pairing and verification off the GUI thread
        (with a Cancel button). Off by default: everything is synchronous."""
        self._background = bool(enabled)

    @property
    def busy(self) -> bool:
        return self._job is not None

    def _startJob(self, fn: Callable[[], Any], done: Callable[[Any, Any], None], text: str) -> bool:
        if self._job is not None:
            return False
        self._job_id += 1
        self._job = self._job_id
        self._job_handlers[self._job_id] = done
        self.busyLabel.setText(text)
        self.busyControls.setVisible(True)
        self._updateEnabledState()
        self._runner.submit(self._job_id, fn)
        return True

    def _jobDone(self, job_id: int, result: Any, error: Any) -> None:
        handler = self._job_handlers.pop(job_id, None)
        if job_id != self._job:
            return      # cancelled: dropped
        self._job = None
        self.busyControls.setVisible(False)
        self.busyLabel.setText("")
        self._updateEnabledState()
        if handler is not None:
            handler(result, error)

    def cancel(self) -> None:
        """Stop waiting for the running job; its result is dropped when it
        arrives (the computation itself finishes in the background)."""
        if self._job is None:
            return
        self._job_handlers.pop(self._job, None)
        self._job = None
        self._fit_after_localization = False
        self.busyControls.setVisible(False)
        self._setStatus("○ Cancelled")
        self._updateEnabledState()
        self._emitStateChanged()

    def _startBackgroundLocalization(self, *, fit: bool) -> bool:
        strategy = self.correspondenceSpec()
        params = {role: strategy.normalize(self.localizationParameters(role)) for role in ("source", "target")}
        pairing = self.pairingParameters()
        images = {"source": self._source_image, "target": self._target_image}

        def work():
            outcome: dict[str, Any] = {}
            for role in ("source", "target"):
                try:
                    outcome[role] = (strategy.localize(images[role], params[role]), None)
                except Exception as error:  # noqa: BLE001 - reported per image
                    outcome[role] = (None, error)
            if outcome["source"][0] is not None and outcome["target"][0] is not None:
                try:
                    outcome["pairs"] = (strategy.correspond(outcome["source"][0], outcome["target"][0],
                                                            params["source"], params["target"], pairing), None)
                except Exception as error:  # noqa: BLE001
                    outcome["pairs"] = (None, error)
            return outcome

        def done(outcome, error):
            if error is not None:
                self._setStatus(f"✕ Localization failed — {error}")
                self._emitStateChanged()
                return
            self._applyBackgroundLocalization(outcome, params)
            if self._fit_after_localization and self._correspondence_result is not None:
                self.fitTransform()
            self._fit_after_localization = False

        self._fit_after_localization = bool(fit)
        self._clearCorrespondence(emit=True)
        self._invalidateTransform(emit=True)
        return self._startJob(work, done, "Localizing and pairing…")

    def _applyBackgroundLocalization(self, outcome: Mapping[str, Any], params: Mapping[str, Any]) -> None:
        name = self.selectedCorrespondence()
        for role in ("source", "target"):
            result, error = outcome[role]
            if error is not None:
                self._localization_errors[role] = str(error)
                self._localization_failure_diagnostics[role] = getattr(error, "diagnostics", None)
                self._localization_stale[role] = self._localization_results[role] is not None
                continue
            self._strategy_parameters[name][role] = dict(params[role])
            if role == "source":
                self._parameters = self._strategy_parameters[name][role]
            self._localization_results[role] = result
            self._localization_stale[role] = False
            self._localization_errors[role] = None
            self._localization_failure_diagnostics[role] = None
        pairs = outcome.get("pairs")
        if pairs is None:
            self._setStatus("○ Waiting for valid Moving and Reference localization")
        elif pairs[1] is not None:
            self._correspondence_error = str(pairs[1])
            self._setStatus(f"○ Waiting for valid correspondence — {pairs[1]}")
        else:
            result = pairs[0]
            self._correspondence_result = result
            self._correspondence_error = None
            self._source_points = result.source_points
            self._target_points = result.target_points
            self.localizationStatusLabel.setText(f"Localization: {result.summary()}")
            self._setStatus(f"○ {result.summary()} — fit a transform")
            self.sigLocalizationChanged.emit(result)
        self._refreshLocalizationStatusLabels()
        self._updateEnabledState()
        self._emitStateChanged()

    def setTransform(
        self,
        transform: SpatialTransform | None,
        *,
        path: str | Path | None = None,
        prefix: str = "Loaded",
    ) -> None:
        """Install a transform supplied by a host without using a file dialog."""
        self._localization_results = {"source": None, "target": None}
        self._localization_stale = {"source": False, "target": False}
        self._localization_errors = {"source": None, "target": None}
        self._correspondence_result = None
        self._correspondence_error = None
        self._source_points = None
        self._target_points = None
        self._fit_result = None
        self._pair_fit = None
        self._refusal = None
        self._fit_warnings = []
        self._saved_paths = {}
        self.verdictLabel.clear()
        self.verdictLabel.setVisible(False)
        self._refreshLocalizationStatusLabels()
        self.localizationStatusLabel.setText(
            "Localization: not available for loaded transform"
            if transform is not None else "Localization: not run"
        )
        self._transform = transform
        self._transform_path = None if path is None else Path(path)
        if transform is None:
            self._setStatus("○ No transform")
            self.residualLabel.setText("Residuals: n/a")
            self.matrixView.clear()
        else:
            self._showTransform(prefix)
        self._updateEnabledState()
        self.sigLocalizationChanged.emit(None)
        self.sigTransformChanged.emit(transform)
        self._emitStateChanged()

    def loadTransform(self) -> None:
        path = guitools.askForFilePath(
            self, "Load transform", nameFilter=TRANSFORM_FILTER
        )
        if path:
            self.loadTransformFromPath(path)

    def loadTransformFromPath(
        self, path: str | Path, *, prefix: str | None = None
    ) -> bool:
        path = Path(path)
        try:
            transform = load(path)
        except Exception as error:
            self._warn("Could not load transform", f"{path.name}: {error}")
            return False
        self.setTransform(
            transform,
            path=path,
            prefix=prefix or f"Loaded {path.name}",
        )
        return True

    def saveTransform(self) -> None:
        if self._transform is None:
            return
        path = guitools.askForFilePath(
            self, "Save transform", nameFilter=TRANSFORM_FILTER, isSaving=True
        )
        if path:
            self.saveTransformToPath(path)

    def saveTransformToPath(self, path: str | Path) -> Path | None:
        if self._transform is None:
            return None
        path = Path(path)
        if not path.suffix:
            path = path.with_suffix(".json")
        try:
            save(path, self._transform)
        except Exception as error:
            self._warn("Could not save transform", f"{path.name}: {error}")
            return None
        self._transform_path = path
        self.setStatus(f"Saved {path.name}")
        return path

    def clear(self) -> None:
        self.setApplyChecked(False)
        self._localization_results = {"source": None, "target": None}
        self._localization_stale = {"source": False, "target": False}
        self._localization_errors = {"source": None, "target": None}
        self._correspondence_result = None
        self._correspondence_error = None
        self._source_points = None
        self._target_points = None
        self._fit_result = None
        self._transform = None
        self._transform_path = None
        self._pair_fit = None
        self._refusal = None
        self._fit_warnings = []
        self._saved_paths = {}
        self._qc_path = None
        self.verdictLabel.clear()
        self.verdictLabel.setVisible(False)
        self.predictResultLabel.clear()
        self.hostStatusLabel.clear()
        self.hostStatusLabel.setVisible(False)
        self._refreshLocalizationStatusLabels()
        self._setStatus("○ No transform")
        self.residualLabel.setText("Residuals: n/a")
        self.matrixView.clear()
        self._updateEnabledState()
        self.sigLocalizationChanged.emit(None)
        self.sigTransformChanged.emit(None)
        self._emitStateChanged()

    def visualize(self) -> None:
        """Compatibility action: open the shared diagnostic workspace."""
        self.openWorkspace("transform" if self._transform is not None else "localization")

    def openWorkspace(self, tab: str = "localization") -> None:
        """Open/raise the one persistent detached inspection workspace."""
        from .TransformCalibrationWorkspace import TransformCalibrationWorkspace

        workspace = self._workspace
        if workspace is None:
            workspace = TransformCalibrationWorkspace(self)
            self._workspace = workspace
            workspace.destroyed.connect(lambda *_: setattr(self, "_workspace", None))
        workspace.showTab(tab)
        workspace.show()
        workspace.raise_()
        workspace.activateWindow()

    # -- host seams -----------------------------------------------------------

    def setRecordingReader(self, reader: Callable[..., CalibrationSource] | None) -> None:
        """Let the host read its own recordings: ``reader(path, role,
        detector=None, **choices) -> CalibrationSource``. The Load buttons
        then also offer a Zarr store (a folder)."""
        self._recording_reader = reader
        for role, button in (("source", self.loadSourceButton), ("target", self.loadTargetButton)):
            if reader is None:
                button.setMenu(None)
                continue
            menu = QtWidgets.QMenu(button)
            menu.addAction("File (HDF5 recording, TIFF, NPY, PNG)…", lambda r=role: self._loadImage(r, "file"))
            menu.addAction("Zarr recording (folder)…", lambda r=role: self._loadImage(r, "zarr"))
            button.setMenu(menu)

    def setPairWarningProvider(self, provider: Callable[..., list] | None) -> None:
        """``provider(moving_source, reference_source) -> [warning, ...]``:
        what only the host can tell about the pair (different setups, a
        camera grid that changed since the recording...)."""
        self._pair_warning_provider = provider
        self._refreshPairWarnings()

    def setCalibrationStore(self, store: Any) -> None:
        """A place to save calibrations under an id (the Save row). ``store``
        has ``exists(id)``, ``save(transform, id, *, overwrite, report,
        figure) -> {kind: path, "notes": [...]}``, ``archive(id) -> new id``
        and ``folder``."""
        self._store = store
        self.storeControls.setVisible(store is not None)
        self._suggestId()
        self._updateEnabledState()

    def setDefaultFolder(self, folder: str | None) -> None:
        """Where the file dialogs start."""
        self._default_folder = folder

    # -- manual point pairs -------------------------------------------------

    @property
    def pairEditor(self) -> PointPairEditor:
        """The clicked pairs (Qt-free; change them through the methods below
        so the fit follows)."""
        return self._pair_editor

    @property
    def manualPairFit(self) -> PairFit | None:
        """The fit to the clicked pairs, with each pair's residual."""
        return self._pair_fit

    @property
    def manualProblem(self) -> str | None:
        """Why the clicked pairs give no transform yet (too few, degenerate)."""
        return self._manual_problem

    def snapSettings(self) -> tuple[bool, float]:
        return self._snap_enabled, self._snap_radius

    def setSnap(self, enabled: bool, radius: float | None = None) -> None:
        """Snap clicks to the brightness centre of the spot under them
        (on by default, within ``radius`` px, default 3)."""
        self._snap_enabled = bool(enabled)
        if radius is not None:
            self._snap_radius = max(0.0, float(radius))
        self._emitStateChanged()

    def addManualPoint(self, role: str, point: Any, *, snap: bool | None = None) -> int:
        """A click at ``point`` ``(row, col)`` in ``role``'s image; returns
        the pair it went to. Snapped to the spot centre when snapping is on."""
        role = self._normalizeRole(role)
        image = self.image(role)
        value = (float(point[0]), float(point[1]))
        if (self._snap_enabled if snap is None else snap) and image is not None:
            value = snap_to_local_maximum(image, value, self._snap_radius)
        index = self._pair_editor.add_point(role, value)
        self._manualPairsChanged()
        return index

    def deleteManualPair(self, index: int) -> None:
        self._pair_editor.delete(index)
        self._manualPairsChanged()

    def moveManualPair(self, index: int, offset: int) -> int:
        new = self._pair_editor.move(index, offset)
        self._manualPairsChanged()
        return new

    def undoManualPairs(self) -> bool:
        done = self._pair_editor.undo()
        self._manualPairsChanged()
        return done

    def clearManualPairs(self) -> None:
        self._pair_editor.clear()
        self._manualPairsChanged()

    def setManualPairs(self, source_points: Any, target_points: Any) -> None:
        """Replace the clicked pairs (Moving and Reference ``(N, 2)`` px)."""
        self._pair_editor.set_pairs(source_points, target_points)
        self._manualPairsChanged()

    def _manualPairsChanged(self, fit: bool = True) -> bool:
        """The pairs changed: rebuild the correspondence and, when the chosen
        model can be fitted to them, fit it (the residuals follow every click)."""
        if self.selectedCorrespondence() != MANUAL:
            self._emitStateChanged()
            return False
        source, target, _indices = self._pair_editor.complete_pairs()
        model = self.selectedModel()
        degree = int(self._fit_parameters.get("degree", 2)) if self._fit_parameters else 2
        need = minimum_pairs(model, degree)
        self._clearCorrespondence(emit=False)
        self._invalidateTransform(emit=True)
        pending = self._pair_editor.open_pair is not None
        if len(source) == 0:
            self._manual_problem = f"No pairs yet: {model} needs at least {need}."
            self.manualStatusLabel.setText(self._manual_problem)
            self._setStatus("○ Click pairs in the point picker")
            self._updateEnabledState()
            self._emitStateChanged()
            return False
        result = manual_correspondence(source, target)
        self._correspondence_result = result
        self._source_points, self._target_points = result.source_points, result.target_points
        self.localizationStatusLabel.setText(f"Localization: {result.summary()}")
        self.sigLocalizationChanged.emit(result)
        if len(source) < need:
            self._manual_problem = (f"{len(source)} pair{'s' if len(source) != 1 else ''}: {model} needs at "
                                    f"least {need}.")
            self._setStatus(f"○ {self._manual_problem}")
        elif fit:
            self._manual_problem = None
            if not self.fitTransform() and self._refusal is None:
                self._manual_problem = self.statusLabel.text().replace("✕ Transform fit failed — ", "")
        else:
            self._manual_problem = None
        text = f"{len(source)} pair{'s' if len(source) != 1 else ''} (minimum {need} for {model})"
        if pending:
            text += f"; pair {self._pair_editor.open_pair + 1} waits for its " \
                    f"{'Reference' if self._pair_editor.next_role() == 'target' else 'Moving'} click"
        if self._manual_problem:
            text += f"\n{self._manual_problem}"
        elif self._pair_fit is not None:
            residuals = self._pair_fit.fit.residuals
            text += f"; RMS {_length_text(residuals.rms, residuals.unit)}"
            worst = self._pair_fit.worst
            if worst is not None and self._pair_fit.loo is not None:
                text += f"; most suspicious: pair {worst + 1}"
        self.manualStatusLabel.setText(text)
        self._updateEnabledState()
        self._emitStateChanged()
        return True

    def openPointPicker(self) -> None:
        """Open/raise the point picker (both images side by side)."""
        from .TransformCalibrationPointPicker import PointPickerDialog

        if self.selectedCorrespondence() != MANUAL:
            self.setSelectedCorrespondence(MANUAL)
        picker = self._picker
        if picker is None:
            picker = PointPickerDialog(self)
            self._picker = picker
            picker.destroyed.connect(lambda *_: setattr(self, "_picker", None))
        picker.refreshFromState()
        picker.show()
        picker.raise_()
        picker.activateWindow()

    def mappedReferencePixels(self) -> np.ndarray | None:
        """Where the fitted transform puts the paired Moving points, in
        Reference *pixels* (the fit may be in world units)."""
        fit = self._fit_result
        if fit is None:
            return None
        mapped = np.asarray(fit.mapped_points, dtype=np.float64)
        reference = self._sources["target"]
        if reference is not None and reference.has_world:
            mapped = reference.pixel_to_world.inverse().map_points(mapped)
        return mapped

    # -- save under an id ---------------------------------------------------

    def saveToStore(self, calibration_id: str | None = None) -> dict | None:
        """Save the current transform under an id in the host's calibrations
        store, asking before an existing id is replaced (keep the old one as
        a dated copy, replace it, or cancel). Returns the paths written."""
        if self._transform is None or self._store is None:
            return None
        calibration_id = (calibration_id or self.idEdit.text()).strip()
        if not calibration_id:
            self.showMessage("Save calibration", "Give the calibration an id first.")
            return None
        archived = None
        overwrite = False
        if self._store.exists(calibration_id):
            answer = self.askOverwrite(calibration_id, f"{calibration_id}_<date-time>")
            if answer == "cancel":
                return None
            if answer == "keep":
                archived = self._store.archive(calibration_id)
            overwrite = True
        try:
            paths = self._store.save(self._transform, calibration_id, overwrite=overwrite,
                                     report=self.reportText(), figure=self.writeQcFigure)
        except Exception as error:  # noqa: BLE001 - shown, nothing half-done is hidden
            self._warn("Could not save calibration", str(error))
            return None
        paths = dict(paths)
        if archived:
            paths.setdefault("notes", []).append(f"the previous calibration was kept as '{archived}'")
        self._saved_paths = paths
        self._qc_path = paths.get("figure")
        record = paths.get("record")
        if record is not None:
            self._transform_path = Path(record)
        self.matrixView.setPlainText(self.reportText())
        where = "and the smart pipeline can name it" if paths.get("pipeline") else "(record only)"
        self.setStatus(f"Saved '{calibration_id}' {where}")
        self._updateEnabledState()
        return paths

    def _openStoreFolder(self) -> None:
        if self._store is not None:
            folder = Path(getattr(self._store, "folder", "")) / "transforms"
            self.openPath(folder if folder.is_dir() else folder.parent)

    # -- QC figure, prediction, verification ----------------------------------

    def writeQcFigure(self, path: str | Path) -> Path | None:
        """The QC figure of the current fit (PNG at ``path``)."""
        result = self._correspondence_result
        fit = self._fit_result
        if result is None or fit is None or self._source_image is None or self._target_image is None:
            return None
        mapped = self.mappedReferencePixels()
        source_loc = self._localization_results.get("source")
        detected = source_loc.points if source_loc is not None else None
        transform = self._transform
        title = ""
        if transform is not None and transform.as_matrix() is not None:
            g = calibration_report.decompose_matrix(transform.as_matrix())
            title = (f"{fit.estimator}: rot {g['rotation_deg']:.2f} deg, scale {g['scale']:.5g} "
                     f"{transform.units}/px, mirror {'yes' if g['mirror'] else 'no'}")
        residuals = np.linalg.norm(
            fit.mapped_points - (self._sources["target"].pixel_to_world.map_points(result.target_points)
                                 if self._sources["target"] is not None and self._sources["target"].has_world
                                 else result.target_points), axis=1)
        return calibration_report.qc_figure(
            self._source_image, self._target_image, result.source_points, result.target_points, mapped,
            inliers=fit.inliers, residuals=residuals, unit=fit.residuals.unit, title=title,
            source_detected=detected, path=path)

    def openQcFigure(self) -> Path | None:
        path = self._qc_path
        if path is None or not Path(path).exists():
            import tempfile

            path = Path(tempfile.mkdtemp(prefix="transform_qc_")) / "qc.png"
            path = self.writeQcFigure(path)
            self._qc_path = path
        if path is not None:
            self.openPath(path)
        return path

    def predict(self, row: float, col: float) -> dict | None:
        """Where Moving pixel ``(row, col)`` lands in the Reference's
        coordinates: ``{axis: value}`` (scan axes in µm for a scan Reference)."""
        if self._transform is None:
            return None
        point = self._transform.map_points(np.array([[float(row), float(col)]]))[0]
        axes = list(self._transform.target_context.extra.get("axes") or ())
        unit = self._transform.units
        if len(axes) != 2:
            axes = ["row", "col"]
        result = {str(axes[0]): float(point[0]), str(axes[1]): float(point[1])}
        suffix = "µm" if unit in ("um", "µm") else unit
        self.predictResultLabel.setText(", ".join(f"{a} = {v:.3f} {suffix}" for a, v in result.items()))
        return result

    def verifyWithFiles(self, files: tuple | None = None) -> bool:
        """Check the current transform on another pair of files (asked for
        when not given): both are localized with the current method's
        parameters (Beads for clicked pairs) and each Reference point is
        compared with the nearest mapped Moving point -- no fitting."""
        if self._transform is None:
            return False
        files = files or self.askVerificationFiles()
        if not files:
            return False
        sources = []
        for role, path in zip(("source", "target"), files):
            if isinstance(path, CalibrationSource):
                sources.append(path)
                continue
            path = Path(path)
            try:
                if self._recording_reader is not None and (path.suffix.lower() in (".h5", ".hdf5", ".zarr")
                                                           or path.is_dir()):
                    current = self._sources[role]
                    detector = current.detector if current is not None else None
                    try:
                        sources.append(self._recording_reader(path, role, detector))
                    except Exception:  # noqa: BLE001 - that detector is not in this file
                        sources.append(self._recording_reader(path, role, None))
                else:
                    sources.append(CalibrationSource(load_image_file(path), path.name, path=path))
            except Exception as error:  # noqa: BLE001
                self.showMessage("Verification", f"Could not read {path.name}: {error}")
                return False
        transform = self._transform
        name = self.selectedCorrespondence()
        spec = self._correspondence_specs["beads" if name == MANUAL else name]
        params = {role: dict(self._strategy_parameters[spec.name][role]) for role in ("source", "target")}
        for role, source in zip(("source", "target"), sources):
            if "pixel_size_row" in spec.parameter_specs:
                rows, cols = source.world_pixel_sizes()
                params[role].update(pixel_size_row=rows, pixel_size_col=cols)
        moving, reference = sources

        def work():
            if not spec.supports_independent_localization:
                pairs = spec.run(moving.image, reference.image, params["source"], params["target"])
                src_pts, tgt_pts = pairs.source_points, pairs.target_points
            else:
                src_pts = spec.localize(moving.image, params["source"]).points
                tgt_pts = spec.localize(reference.image, params["target"]).points
            if reference.has_world:
                tgt_pts = reference.pixel_to_world.map_points(tgt_pts)
            unit = transform.units
            tolerance = 0.5 if unit in ("um", "µm") else max(
                2.0, 5.0 * float(transform.residuals.rms if transform.residuals else 1.0))
            report = calibration_report.verify_on_points(transform, src_pts, tgt_pts, tolerance=tolerance)
            report["grid_problems"] = calibration_report.context_problems(
                transform.source_context, moving.context)
            report["files"] = [moving.name, reference.name]
            return report

        def done(report, error):
            if error is not None:
                self.showMessage("Verification", f"Could not verify: {error}")
                return
            self._verification = report
            self.showMessage("Verification", _verification_text(report))
            self._emitStateChanged()

        if self._background:
            return self._startJob(work, done, "Verifying…")
        try:
            done(work(), None)
        except Exception as error:  # noqa: BLE001
            done(None, error)
        return True

    @property
    def verification(self) -> dict | None:
        """The last verification report."""
        return self._verification

    # -- dialogs (replaced in tests) -------------------------------------------

    def askOverwrite(self, calibration_id: str, archived_id: str) -> str:
        """'replace', 'keep' (keep the old one as ``archived_id``) or 'cancel'."""
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setWindowTitle("Calibration exists")
        box.setText(f'A calibration "{calibration_id}" exists already.')
        box.setInformativeText(f'Keep the old one as "{archived_id}", replace it, or cancel?')
        keep = box.addButton("Keep old as dated copy", QtWidgets.QMessageBox.AcceptRole)
        replace_button = box.addButton("Replace", QtWidgets.QMessageBox.DestructiveRole)
        box.addButton(QtWidgets.QMessageBox.Cancel)
        box.exec_() if hasattr(box, "exec_") else box.exec()
        clicked = box.clickedButton()
        return "keep" if clicked is keep else "replace" if clicked is replace_button else "cancel"

    def askOpenFolder(self, title: str) -> str | None:
        path = QtWidgets.QFileDialog.getExistingDirectory(self, title, str(self._default_folder or ""))
        return path or None

    def askVerificationFiles(self) -> tuple | None:
        """Two paths (Moving, Reference) for the verification, or None."""
        moving = guitools.askForFilePath(self, "Verification: moving file", defaultFolder=self._default_folder,
                                         nameFilter=RECORDING_FILTER)
        if not moving:
            return None
        reference = guitools.askForFilePath(self, "Verification: reference file",
                                            defaultFolder=str(Path(moving).parent), nameFilter=RECORDING_FILTER)
        return (moving, reference) if reference else None

    def showMessage(self, title: str, text: str) -> None:
        QtWidgets.QMessageBox.information(self, title, text)

    def openPath(self, path: str | Path) -> None:
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))

    def copyText(self, text: str) -> None:
        QtWidgets.QApplication.clipboard().setText(text)

    # -- helpers ----------------------------------------------------------

    def _invalidateLocalization(self, text: str, *, emit: bool) -> None:
        """Compatibility helper: clear both localization stages completely."""
        self._clearAllLocalization(aggregate=text, emit=emit)
        self._emitStateChanged()

    def _invalidateTransform(self, *, emit: bool) -> None:
        had_transform = self._transform is not None
        self._transform = None
        self._fit_result = None
        self._pair_fit = None
        self._transform_path = None
        self._refusal = None
        self._fit_warnings = []
        self._saved_paths = {}
        self._qc_path = None
        self._setStatus("○ No transform fit")
        self.residualLabel.setText("Residuals: n/a")
        self.matrixView.clear()
        self.verdictLabel.clear()
        self.verdictLabel.setVisible(False)
        self.predictResultLabel.clear()
        self._updateEnabledState()
        if emit and had_transform:
            self.sigTransformChanged.emit(None)

    def _showTransform(self, prefix: str) -> None:
        assert self._transform is not None
        residuals = self._transform.residuals
        if residuals is None:
            self._setStatus(f"✓ {prefix} transform")
            self.residualLabel.setText("Residuals: n/a")
        else:
            self._setStatus(
                f"✓ {residuals.n_points} pairs · RMS {residuals.rms:.3f} "
                f"{residuals.unit} · max {residuals.max:.3f} {residuals.unit}"
            )
            self.residualLabel.setText(f"Residuals: {residuals.summary()}")
        self.matrixView.setPlainText(self.reportText())
        if self._fit_result is not None:
            residuals = self._transform.residuals
            g = None
            matrix = self._transform.as_matrix()
            if matrix is not None:
                g = calibration_report.decompose_matrix(matrix)
            text = f"Accepted: {residuals.n_inliers if residuals else '?'} pairs"
            if residuals is not None:
                text += f", residual RMS {_length_text(residuals.rms, residuals.unit)}"
            if g is not None:
                text += f", rotation {g['rotation_deg']:.2f}°, {'mirrored' if g['mirror'] else 'not mirrored'}"
            self.verdictLabel.setText(text + ".")
            self.verdictLabel.setStyleSheet(_OK_STYLE)
            self.verdictLabel.setVisible(True)
        self._updateEnabledState()

    def _showRefusal(self, transform: SpatialTransform) -> None:
        self._setStatus(f"✕ Refused — {self._refusal}")
        self.verdictLabel.setText(f"Refused: {self._refusal}. Nothing can be saved.")
        self.verdictLabel.setStyleSheet(_BAD_STYLE)
        self.verdictLabel.setVisible(True)
        self.matrixView.setPlainText("\n".join(self._reportLines(transform)))
        self._updateEnabledState()

    def reportText(self) -> str:
        """The result in words (what the result box shows and Copy report copies)."""
        if self._transform is None:
            return ""
        return "\n".join(self._reportLines(self._transform))

    def _reportLines(self, transform: SpatialTransform) -> list[str]:
        lines = calibration_report.describe_transform(transform, self._fit_result)
        source = self._sources["source"]
        target = self._sources["target"]
        names = [f"Moving: {source.name}" if source else None, f"Reference: {target.name}" if target else None]
        lines = [line for line in names if line] + lines
        if self._pair_fit is not None and self._correspondence_result is not None:
            unit = transform.units
            lines.append("Pair residuals (leave-one-out in brackets):")
            loo = self._pair_fit.loo
            for k, value in enumerate(self._pair_fit.residuals):
                check = "" if loo is None or not np.isfinite(loo[k]) else f" ({_length_text(loo[k], unit)})"
                lines.append(f"  {k + 1}: {_length_text(value, unit)}{check}")
        lines += [f"Warning: {w}" for w in self._fit_warnings]
        lines += [f"Warning: {w}" for w in self._pair_warnings]
        if self._saved_paths:
            lines += [f"{kind.capitalize()}: {path}" for kind, path in self._saved_paths.items()
                      if kind != "notes"]
            lines += [f"Note: {note}" for note in self._saved_paths.get("notes", [])]
        return lines

    def _setStatus(self, text: str) -> None:
        self.statusLabel.setText(text)

    def setStatus(self, text: str) -> None:
        """Host-facing message seam, separate from transform fit status."""
        message = str(text)
        self.hostStatusLabel.setText(message)
        self.hostStatusLabel.setVisible(bool(message))
        self._emitStateChanged()

    def _warn(self, title: str, text: str) -> None:
        QtWidgets.QMessageBox.warning(self, title, text)

    def _normalizeParameters(self, params: dict) -> dict[str, Any]:
        """Compatibility helper: normalize localization parameters only."""
        return self.correspondenceSpec().normalize(params)

    def _normalizeTransformParameters(self, params: dict) -> dict[str, Any]:
        return normalize_fit_parameters(self.selectedModel(), params)


def _context_for_image(
    context: AcquisitionContext | None,
    image: np.ndarray,
    path: Path | None,
) -> AcquisitionContext:
    base = context or AcquisitionContext()
    extra = dict(base.extra)
    if path is not None:
        extra.setdefault("file", path.name)
    return replace(base, image_shape=tuple(image.shape), extra=extra)


def _verification_text(report: Mapping[str, Any]) -> str:
    unit = str(report.get("units", "px"))
    lines = [f"{report['matched']} of {report['reference_points']} Reference points have a mapped Moving point "
             f"within {_length_text(report['tolerance'], unit)}."]
    if report["matched"]:
        offset = ", ".join(_length_text(v, unit) for v in report["mean_offset"])
        lines.append(f"Distance RMS {_length_text(report['rms'], unit)}, median {_length_text(report['median'], unit)}, "
                     f"max {_length_text(report['max'], unit)}; mean offset {offset}.")
    problems = report.get("grid_problems") or []
    lines.append("Moving grid: " + ("; ".join(problems) if problems else "as calibrated."))
    return "\n".join(lines)


def _length_text(value: float, unit: str) -> str:
    if unit in ("um", "µm"):
        return f"{value * 1000:.0f} nm"
    return f"{value:.3f} {unit}"


def _facts_table(lines: list) -> str:
    """``(label, value)`` facts as a two-column HTML table."""
    def cell(value: Any) -> str:
        text = str(value).replace("&", "&amp;").replace("<", "&lt;")
        return text.replace(", ", ",<br>") if len(text) > 40 else text

    half = (len(lines) + 1) // 2

    def table(part: list) -> str:
        return "<table>" + "".join(
            f'<tr><td style="padding-right:6px"><b>{cell(label)}</b></td><td>{cell(value)}</td></tr>'
            for label, value in part) + "</table>"

    return (f'<table><tr><td valign="top" style="padding-right:14px">{table(lines[:half])}</td>'
            f'<td valign="top">{table(lines[half:])}</td></tr></table>')


def _thumbnail(image: np.ndarray, size: QtCore.QSize) -> QtGui.QPixmap:
    """A grey thumbnail of ``image`` (1-99.7 percentile levels)."""
    data = np.asarray(image, dtype=np.float64)
    step = max(1, int(max(data.shape) // max(1, max(size.width(), size.height()) * 2)))
    data = data[::step, ::step]
    lo, hi = np.percentile(data, (1, 99.7)) if data.size else (0.0, 1.0)
    scaled = np.clip((data - lo) / (hi - lo if hi > lo else 1.0), 0, 1)
    grey = np.ascontiguousarray((scaled * 255).astype(np.uint8))
    qimage = QtGui.QImage(grey.data, grey.shape[1], grey.shape[0], grey.strides[0],
                          QtGui.QImage.Format_Grayscale8).copy()
    return QtGui.QPixmap.fromImage(qimage).scaled(size, QtCore.Qt.KeepAspectRatio,
                                                  QtCore.Qt.SmoothTransformation)


def _as_calibration_image(image: Any, label: str) -> np.ndarray:
    array = np.squeeze(np.asarray(image))
    if array.ndim != 2:
        raise ValueError(f"{label} must be 2-D, got shape {array.shape}")
    return np.ascontiguousarray(array)


def _format_axis(axis: Any, image: np.ndarray, title: str) -> None:
    axis.set_title(title)
    axis.set_xlim(-0.5, image.shape[1] - 0.5)
    axis.set_ylim(image.shape[0] - 0.5, -0.5)
    axis.set_aspect("equal")
    axis.legend(loc="upper right", fontsize=8)


def _figure_canvas() -> Any:
    try:
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
    except ImportError:  # matplotlib < 3.5
        from matplotlib.backends.backend_qt5agg import (  # type: ignore[no-redef]
            FigureCanvasQTAgg,
        )
    return FigureCanvasQTAgg


def main() -> int:
    import sys

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    widget = TransformCalibrationWidget()
    widget.resize(680, 440)
    widget.show()
    return int(app.exec_() if hasattr(app, "exec_") else app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
