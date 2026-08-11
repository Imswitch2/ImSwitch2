"""Standalone harness for creating and inspecting a spatial transform.

Step 0 of the transform-module plan: prove the core model end to end with zero
hardware and zero risk to the code that currently owns transforms. Both images
come from files rather than from a detector, so a calibration can be built,
saved, reloaded and eyeballed on recorded data alone.

This is deliberately a plain ``QWidget`` with its logic inline, not an
ImSwitch widget/controller pair. It lives in ``imcommon`` so that imcontrol and
improcess can both host it later, and ``imcommon`` may not import either of
them. The live snap-from-detector path and the display-layer apply belong to
the hookup phases, once the affines are trusted.

Run it on its own with::

    python -m imswitch.imcommon.view.widgets.TransformCalibrationWidget
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
    find_grid_correspondence,
)
from imswitch.imcommon.view import guitools

__all__ = ["TransformCalibrationWidget", "load_image_file", "main"]


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
    """Read a 2-D image from one of the formats a recording is likely to be in.

    Volumes and stacks are reduced to a single plane by taking the middle index
    of every leading axis -- a calibration is measured on one plane, and
    silently averaging or max-projecting would change the spot shapes the
    detector is about to fit.
    """
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
    """Load two images, fit a transform between them, save it, look at it."""

    sigCalibrated = QtCore.Signal(object)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Transform calibration")

        self._parameters = dict(DEFAULT_PARAMETERS)
        self._source_image: np.ndarray | None = None
        self._target_image: np.ndarray | None = None
        self._source_path: Path | None = None
        self._target_path: Path | None = None
        self._transform: SpatialTransform | None = None
        self._source_points: np.ndarray | None = None
        self._target_points: np.ndarray | None = None
        self._visualization: QtWidgets.QDialog | None = None

        self._buildUi()
        self._modelChanged()
        self._updateEnabledState()

    # -- construction ----------------------------------------------------

    def _buildUi(self) -> None:
        self.loadSourceButton = guitools.BetterPushButton("Load moving image...")
        self.loadTargetButton = guitools.BetterPushButton("Load reference image...")
        self.sourceLabel = QtWidgets.QLabel("Moving: not loaded")
        self.targetLabel = QtWidgets.QLabel("Reference: not loaded")

        self.modelCombo = guitools.BetterComboBox()
        for spec in estimator_specs():
            self.modelCombo.addItem(spec.label, spec.name)
            self.modelCombo.setItemData(
                self.modelCombo.count() - 1,
                f"{spec.description}\n\nNeeds at least {spec.min_samples} point "
                f"pair(s).",
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
        images.addWidget(self.targetLabel, 0, 1)
        images.addWidget(self.loadSourceButton, 1, 0)
        images.addWidget(self.sourceLabel, 1, 1)
        images.setColumnStretch(1, 1)

        model = QtWidgets.QHBoxLayout()
        model.addWidget(QtWidgets.QLabel("Model:"))
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

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(images)
        layout.addLayout(model)
        layout.addWidget(self.modelHintLabel)
        layout.addLayout(actions)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.residualLabel)
        layout.addWidget(self.matrixView)
        layout.addStretch()

        self.loadSourceButton.clicked.connect(lambda: self._loadImage("source"))
        self.loadTargetButton.clicked.connect(lambda: self._loadImage("target"))
        self.modelCombo.currentIndexChanged.connect(self._modelChanged)
        self.editParametersButton.clicked.connect(self.editParameters)
        self.calibrateButton.clicked.connect(self.calibrate)
        self.visualizeButton.clicked.connect(self.visualize)
        self.loadTransformButton.clicked.connect(self.loadTransform)
        self.saveTransformButton.clicked.connect(self.saveTransform)
        self.clearButton.clicked.connect(self.clear)

    # -- state -----------------------------------------------------------

    @property
    def transform(self) -> SpatialTransform | None:
        """The calibrated or loaded transform, if there is one."""
        return self._transform

    def selectedModel(self) -> str:
        """The estimator name currently chosen in the Model box."""
        return str(self.modelCombo.currentData())

    def setSelectedModel(self, name: str) -> None:
        """Choose the model class to fit, by estimator name."""
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

    def _updateEnabledState(self) -> None:
        has_images = self._source_image is not None and self._target_image is not None
        self.calibrateButton.setEnabled(has_images)
        self.saveTransformButton.setEnabled(self._transform is not None)
        self.visualizeButton.setEnabled(
            self._transform is not None and self._source_points is not None
        )

    def setSourceImage(self, image: Any, name: str | None = None) -> None:
        """Set the moving image directly, bypassing the file dialog.

        Also the seam the live-acquisition front-end will use later: a snapped
        detector frame goes in here, and nothing else about the harness changes.
        """
        self._source_image = _as_calibration_image(image, "moving image")
        self._source_path = None if name is None else Path(name)
        self.sourceLabel.setText(
            f"Moving: {name or 'in memory'}  {self._source_image.shape}"
        )
        self._updateEnabledState()

    def setTargetImage(self, image: Any, name: str | None = None) -> None:
        """Set the reference image directly, bypassing the file dialog."""
        self._target_image = _as_calibration_image(image, "reference image")
        self._target_path = None if name is None else Path(name)
        self.targetLabel.setText(
            f"Reference: {name or 'in memory'}  {self._target_image.shape}"
        )
        self._updateEnabledState()

    # -- actions ---------------------------------------------------------

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
            self, self._parameters, title="Edit calibration parameters"
        )
        if updated is None:
            return
        try:
            self._parameters = self._normalizeParameters(updated)
        except (KeyError, TypeError, ValueError) as error:
            self._warn("Invalid parameters", str(error))

    def calibrate(self) -> None:
        if self._source_image is None or self._target_image is None:
            return

        try:
            source_points, target_points = find_grid_correspondence(
                self._source_image,
                self._target_image,
                n_rows=self._parameters["n_rows"],
                n_cols=self._parameters["n_cols"],
                min_distance=self._parameters["min_distance"],
                threshold_rel=self._parameters["threshold_rel"],
                refine_radius=self._parameters["refine_radius"],
                gaussian_sigma=self._parameters["gaussian_sigma"],
                max_relative_tilt_deg=self._parameters["max_relative_tilt_deg"],
            )
            fit = fit_transform(
                self.selectedModel(),
                source_points,
                target_points,
                residual_threshold=self._parameters["residual_threshold"],
                input_shape=tuple(self._source_image.shape),
            )
        except Exception as error:
            self._setStatus(f"Calibration failed: {error}")
            self._transform = None
            self._source_points = None
            self._target_points = None
            self.residualLabel.setText("Residuals: n/a")
            self.matrixView.clear()
            self._updateEnabledState()
            return

        self._source_points = source_points
        self._target_points = target_points
        self._transform = SpatialTransform(
            model=fit.transform,
            source_frame="moving",
            target_frame="reference",
            units="px",
            source_context=AcquisitionContext(
                image_shape=tuple(self._source_image.shape),
                extra=(
                    {"file": self._source_path.name} if self._source_path else {}
                ),
            ),
            target_context=AcquisitionContext(
                image_shape=tuple(self._target_image.shape),
                extra=(
                    {"file": self._target_path.name} if self._target_path else {}
                ),
            ),
            residuals=fit.residuals,
            provenance={
                "strategy": "foci_grid",
                # Which model class was fitted. A rigid or similarity fit is
                # stored as an affine matrix, so without this the constraint
                # that produced it would be lost.
                "estimator": fit.estimator,
                "n_rows": int(self._parameters["n_rows"]),
                "n_cols": int(self._parameters["n_cols"]),
                "detection": {
                    key: self._parameters[key]
                    for key in (
                        "min_distance",
                        "threshold_rel",
                        "refine_radius",
                        "gaussian_sigma",
                    )
                },
            },
        )

        self._showTransform("Calibrated")
        self.sigCalibrated.emit(self._transform)

    def loadTransform(self) -> None:
        path = guitools.askForFilePath(
            self, "Load transform", nameFilter=TRANSFORM_FILTER
        )
        if not path:
            return
        try:
            self._transform = load(path)
        except Exception as error:
            self._warn("Could not load transform", f"{Path(path).name}: {error}")
            return

        # Points belong to the images the fit was made from, not to a file that
        # was loaded afterwards; drop them so Visualize cannot show a stale
        # overlay against the wrong picture.
        self._source_points = None
        self._target_points = None
        self._showTransform(f"Loaded {Path(path).name}")

    def saveTransform(self) -> None:
        if self._transform is None:
            return
        path = guitools.askForFilePath(
            self, "Save transform", nameFilter=TRANSFORM_FILTER, isSaving=True
        )
        if not path:
            return
        if not Path(path).suffix:
            path = f"{path}.json"
        try:
            save(path, self._transform)
        except Exception as error:
            self._warn("Could not save transform", f"{Path(path).name}: {error}")
            return
        self._setStatus(f"Saved {Path(path).name}  |  {self._transform.summary()}")

    def clear(self) -> None:
        self._transform = None
        self._source_points = None
        self._target_points = None
        self._setStatus("No transform")
        self.residualLabel.setText("Residuals: n/a")
        self.matrixView.clear()
        self._updateEnabledState()

    def visualize(self) -> None:
        """Show both images with their detected and mapped points overlaid."""
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

    # -- helpers ---------------------------------------------------------

    def _showTransform(self, prefix: str) -> None:
        assert self._transform is not None
        # Residuals get their own line below; repeating them here just makes
        # both harder to read.
        estimator = self._transform.provenance.get("estimator")
        # "affine" is the stored kind for rigid and similarity fits too, so name
        # the estimator when it differs -- otherwise the constraint is invisible.
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
            "Residuals: n/a" if residuals is None else f"Residuals: {residuals.summary()}"
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

    def _warn(self, title: str, text: str) -> None:
        QtWidgets.QMessageBox.warning(self, title, text)

    def _normalizeParameters(self, params: dict) -> dict[str, Any]:
        normalized = {
            "n_rows": int(params["n_rows"]),
            "n_cols": int(params["n_cols"]),
            "min_distance": int(params["min_distance"]),
            "threshold_rel": float(params["threshold_rel"]),
            "refine_radius": int(params["refine_radius"]),
            "gaussian_sigma": float(params["gaussian_sigma"]),
            "residual_threshold": float(params["residual_threshold"]),
            "max_relative_tilt_deg": float(params["max_relative_tilt_deg"]),
        }
        if normalized["n_rows"] < 1 or normalized["n_cols"] < 1:
            raise ValueError("n_rows and n_cols must be at least 1")
        if normalized["min_distance"] < 1:
            raise ValueError("min_distance must be at least 1")
        if not 0.0 <= normalized["threshold_rel"] <= 1.0:
            raise ValueError("threshold_rel must be between 0 and 1")
        if normalized["refine_radius"] < 0:
            raise ValueError("refine_radius must be at least 0")
        if normalized["gaussian_sigma"] < 0:
            raise ValueError("gaussian_sigma must be at least 0")
        if normalized["residual_threshold"] <= 0:
            raise ValueError("residual_threshold must be greater than 0")
        if not 0.0 < normalized["max_relative_tilt_deg"] <= 45.0:
            raise ValueError("max_relative_tilt_deg must be within (0, 45]")
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
        # The overlay that actually judges the fit: where the moving points
        # land once transformed. Tight rings mean a good calibration.
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
    """Return matplotlib's Qt canvas across backend renames."""
    try:
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
    except ImportError:  # matplotlib < 3.5
        from matplotlib.backends.backend_qt5agg import (  # type: ignore[no-redef]
            FigureCanvasQTAgg,
        )
    return FigureCanvasQTAgg


def main() -> int:
    """Run the harness on its own, without starting ImSwitch."""
    import sys

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    widget = TransformCalibrationWidget()
    widget.resize(620, 340)
    widget.show()
    return int(app.exec_() if hasattr(app, "exec_") else app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
