"""Detached inspection/tuning workspace for transform calibration.

The compact :class:`TransformCalibrationWidget` remains the authoritative state
and fast-path workflow.  This window only exposes richer visualization,
per-image localization tuning, and transform-fit diagnostics.
"""

from __future__ import annotations

from typing import Any, Mapping, TYPE_CHECKING

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.algorithms.transforms import ParameterSpec, parameter_defaults

if TYPE_CHECKING:
    from .TransformCalibrationWidget import TransformCalibrationWidget

__all__ = ["ParameterFormWidget", "TransformCalibrationWorkspace"]


class ParameterFormWidget(QtWidgets.QWidget):
    """Embeddable typed parameter editor used by both workspace tabs."""

    sigValuesChanged = QtCore.Signal(object)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._specs: dict[str, ParameterSpec] = {}
        self._editors: dict[str, QtWidgets.QWidget] = {}
        self._updating = False
        self._root = QtWidgets.QVBoxLayout(self)
        self._root.setContentsMargins(0, 0, 0, 0)
        self._root.setSpacing(6)

    def setParameters(
        self, specs: Mapping[str, ParameterSpec], values: Mapping[str, Any]
    ) -> None:
        self._specs = dict(specs)
        while self._root.count():
            item = self._root.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._editors.clear()
        self._updating = True
        try:
            groups: dict[str, list[tuple[str, ParameterSpec]]] = {}
            advanced: list[tuple[str, ParameterSpec]] = []
            for name, spec in self._specs.items():
                if spec.advanced:
                    advanced.append((name, spec))
                else:
                    groups.setdefault(spec.group or "Parameters", []).append((name, spec))
            for group, items in groups.items():
                self._root.addWidget(self._buildGroup(group, items, values))
            if advanced:
                button = QtWidgets.QToolButton()
                button.setText("Advanced")
                button.setCheckable(True)
                button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
                button.setArrowType(QtCore.Qt.RightArrow)
                self._root.addWidget(button)
                advanced_box = QtWidgets.QWidget()
                advanced_layout = QtWidgets.QVBoxLayout(advanced_box)
                advanced_layout.setContentsMargins(8, 0, 0, 0)
                grouped: dict[str, list[tuple[str, ParameterSpec]]] = {}
                for name, spec in advanced:
                    grouped.setdefault(spec.group or "Advanced", []).append((name, spec))
                for group, items in grouped.items():
                    advanced_layout.addWidget(self._buildGroup(group, items, values))
                advanced_box.setVisible(False)
                self._root.addWidget(advanced_box)
                button.toggled.connect(advanced_box.setVisible)
                button.toggled.connect(
                    lambda checked, b=button: b.setArrowType(
                        QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow
                    )
                )
            defaults = QtWidgets.QPushButton("Restore defaults")
            defaults.clicked.connect(self._restoreDefaults)
            self._root.addWidget(defaults)
            self._root.addStretch(1)
        finally:
            self._updating = False
        self._refreshConditions()

    def _buildGroup(
        self,
        title: str,
        items: list[tuple[str, ParameterSpec]],
        values: Mapping[str, Any],
    ) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox(title)
        form = QtWidgets.QFormLayout(box)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        for name, spec in items:
            editor = self._makeEditor(spec, values.get(name, spec.default))
            editor.setToolTip(spec.description)
            label = QtWidgets.QLabel(spec.label)
            label.setToolTip(spec.description)
            form.addRow(label, editor)
            self._editors[name] = editor
            self._connectEditor(editor)
        return box

    def _makeEditor(self, spec: ParameterSpec, value: Any) -> QtWidgets.QWidget:
        if spec.choices is not None:
            widget = QtWidgets.QComboBox()
            for choice in spec.choices:
                widget.addItem(str(choice), choice)
            index = widget.findData(value)
            if index >= 0:
                widget.setCurrentIndex(index)
            return widget
        if spec.value_type is bool:
            widget = QtWidgets.QCheckBox()
            widget.setChecked(bool(value))
            return widget
        if spec.value_type is int:
            widget = QtWidgets.QSpinBox()
            widget.setRange(
                int(spec.minimum) if spec.minimum is not None else -2_000_000_000,
                int(spec.maximum) if spec.maximum is not None else 2_000_000_000,
            )
            widget.setSingleStep(int(spec.step) if spec.step is not None else 1)
            widget.setValue(int(value))
            return widget
        if spec.value_type is float:
            widget = QtWidgets.QDoubleSpinBox()
            widget.setDecimals(spec.decimals if spec.decimals is not None else 6)
            widget.setRange(
                float(spec.minimum) if spec.minimum is not None else -1e12,
                float(spec.maximum) if spec.maximum is not None else 1e12,
            )
            widget.setSingleStep(float(spec.step) if spec.step is not None else 0.1)
            widget.setValue(float(value))
            return widget
        return QtWidgets.QLineEdit(str(value))

    def _connectEditor(self, editor: QtWidgets.QWidget) -> None:
        if isinstance(editor, QtWidgets.QComboBox):
            editor.currentIndexChanged.connect(self._changed)
        elif isinstance(editor, QtWidgets.QCheckBox):
            editor.toggled.connect(self._changed)
        elif isinstance(editor, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
            editor.valueChanged.connect(self._changed)
        elif isinstance(editor, QtWidgets.QLineEdit):
            editor.editingFinished.connect(self._changed)

    def _value(self, name: str) -> Any:
        editor = self._editors[name]
        if isinstance(editor, QtWidgets.QComboBox):
            return editor.currentData()
        if isinstance(editor, QtWidgets.QCheckBox):
            return editor.isChecked()
        if isinstance(editor, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
            return editor.value()
        if isinstance(editor, QtWidgets.QLineEdit):
            return editor.text()
        raise TypeError(type(editor).__name__)

    def values(self) -> dict[str, Any]:
        result = {}
        for name, spec in self._specs.items():
            result[name] = spec.normalize(self._value(name))
        return result

    def _changed(self, *_args: Any) -> None:
        if self._updating:
            return
        self._refreshConditions()
        try:
            values = self.values()
        except (TypeError, ValueError):
            return
        self.sigValuesChanged.emit(values)

    def _refreshConditions(self) -> None:
        raw = {}
        for name in self._editors:
            try:
                raw[name] = self._value(name)
            except Exception:
                pass
        for name, spec in self._specs.items():
            editor = self._editors.get(name)
            if editor is None or spec.enabled_when is None:
                continue
            dependency, expected = spec.enabled_when
            editor.setEnabled(raw.get(dependency) == expected)

    def _restoreDefaults(self) -> None:
        self.setParameters(self._specs, parameter_defaults(self._specs))
        self.sigValuesChanged.emit(parameter_defaults(self._specs))


class _LocalizationViewer(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.setAspectLocked(True)
        self.plot.getViewBox().invertY(True)
        self.plot.hideAxis("left")
        self.plot.hideAxis("bottom")
        self.image_item = pg.ImageItem(axisOrder="row-major")
        self.plot.addItem(self.image_item)
        self.detected = pg.ScatterPlotItem(symbol="+", size=10, pen=pg.mkPen("#7ec8e3", width=1.5))
        self.predicted = pg.ScatterPlotItem(symbol="o", size=9, pen=pg.mkPen("#b0b0b0", width=1.2), brush=None)
        self.refined = pg.ScatterPlotItem(symbol="x", size=10, pen=pg.mkPen("#55aa55", width=1.5))
        self.plot.addItem(self.predicted)
        self.plot.addItem(self.detected)
        self.plot.addItem(self.refined)
        root.addWidget(self.plot, 1)
        controls = QtWidgets.QHBoxLayout()
        self.detected_check = QtWidgets.QCheckBox("Detected")
        self.predicted_check = QtWidgets.QCheckBox("Predicted grid")
        self.refined_check = QtWidgets.QCheckBox("Refined")
        for check in (self.detected_check, self.predicted_check, self.refined_check):
            check.setChecked(True)
            controls.addWidget(check)
        controls.addStretch(1)
        root.addLayout(controls)
        self.detected_check.toggled.connect(self.detected.setVisible)
        self.predicted_check.toggled.connect(self.predicted.setVisible)
        self.refined_check.toggled.connect(self.refined.setVisible)
        self._image_key = None

    @staticmethod
    def _setPoints(item: pg.ScatterPlotItem, points: np.ndarray | None) -> None:
        if points is None or len(points) == 0:
            item.setData([], [])
            return
        array = np.asarray(points)
        item.setData(x=array[:, 1], y=array[:, 0])

    def setData(self, image: np.ndarray | None, result: Any, *, key: Any) -> None:
        reset = key != self._image_key
        self._image_key = key
        if image is None:
            self.image_item.setVisible(False)
            self._setPoints(self.detected, None)
            self._setPoints(self.predicted, None)
            self._setPoints(self.refined, None)
            return
        self.image_item.setVisible(True)
        self.image_item.setImage(np.asarray(image), autoLevels=True)
        diagnostics = None if result is None else result.diagnostics
        self._setPoints(self.detected, None if diagnostics is None else diagnostics.detected_points)
        self._setPoints(self.predicted, None if diagnostics is None else diagnostics.predicted_points)
        self._setPoints(self.refined, None if diagnostics is None else diagnostics.refined_points)
        if reset:
            self.plot.autoRange()


class _TransformViewer(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.setAspectLocked(True)
        self.plot.getViewBox().invertY(True)
        self.plot.hideAxis("left")
        self.plot.hideAxis("bottom")
        self.image_item = pg.ImageItem(axisOrder="row-major")
        self.reference = pg.ScatterPlotItem(symbol="o", size=9, pen=pg.mkPen("#55aa55", width=1.4), brush=None)
        self.mapped = pg.ScatterPlotItem(symbol="x", size=9, pen=pg.mkPen("#4c8bd8", width=1.3))
        self.outliers = pg.ScatterPlotItem(symbol="x", size=11, pen=pg.mkPen("#ff9966", width=1.6))
        self.residuals = pg.PlotDataItem(pen=pg.mkPen("#ff6666", width=1.0))
        for item in (self.image_item, self.residuals, self.reference, self.mapped, self.outliers):
            self.plot.addItem(item)
        root.addWidget(self.plot, 1)
        self._key = None

    def setData(self, owner: "TransformCalibrationWidget") -> None:
        image = owner.image("target")
        result = owner.correspondenceResult
        fit = owner.fitResult
        if image is None:
            self.image_item.setVisible(False)
            return
        self.image_item.setVisible(True)
        self.image_item.setImage(np.asarray(image), autoLevels=True)
        if result is None:
            self.reference.setData([], [])
            self.mapped.setData([], [])
            self.outliers.setData([], [])
            self.residuals.setData([], [])
        else:
            target = result.target_points
            self.reference.setData(x=target[:, 1], y=target[:, 0])
            if fit is None:
                self.mapped.setData([], [])
                self.outliers.setData([], [])
                self.residuals.setData([], [])
            else:
                mapped = np.asarray(fit.mapped_points)
                inliers = np.asarray(fit.inliers, dtype=bool)
                self.mapped.setData(x=mapped[inliers, 1], y=mapped[inliers, 0])
                self.outliers.setData(x=mapped[~inliers, 1], y=mapped[~inliers, 0])
                xs, ys = [], []
                for reference, predicted in zip(target, mapped):
                    xs.extend([reference[1], predicted[1], np.nan])
                    ys.extend([reference[0], predicted[0], np.nan])
                self.residuals.setData(xs, ys)
        key = (id(image), tuple(image.shape))
        if key != self._key:
            self._key = key
            self.plot.autoRange()


class TransformCalibrationWorkspace(QtWidgets.QDialog):
    """One persistent detached window for diagnostics, tuning and reruns."""

    def __init__(self, owner: "TransformCalibrationWidget") -> None:
        super().__init__(owner)
        self.owner = owner
        self._skipLocalizationFormSync = False
        self._skipTransformFormSync = False
        self.setWindowTitle("Transform Calibration — Inspect / tune")
        self.setWindowModality(QtCore.Qt.NonModal)
        self.setWindowFlags(
            self.windowFlags()
            | QtCore.Qt.WindowMinimizeButtonHint
            | QtCore.Qt.WindowMaximizeButtonHint
        )
        self.resize(1280, 820)
        self.setMinimumSize(900, 620)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, False)

        root = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        root.addWidget(self.tabs, 1)
        self.localizationTab = self._buildLocalizationTab()
        self.transformTab = self._buildTransformTab()
        self.tabs.addTab(self.localizationTab, "Localization")
        self.tabs.addTab(self.transformTab, "Transform")
        close = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        close.rejected.connect(self.hide)
        root.addWidget(close)

        owner.sigStateChanged.connect(self.refreshFromState)
        self.refreshFromState()

    def showTab(self, tab: str) -> None:
        index = 1 if str(tab).lower().startswith("transform") else 0
        self.tabs.setCurrentIndex(index)
        self.refreshFromState()

    def _buildLocalizationTab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(page)
        selector = QtWidgets.QHBoxLayout()
        selector.addWidget(QtWidgets.QLabel("Image:"))
        self.imageCombo = QtWidgets.QComboBox()
        self.imageCombo.addItem("Moving", "source")
        self.imageCombo.addItem("Reference", "target")
        selector.addWidget(self.imageCombo)
        selector.addStretch(1)
        root.addLayout(selector)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.localizationViewer = _LocalizationViewer()
        splitter.addWidget(self.localizationViewer)
        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        method_row = QtWidgets.QHBoxLayout()
        method_row.addWidget(QtWidgets.QLabel("Method:"))
        self.methodCombo = QtWidgets.QComboBox()
        method_row.addWidget(self.methodCombo, 1)
        right_layout.addLayout(method_row)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        self.localizationForm = ParameterFormWidget()
        scroll.setWidget(self.localizationForm)
        right_layout.addWidget(scroll, 1)
        self.runSelectedButton = QtWidgets.QPushButton("Run localization")
        right_layout.addWidget(self.runSelectedButton)
        self.localizationStatus = QtWidgets.QLabel()
        self.localizationStatus.setWordWrap(True)
        right_layout.addWidget(self.localizationStatus)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([760, 440])
        root.addWidget(splitter, 1)

        self.imageCombo.currentIndexChanged.connect(self._localizationSelectionChanged)
        self.methodCombo.currentIndexChanged.connect(self._methodChanged)
        self.localizationForm.sigValuesChanged.connect(self._localizationParametersChanged)
        self.runSelectedButton.clicked.connect(self._runSelectedLocalization)
        return page

    def _buildTransformTab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(page)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.transformViewer = _TransformViewer()
        splitter.addWidget(self.transformViewer)
        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        model_row = QtWidgets.QHBoxLayout()
        model_row.addWidget(QtWidgets.QLabel("Model:"))
        self.workspaceModelCombo = QtWidgets.QComboBox()
        model_row.addWidget(self.workspaceModelCombo, 1)
        right_layout.addLayout(model_row)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        self.transformForm = ParameterFormWidget()
        scroll.setWidget(self.transformForm)
        right_layout.addWidget(scroll, 1)
        self.fitButton = QtWidgets.QPushButton("Fit transform")
        right_layout.addWidget(self.fitButton)
        self.transformStatus = QtWidgets.QLabel()
        self.transformStatus.setWordWrap(True)
        right_layout.addWidget(self.transformStatus)
        self.matrixView = QtWidgets.QPlainTextEdit()
        self.matrixView.setReadOnly(True)
        self.matrixView.setMaximumHeight(150)
        right_layout.addWidget(self.matrixView)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([760, 440])
        root.addWidget(splitter, 1)

        self.workspaceModelCombo.currentIndexChanged.connect(self._workspaceModelChanged)
        self.transformForm.sigValuesChanged.connect(self._transformParametersChanged)
        self.fitButton.clicked.connect(self.owner.fitTransform)
        return page

    def _selectedRole(self) -> str:
        return str(self.imageCombo.currentData() or "source")

    def refreshFromState(self) -> None:
        self._syncMethodCombo()
        self._syncModelCombo()
        self._refreshLocalizationPanel()
        self._refreshTransformPanel()

    def _syncMethodCombo(self) -> None:
        blocker = QtCore.QSignalBlocker(self.methodCombo)
        self.methodCombo.clear()
        for spec in self.owner.correspondenceSpecs():
            self.methodCombo.addItem(spec.label, spec.name)
        index = self.methodCombo.findData(self.owner.selectedCorrespondence())
        if index >= 0:
            self.methodCombo.setCurrentIndex(index)
        del blocker

    def _syncModelCombo(self) -> None:
        blocker = QtCore.QSignalBlocker(self.workspaceModelCombo)
        self.workspaceModelCombo.clear()
        for index in range(self.owner.modelCombo.count()):
            self.workspaceModelCombo.addItem(
                self.owner.modelCombo.itemText(index), self.owner.modelCombo.itemData(index)
            )
        index = self.workspaceModelCombo.findData(self.owner.selectedModel())
        if index >= 0:
            self.workspaceModelCombo.setCurrentIndex(index)
        del blocker

    def _refreshLocalizationPanel(self) -> None:
        role = self._selectedRole()
        result = self.owner.localizationResult(role)
        image = self.owner.image(role)
        self.localizationViewer.setData(
            image, result, key=(role, None if image is None else id(image), None if image is None else tuple(image.shape))
        )
        if not self._skipLocalizationFormSync:
            self.localizationForm.setParameters(
                self.owner.localizationParameterSpecs(), self.owner.localizationParameters(role)
            )
        if image is None:
            text = "Image not acquired."
        elif self.owner.localizationError(role):
            text = f"Localization failed: {self.owner.localizationError(role)}"
            if result is not None:
                text += "\nPrevious localization remains displayed for comparison."
        elif result is None:
            text = "Not localized."
        elif self.owner.localizationIsStale(role):
            text = f"{result.summary()}\nParameters changed — rerun localization."
        else:
            details = dict(result.metadata)
            rms = (details.get("diagnostics") or {}).get("fit_rms_px")
            text = result.summary()
            if rms is not None:
                text += f" · grid RMS {float(rms):.3f} px"
        self.localizationStatus.setText(text)
        self.runSelectedButton.setEnabled(image is not None)

    def _refreshTransformPanel(self) -> None:
        self.transformViewer.setData(self.owner)
        if not self._skipTransformFormSync:
            self.transformForm.setParameters(
                self.owner.transformParameterSpecs(), self.owner.transformParameters()
            )
        self.fitButton.setEnabled(self.owner.correspondenceResult is not None)
        transform = self.owner.transform
        if transform is None:
            self.transformStatus.setText(self.owner.statusLabel.text())
            self.matrixView.clear()
        else:
            residuals = transform.residuals
            self.transformStatus.setText(
                "Transform fitted." if residuals is None else residuals.summary()
            )
            matrix = transform.as_matrix()
            self.matrixView.setPlainText(
                "not an affine model -- no matrix"
                if matrix is None
                else "\n".join(
                    "  ".join(f"{value:+10.5f}" for value in row) for row in matrix
                )
            )

    def _localizationSelectionChanged(self, *_args: Any) -> None:
        self._refreshLocalizationPanel()

    def _methodChanged(self, *_args: Any) -> None:
        name = self.methodCombo.currentData()
        if name and str(name) != self.owner.selectedCorrespondence():
            self.owner.setSelectedCorrespondence(str(name))

    def _localizationParametersChanged(self, values: Mapping[str, Any]) -> None:
        try:
            self._skipLocalizationFormSync = True
            self.owner.setLocalizationParameters(dict(values), self._selectedRole())
        except (TypeError, ValueError) as error:
            self.localizationStatus.setText(f"Invalid parameter combination: {error}")
        finally:
            self._skipLocalizationFormSync = False

    def _runSelectedLocalization(self) -> None:
        role = self._selectedRole()
        if self.owner.runLocalizationForRole(role):
            # If the other image is current too, update the correspondence so
            # Transform can be refitted immediately without rerunning it.
            self.owner.buildCorrespondence()

    def _workspaceModelChanged(self, *_args: Any) -> None:
        name = self.workspaceModelCombo.currentData()
        if name and str(name) != self.owner.selectedModel():
            self.owner.setSelectedModel(str(name))

    def _transformParametersChanged(self, values: Mapping[str, Any]) -> None:
        try:
            self._skipTransformFormSync = True
            self.owner.setTransformParameters(dict(values))
        except (TypeError, ValueError) as error:
            self.transformStatus.setText(f"Invalid fit parameters: {error}")
        finally:
            self._skipTransformFormSync = False
