"""General UI for creating, inspecting and applying a spatial calibration.

The widget deliberately owns only transform-calibration concepts: two images,
a correspondence/localization strategy, a transform model, persistence and an
opaque apply target supplied by its host. Hardware acquisition and viewer
lifecycle stay outside :mod:`imcommon`; ImControl wires those through the
signals and registration seams exposed here.

It can still be run standalone with file-loaded images::

    python -m imswitch.imcommon.view.widgets.TransformCalibrationWidget
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from qtpy import QtCore, QtWidgets

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
from imswitch.imcommon.algorithms.transforms.correspondence import (
    CorrespondenceResult,
    CorrespondenceStrategySpec,
    ImageLocalizationResult,
    LocalizationDiagnostics,
    builtin_correspondence_specs,
)
from imswitch.imcommon.view import guitools

__all__ = ["TransformCalibrationWidget", "load_image_file", "main"]


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
        self._parameters: dict[str, Any] = {}  # compatibility alias: Moving params
        self._model_specs = {spec.name: spec for spec in estimator_specs()}
        self._model_parameters = {
            name: fit_parameter_defaults(name) for name in self._model_specs
        }
        self._fit_parameters: dict[str, Any] = {}

        self._buildUi()
        for spec in builtin_correspondence_specs():
            self.registerCorrespondenceStrategy(spec)
        self.setSelectedCorrespondence("foci_grid")
        self._modelChanged()
        self.setHostIntegrationEnabled(False)
        self._updateEnabledState()

    # -- construction ----------------------------------------------------

    def _buildUi(self) -> None:
        self.loadSourceButton = guitools.BetterPushButton("Load moving image...")
        self.loadTargetButton = guitools.BetterPushButton("Load reference image...")
        self.snapSourceButton = guitools.BetterPushButton("Snap moving")
        self.snapTargetButton = guitools.BetterPushButton("Snap reference")
        self.sourceLabel = QtWidgets.QLabel("Moving: not loaded")
        self.targetLabel = QtWidgets.QLabel("Reference: not loaded")

        self.correspondenceCombo = guitools.BetterComboBox()
        self.correspondenceHintLabel = QtWidgets.QLabel()
        self.correspondenceHintLabel.setWordWrap(True)
        self.correspondenceHintLabel.setVisible(False)
        self.localizationParametersButton = guitools.BetterPushButton("Inspect / tune…")
        self.editParametersButton = self.localizationParametersButton

        self.runLocalizationButton = guitools.BetterPushButton("Run localization")
        self.calibrateButton = self.runLocalizationButton
        self.sourceLocalizationStatusLabel = QtWidgets.QLabel("Moving      ○ Not localized")
        self.targetLocalizationStatusLabel = QtWidgets.QLabel("Reference   ○ Not localized")
        self.sourceLocalizationStatusLabel.setWordWrap(True)
        self.targetLocalizationStatusLabel.setWordWrap(True)
        # Compatibility-only aggregate label retained for existing callers/tests.
        self.localizationStatusLabel = QtWidgets.QLabel("Localization: not run")
        self.localizationStatusLabel.setVisible(False)

        self.modelCombo = guitools.BetterComboBox()
        for spec in estimator_specs():
            self.modelCombo.addItem(spec.label, spec.name)
            self.modelCombo.setItemData(
                self.modelCombo.count() - 1,
                f"{spec.description}\n\nNeeds at least {spec.min_samples} point pair(s).",
                QtCore.Qt.ToolTipRole,
            )
        self.modelCombo.setCurrentIndex(0)
        self.modelHintLabel = QtWidgets.QLabel()
        self.modelHintLabel.setVisible(False)
        self.transformParametersButton = guitools.BetterPushButton("Inspect / tune…")
        self.fitTransformButton = guitools.BetterPushButton("Fit transform")

        # Compatibility alias: the old inspector action now opens the shared workspace.
        self.visualizeButton = self.localizationParametersButton
        self.loadTransformButton = guitools.BetterPushButton("Load")
        self.saveTransformButton = guitools.BetterPushButton("Save")
        self.clearButton = guitools.BetterPushButton("Clear")

        self.statusLabel = QtWidgets.QLabel("○ No transform")
        self.statusLabel.setWordWrap(True)
        self.hostStatusLabel = QtWidgets.QLabel("")
        self.hostStatusLabel.setWordWrap(True)
        self.hostStatusLabel.setVisible(False)
        self.residualLabel = QtWidgets.QLabel("Residuals: n/a")
        self.residualLabel.setVisible(False)
        self.matrixView = QtWidgets.QPlainTextEdit()
        self.matrixView.setReadOnly(True)
        self.matrixView.setMaximumHeight(80)
        self.matrixView.setPlaceholderText(
            "The fitted matrix appears here, moving -> reference, in (row, col)."
        )

        images = QtWidgets.QGridLayout()
        images.addWidget(self.loadTargetButton, 0, 0)
        images.addWidget(self.snapTargetButton, 0, 1)
        images.addWidget(self.targetLabel, 0, 2)
        images.addWidget(self.loadSourceButton, 1, 0)
        images.addWidget(self.snapSourceButton, 1, 1)
        images.addWidget(self.sourceLabel, 1, 2)
        images.setColumnStretch(2, 1)

        localization_title = QtWidgets.QLabel("<b>Localization</b>")
        localization_method = QtWidgets.QHBoxLayout()
        localization_method.addWidget(QtWidgets.QLabel("Method:"))
        localization_method.addWidget(self.correspondenceCombo, 1)
        localization_actions = QtWidgets.QHBoxLayout()
        localization_actions.addWidget(self.runLocalizationButton)
        localization_actions.addWidget(self.localizationParametersButton)
        localization_actions.addStretch(1)

        transform_title = QtWidgets.QLabel("<b>Transform</b>")
        model = QtWidgets.QHBoxLayout()
        model.addWidget(QtWidgets.QLabel("Model:"))
        model.addWidget(self.modelCombo, 1)
        transform_actions = QtWidgets.QHBoxLayout()
        transform_actions.addWidget(self.fitTransformButton)
        transform_actions.addWidget(self.transformParametersButton)
        transform_actions.addStretch(1)

        persistence = QtWidgets.QHBoxLayout()
        persistence.addWidget(self.loadTransformButton)
        persistence.addWidget(self.saveTransformButton)
        persistence.addWidget(self.clearButton)
        persistence.addStretch(1)

        self.applyControls = QtWidgets.QWidget()
        apply_layout = QtWidgets.QHBoxLayout(self.applyControls)
        apply_layout.setContentsMargins(0, 0, 0, 0)
        self.applyCheck = QtWidgets.QCheckBox("Apply")
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

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(images)
        layout.addSpacing(8)
        layout.addWidget(localization_title)
        layout.addLayout(localization_method)
        layout.addLayout(localization_actions)
        layout.addWidget(self.sourceLocalizationStatusLabel)
        layout.addWidget(self.targetLocalizationStatusLabel)
        layout.addSpacing(8)
        layout.addWidget(transform_title)
        layout.addLayout(model)
        layout.addLayout(transform_actions)
        layout.addWidget(self.statusLabel)
        layout.addSpacing(6)
        layout.addLayout(persistence)
        layout.addWidget(self.applyControls)
        layout.addWidget(self.hostStatusLabel)
        layout.addWidget(self.matrixView)
        layout.addStretch()

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
        self.fitTransformButton.clicked.connect(self.fitTransform)
        self.loadTransformButton.clicked.connect(self.loadTransform)
        self.saveTransformButton.clicked.connect(self.saveTransform)
        self.clearButton.clicked.connect(self.clear)
        self.applyCheck.toggled.connect(self.sigApplyToggled)
        self.applyTargetCombo.currentIndexChanged.connect(
            lambda *_: self.sigApplyTargetChanged.emit(self.selectedApplyTarget())
        )
        self.setStartupButton.clicked.connect(lambda *_: self.sigSetStartupRequested.emit())

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
        self.localizationParametersButton.setEnabled(True)
        self._clearAllLocalization(
            aggregate=f"Localization: not run for {spec.label}", emit=True
        )
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
        self.snapSourceButton.setVisible(bool(enabled))
        self.snapTargetButton.setVisible(bool(enabled))
        self.applyControls.setVisible(bool(enabled))

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
        self.runLocalizationButton.setEnabled(has_images)
        self.fitTransformButton.setEnabled(has_correspondence)
        self.saveTransformButton.setEnabled(has_transform)
        # The diagnostic workspace is useful before success too.
        self.localizationParametersButton.setEnabled(True)
        self.transformParametersButton.setEnabled(True)
        has_target = self.applyTargetCombo.count() > 0
        self.applyCheck.setEnabled(has_transform and has_target)
        self.applyTargetCombo.setEnabled(has_transform and has_target)
        self.setStartupButton.setEnabled(has_transform)

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
        self._source_image = _as_calibration_image(image, "moving image")
        self._source_path = None if name is None or context is not None else Path(name)
        self._source_context = _context_for_image(
            context, self._source_image, self._source_path
        )
        self.sourceLabel.setText(
            f"Moving: {name or 'in memory'}  {self._source_image.shape}"
        )
        self._clearLocalizationRole("source")

    def setTargetImage(
        self,
        image: Any,
        name: str | None = None,
        *,
        context: AcquisitionContext | None = None,
    ) -> None:
        self._target_image = _as_calibration_image(image, "reference image")
        self._target_path = None if name is None or context is not None else Path(name)
        self._target_context = _context_for_image(
            context, self._target_image, self._target_path
        )
        self.targetLabel.setText(
            f"Reference: {name or 'in memory'}  {self._target_image.shape}"
        )
        self._clearLocalizationRole("target")

    # -- actions ----------------------------------------------------------

    def _loadImage(self, role: str) -> None:
        path = guitools.askForFilePath(
            self,
            f"Load {'moving' if role == 'source' else 'reference'} image",
            nameFilter=IMAGE_FILTER,
        )
        if not path:
            return
        try:
            image = load_image_file(path)
        except Exception as error:
            self._warn("Could not load image", f"{Path(path).name}: {error}")
            return
        setter = self.setSourceImage if role == "source" else self.setTargetImage
        setter(image, Path(path).name)

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
        """Fast path: localize Moving and Reference, then pair them."""
        if self._source_image is None or self._target_image is None:
            return False
        strategy = self.correspondenceSpec()
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
        try:
            fit_params = normalize_fit_parameters(model, self._fit_parameters)
            fit = fit_transform(
                model,
                self._correspondence_result.source_points,
                self._correspondence_result.target_points,
                input_shape=tuple(self._source_image.shape),
                **fit_params,
            )
        except Exception as error:
            self._invalidateTransform(emit=True)
            self._setStatus(f"✕ Transform fit failed — {error}")
            self._updateEnabledState()
            self._emitStateChanged()
            return False

        self._fit_parameters = fit_params
        self._model_parameters[model] = fit_params
        result = self._correspondence_result
        correspondence_metadata = dict(result.metadata)
        provenance = {
            "strategy": self.selectedCorrespondence(),
            "localization_parameters": {
                "moving": self.localizationParameters("source"),
                "reference": self.localizationParameters("target"),
            },
            "correspondence": correspondence_metadata,
            "estimator": fit.estimator,
            "fit_parameters": dict(fit_params),
        }
        # Preserve compact Step-0 provenance keys for compatibility.
        provenance.update(correspondence_metadata)

        self._fit_result = fit
        self._transform = SpatialTransform(
            model=fit.transform,
            source_frame="moving",
            target_frame="reference",
            units="px",
            source_context=self._source_context,
            target_context=self._target_context,
            residuals=fit.residuals,
            provenance=provenance,
        )
        self._transform_path = None

        self._showTransform("Fitted")
        self.sigCalibrated.emit(self._transform)
        self.sigTransformChanged.emit(self._transform)
        self._emitStateChanged()
        return True

    def calibrate(self) -> bool:
        """Backward-compatible one-shot localization + transform fit."""
        if not self.runLocalization():
            return False
        return self.fitTransform()

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

    # -- helpers ----------------------------------------------------------

    def _invalidateLocalization(self, text: str, *, emit: bool) -> None:
        """Compatibility helper: clear both localization stages completely."""
        self._clearAllLocalization(aggregate=text, emit=emit)
        self._emitStateChanged()

    def _invalidateTransform(self, *, emit: bool) -> None:
        had_transform = self._transform is not None
        self._transform = None
        self._fit_result = None
        self._transform_path = None
        self._setStatus("○ No transform fit")
        self.residualLabel.setText("Residuals: n/a")
        self.matrixView.clear()
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
        matrix = self._transform.as_matrix()
        self.matrixView.setPlainText(
            "not an affine model -- no matrix"
            if matrix is None
            else "\n".join(
                "  ".join(f"{value:+10.5f}" for value in row) for row in matrix
            )
        )
        self._updateEnabledState()

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
