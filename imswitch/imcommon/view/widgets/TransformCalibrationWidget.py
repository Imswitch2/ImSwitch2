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
    fit_transform,
    load,
    save,
)
from imswitch.imcommon.algorithms.transforms.correspondence import (
    CorrespondenceStrategySpec,
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
    "residual_threshold": 2.0,
    "max_relative_tilt_deg": 30.0,
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


class TransformCalibrationWidget(QtWidgets.QWidget):
    """Fit a selectable transform model from selectable image correspondence."""

    sigCalibrated = QtCore.Signal(object)
    sigTransformChanged = QtCore.Signal(object)
    sigSnapSourceRequested = QtCore.Signal()
    sigSnapTargetRequested = QtCore.Signal()
    sigApplyToggled = QtCore.Signal(bool)
    sigApplyTargetChanged = QtCore.Signal(object)
    sigSetStartupRequested = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Transform calibration")

        self._source_image: np.ndarray | None = None
        self._target_image: np.ndarray | None = None
        self._source_path: Path | None = None
        self._target_path: Path | None = None
        self._source_context = AcquisitionContext()
        self._target_context = AcquisitionContext()
        self._transform: SpatialTransform | None = None
        self._transform_path: Path | None = None
        self._source_points: np.ndarray | None = None
        self._target_points: np.ndarray | None = None
        self._visualization: QtWidgets.QDialog | None = None

        self._correspondence_specs: dict[str, CorrespondenceStrategySpec] = {}
        self._strategy_parameters: dict[str, dict[str, Any]] = {}
        self._parameters: dict[str, Any] = {}

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
        self.correspondenceHintLabel.setEnabled(False)

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
        self.modelHintLabel.setWordWrap(True)
        self.modelHintLabel.setEnabled(False)

        self.editParametersButton = guitools.BetterPushButton("Edit parameters")
        self.calibrateButton = guitools.BetterPushButton("Calibrate")
        self.visualizeButton = guitools.BetterPushButton("Visualize")
        self.loadTransformButton = guitools.BetterPushButton("Load")
        self.saveTransformButton = guitools.BetterPushButton("Save")
        self.clearButton = guitools.BetterPushButton("Clear")

        self.statusLabel = QtWidgets.QLabel("No transform")
        self.statusLabel.setWordWrap(True)
        self.residualLabel = QtWidgets.QLabel("Residuals: n/a")
        self.matrixView = QtWidgets.QPlainTextEdit()
        self.matrixView.setReadOnly(True)
        self.matrixView.setMaximumHeight(90)
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

        correspondence = QtWidgets.QHBoxLayout()
        correspondence.addWidget(QtWidgets.QLabel("Localization / correspondence:"))
        correspondence.addWidget(self.correspondenceCombo, 1)

        model = QtWidgets.QHBoxLayout()
        model.addWidget(QtWidgets.QLabel("Transform model:"))
        model.addWidget(self.modelCombo, 1)

        actions = QtWidgets.QGridLayout()
        actions.addWidget(self.calibrateButton, 0, 0)
        actions.addWidget(self.visualizeButton, 0, 1)
        actions.addWidget(self.editParametersButton, 0, 2)
        actions.addWidget(self.loadTransformButton, 1, 0)
        actions.addWidget(self.saveTransformButton, 1, 1)
        actions.addWidget(self.clearButton, 1, 2)
        for column in range(3):
            actions.setColumnStretch(column, 1)

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
        layout.addLayout(correspondence)
        layout.addWidget(self.correspondenceHintLabel)
        layout.addLayout(model)
        layout.addWidget(self.modelHintLabel)
        layout.addLayout(actions)
        layout.addWidget(self.applyControls)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.residualLabel)
        layout.addWidget(self.matrixView)
        layout.addStretch()

        self.loadSourceButton.clicked.connect(lambda: self._loadImage("source"))
        self.loadTargetButton.clicked.connect(lambda: self._loadImage("target"))
        self.snapSourceButton.clicked.connect(
            lambda *_: self.sigSnapSourceRequested.emit()
        )
        self.snapTargetButton.clicked.connect(
            lambda *_: self.sigSnapTargetRequested.emit()
        )
        self.correspondenceCombo.currentIndexChanged.connect(
            self._correspondenceChanged
        )
        self.modelCombo.currentIndexChanged.connect(self._modelChanged)
        self.editParametersButton.clicked.connect(self.editParameters)
        self.calibrateButton.clicked.connect(self.calibrate)
        self.visualizeButton.clicked.connect(self.visualize)
        self.loadTransformButton.clicked.connect(self.loadTransform)
        self.saveTransformButton.clicked.connect(self.saveTransform)
        self.clearButton.clicked.connect(self.clear)
        self.applyCheck.toggled.connect(self.sigApplyToggled)
        self.applyTargetCombo.currentIndexChanged.connect(
            lambda *_: self.sigApplyTargetChanged.emit(self.selectedApplyTarget())
        )
        self.setStartupButton.clicked.connect(
            lambda *_: self.sigSetStartupRequested.emit()
        )

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
        values.setdefault("residual_threshold", 2.0)

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
        self._strategy_parameters[spec.name] = values
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
        self.correspondenceCombo.setCurrentIndex(index)
        self._correspondenceChanged()

    def _correspondenceChanged(self) -> None:
        name = self.selectedCorrespondence()
        spec = self._correspondence_specs.get(name)
        if spec is None:
            return
        self._parameters = self._strategy_parameters[name]
        self.correspondenceHintLabel.setText(spec.description)

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
        spec = next(
            spec for spec in estimator_specs() if spec.name == self.selectedModel()
        )
        self.modelHintLabel.setText(spec.description)

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
    def currentTransformPath(self) -> Path | None:
        return self._transform_path

    def _updateEnabledState(self) -> None:
        has_images = self._source_image is not None and self._target_image is not None
        has_transform = self._transform is not None
        self.calibrateButton.setEnabled(has_images)
        self.saveTransformButton.setEnabled(has_transform)
        self.visualizeButton.setEnabled(
            has_transform and self._source_points is not None
        )
        has_target = self.applyTargetCombo.count() > 0
        self.applyCheck.setEnabled(has_transform and has_target)
        self.applyTargetCombo.setEnabled(has_transform and has_target)
        self.setStartupButton.setEnabled(has_transform)

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
        self._source_points = None
        self._target_points = None
        self._updateEnabledState()

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
        self._source_points = None
        self._target_points = None
        self._updateEnabledState()

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
        updated = guitools.JsonEditorDialog.edit_params(
            self,
            self._parameters,
            title=f"Edit {self.selectedCorrespondence()} calibration parameters",
        )
        if updated is None:
            return
        try:
            self._parameters = self._normalizeParameters(updated)
            self._strategy_parameters[self.selectedCorrespondence()] = self._parameters
        except (KeyError, TypeError, ValueError) as error:
            self._warn("Invalid parameters", str(error))

    def calibrate(self) -> None:
        if self._source_image is None or self._target_image is None:
            return

        strategy_name = self.selectedCorrespondence()
        strategy = self._correspondence_specs[strategy_name]
        try:
            params = self._normalizeParameters(self._parameters)
            result = strategy.run(self._source_image, self._target_image, params)
            source_points = result.source_points
            target_points = result.target_points
            fit = fit_transform(
                self.selectedModel(),
                source_points,
                target_points,
                residual_threshold=params["residual_threshold"],
                input_shape=tuple(self._source_image.shape),
            )
        except Exception as error:
            self._setStatus(f"Calibration failed: {error}")
            self._transform = None
            self._transform_path = None
            self._source_points = None
            self._target_points = None
            self.residualLabel.setText("Residuals: n/a")
            self.matrixView.clear()
            self._updateEnabledState()
            self.sigTransformChanged.emit(None)
            return

        self._source_points = source_points
        self._target_points = target_points
        correspondence_metadata = dict(result.metadata)
        provenance = {
            "strategy": strategy_name,
            "correspondence": correspondence_metadata,
            "estimator": fit.estimator,
        }
        # Preserve the compact Step-0 provenance keys for compatibility while
        # making the full strategy metadata available under "correspondence".
        provenance.update(correspondence_metadata)

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

        self._showTransform("Calibrated")
        self.sigCalibrated.emit(self._transform)
        self.sigTransformChanged.emit(self._transform)

    def setTransform(
        self,
        transform: SpatialTransform | None,
        *,
        path: str | Path | None = None,
        prefix: str = "Loaded",
    ) -> None:
        """Install a transform supplied by a host without using a file dialog."""
        self._transform = transform
        self._transform_path = None if path is None else Path(path)
        self._source_points = None
        self._target_points = None
        if transform is None:
            self._setStatus("No transform")
            self.residualLabel.setText("Residuals: n/a")
            self.matrixView.clear()
        else:
            self._showTransform(prefix)
        self._updateEnabledState()
        self.sigTransformChanged.emit(transform)

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
        self._setStatus(f"Saved {path.name}  |  {self._transform.summary()}")
        return path

    def clear(self) -> None:
        self.setApplyChecked(False)
        self._transform = None
        self._transform_path = None
        self._source_points = None
        self._target_points = None
        self._setStatus("No transform")
        self.residualLabel.setText("Residuals: n/a")
        self.matrixView.clear()
        self._updateEnabledState()
        self.sigTransformChanged.emit(None)

    def visualize(self) -> None:
        if (
            self._transform is None
            or self._source_points is None
            or self._target_points is None
            or self._source_image is None
            or self._target_image is None
        ):
            return
        try:
            dialog = self._buildVisualization()
        except Exception as error:
            self._warn("Could not visualize", str(error))
            return
        if self._visualization is not None:
            self._visualization.close()
        self._visualization = dialog
        dialog.show()

    # -- helpers ----------------------------------------------------------

    def _showTransform(self, prefix: str) -> None:
        assert self._transform is not None
        estimator = self._transform.provenance.get("estimator")
        descriptor = (
            self._transform.kind
            if estimator in (None, self._transform.kind)
            else f"{estimator} -> {self._transform.kind}"
        )
        self._setStatus(
            f"{prefix}  |  {self._transform.source_frame} -> "
            f"{self._transform.target_frame} "
            f"[{descriptor}, {self._transform.ndim}-D, {self._transform.units}]"
        )
        residuals = self._transform.residuals
        self.residualLabel.setText(
            "Residuals: n/a"
            if residuals is None
            else f"Residuals: {residuals.summary()}"
        )
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
        """Host-facing status seam for apply/persistence messages."""
        self._setStatus(str(text))

    def _warn(self, title: str, text: str) -> None:
        QtWidgets.QMessageBox.warning(self, title, text)

    def _normalizeParameters(self, params: dict) -> dict[str, Any]:
        strategy = self._correspondence_specs[self.selectedCorrespondence()]
        strategy_values = strategy.normalize(params)
        normalized = dict(params)
        normalized.update(strategy_values)
        normalized["residual_threshold"] = float(params["residual_threshold"])
        if normalized["residual_threshold"] <= 0:
            raise ValueError("residual_threshold must be greater than 0")
        return normalized

    def _buildVisualization(self) -> QtWidgets.QDialog:
        figure_canvas = _figure_canvas()
        from matplotlib.figure import Figure

        assert self._transform is not None
        assert self._source_points is not None and self._target_points is not None

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Transform calibration")
        dialog.resize(1000, 540)

        figure = Figure(figsize=(10, 5), dpi=100)
        canvas = figure_canvas(figure)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(canvas)

        source_axis, target_axis = figure.subplots(1, 2)
        source_axis.imshow(self._source_image, cmap="gray")
        source_axis.scatter(
            self._source_points[:, 1],
            self._source_points[:, 0],
            s=30,
            facecolors="none",
            edgecolors="cyan",
            linewidths=1.0,
            label="detected (moving)",
        )
        _format_axis(source_axis, self._source_image, "Moving")

        mapped = self._transform.map_points(self._source_points)
        target_axis.imshow(self._target_image, cmap="gray")
        target_axis.scatter(
            self._target_points[:, 1],
            self._target_points[:, 0],
            s=44,
            facecolors="none",
            edgecolors="lime",
            linewidths=1.0,
            label="detected (reference)",
        )
        target_axis.scatter(
            mapped[:, 1],
            mapped[:, 0],
            s=12,
            marker="x",
            color="magenta",
            linewidths=0.9,
            label="moving, transformed",
        )
        _format_axis(target_axis, self._target_image, "Reference")
        figure.tight_layout()
        canvas.draw()
        return dialog


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
