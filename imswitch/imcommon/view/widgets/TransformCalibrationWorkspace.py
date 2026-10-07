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
        self.missing = pg.ScatterPlotItem(symbol="o", size=10, pen=pg.mkPen("#a67c52", width=1.5), brush=None)
        for item in (self.predicted, self.detected, self.refined, self.missing):
            self.plot.addItem(item)
        root.addWidget(self.plot, 1)
        controls = QtWidgets.QHBoxLayout()
        self.detected_check = QtWidgets.QCheckBox("Detected")
        self.predicted_check = QtWidgets.QCheckBox("Predicted grid")
        self.refined_check = QtWidgets.QCheckBox("Refined")
        self.missing_check = QtWidgets.QCheckBox("Missing")
        for check in (self.detected_check, self.predicted_check, self.refined_check, self.missing_check):
            check.setChecked(True)
            controls.addWidget(check)
        controls.addStretch(1)
        root.addLayout(controls)
        self.detected_check.toggled.connect(self.detected.setVisible)
        self.predicted_check.toggled.connect(self.predicted.setVisible)
        self.refined_check.toggled.connect(self.refined.setVisible)
        self.missing_check.toggled.connect(self.missing.setVisible)
        self._image_key = None

    @staticmethod
    def _setPoints(
        item: pg.ScatterPlotItem,
        points: np.ndarray | None,
        *,
        offset_rc: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        if points is None or len(points) == 0:
            item.setData([], [])
            return
        array = np.asarray(points, dtype=np.float64)
        row_offset, col_offset = offset_rc
        item.setData(x=array[:, 1] - col_offset, y=array[:, 0] - row_offset)

    def setData(
        self,
        image: np.ndarray | None,
        result: Any,
        *,
        failure_diagnostics: Any = None,
        key: Any,
        view: str = "localization",
    ) -> None:
        diagnostics = failure_diagnostics
        if diagnostics is None and result is not None:
            diagnostics = result.diagnostics
        metadata = {} if diagnostics is None else dict(diagnostics.metadata or {})
        crop_coord = metadata.get("crop_coord")
        raw_view = str(view) == "raw"

        if image is None:
            self.image_item.setVisible(False)
            for item in (self.detected, self.predicted, self.refined, self.missing):
                self._setPoints(item, None)
            return

        array = np.asarray(image)
        offset = (0.0, 0.0)
        if not raw_view and crop_coord is not None:
            try:
                y0, y1, x0, x1 = (int(value) for value in crop_coord)
                if 0 <= y0 < y1 <= array.shape[0] and 0 <= x0 < x1 <= array.shape[1]:
                    array = array[y0:y1, x0:x1]
                    offset = (float(y0), float(x0))
            except Exception:
                pass

        self.image_item.setVisible(True)
        self.image_item.setImage(array, autoLevels=True)
        if raw_view:
            for item in (self.detected, self.predicted, self.refined, self.missing):
                self._setPoints(item, None)
        else:
            self._setPoints(self.detected, None if diagnostics is None else diagnostics.detected_points, offset_rc=offset)
            self._setPoints(self.predicted, None if diagnostics is None else diagnostics.predicted_points, offset_rc=offset)
            self._setPoints(self.refined, None if diagnostics is None else diagnostics.refined_points, offset_rc=offset)
            self._setPoints(self.missing, None if diagnostics is None else diagnostics.missing_points, offset_rc=offset)

        display_key = (key, str(view), tuple(array.shape), tuple(offset))
        if display_key != self._image_key:
            self._image_key = display_key
            self.plot.autoRange()


class _TransformCorrespondenceViewer(QtWidgets.QWidget):
    """Reference-image view of fitted correspondences and residual vectors."""

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
        self.reference = pg.ScatterPlotItem(
            symbol="o", size=9, pen=pg.mkPen("#55aa55", width=1.4), brush=None
        )
        self.mapped = pg.ScatterPlotItem(
            symbol="x", size=9, pen=pg.mkPen("#4c8bd8", width=1.3)
        )
        self.outliers = pg.ScatterPlotItem(
            symbol="x", size=11, pen=pg.mkPen("#ff9966", width=1.6)
        )
        self.residuals = pg.PlotDataItem(pen=pg.mkPen("#ff6666", width=1.0))
        for item in (
            self.image_item,
            self.residuals,
            self.reference,
            self.mapped,
            self.outliers,
        ):
            self.plot.addItem(item)
        root.addWidget(self.plot, 1)

        controls = QtWidgets.QHBoxLayout()
        self.reference_check = QtWidgets.QCheckBox("Reference ○")
        self.mapped_check = QtWidgets.QCheckBox("Transformed moving ×")
        self.outliers_check = QtWidgets.QCheckBox("Outliers ×")
        self.residuals_check = QtWidgets.QCheckBox("Residuals —")
        for check in (
            self.reference_check,
            self.mapped_check,
            self.outliers_check,
            self.residuals_check,
        ):
            check.setChecked(True)
            controls.addWidget(check)
        controls.addStretch(1)
        root.addLayout(controls)

        self.reference_check.setToolTip(
            "Localized reference-image positions used as transform targets."
        )
        self.mapped_check.setToolTip(
            "Moving-image positions after applying the fitted transform (inliers)."
        )
        self.outliers_check.setToolTip(
            "Transformed Moving positions whose residual exceeds the fit threshold."
        )
        self.residuals_check.setToolTip(
            "Actual residual vector from each reference position to the corresponding "
            "transformed Moving position. Vector lengths are not magnified."
        )
        self.reference_check.toggled.connect(self.reference.setVisible)
        self.mapped_check.toggled.connect(self.mapped.setVisible)
        self.outliers_check.toggled.connect(self.outliers.setVisible)
        self.residuals_check.toggled.connect(self.residuals.setVisible)
        self._key = None

    def setData(self, owner: "TransformCalibrationWidget") -> None:
        image = owner.image("target")
        result = owner.correspondenceResult
        fit = owner.fitResult
        if image is None:
            self.image_item.setVisible(False)
            self.reference.setData([], [])
            self.mapped.setData([], [])
            self.outliers.setData([], [])
            self.residuals.setData([], [])
            return
        self.image_item.setVisible(True)
        self.image_item.setImage(np.asarray(image), autoLevels=True)
        if result is None:
            self.reference.setData([], [])
            self.mapped.setData([], [])
            self.outliers.setData([], [])
            self.residuals.setData([], [])
        else:
            target = np.asarray(result.target_points, dtype=np.float64)
            self.reference.setData(x=target[:, 1], y=target[:, 0])
            if fit is None:
                self.mapped.setData([], [])
                self.outliers.setData([], [])
                self.residuals.setData([], [])
            else:
                mapped = np.asarray(fit.mapped_points, dtype=np.float64)
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


class _TransformFieldViewer(QtWidgets.QWidget):
    """SLMcore-inspired sampled displacement-field view of the fitted transform."""

    DEFAULT_VECTOR_SCALE = 2.0

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._source_points: np.ndarray | None = None
        self._displacements: np.ndarray | None = None
        self._bounds: tuple[float, float, float, float] | None = None
        self._colormap = pg.colormap.get("viridis")

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.setAspectLocked(True)
        self.plot.getViewBox().invertY(True)
        self.plot.hideAxis("left")
        self.plot.hideAxis("bottom")
        self.vectors = pg.PlotDataItem(pen=pg.mkPen("#888888", width=1.0))
        self.origins = pg.ScatterPlotItem(size=7, pen=None)
        self.endpoints = pg.ScatterPlotItem(
            size=4, pen=None, brush=pg.mkBrush("#dddddd")
        )
        for item in (self.vectors, self.origins, self.endpoints):
            self.plot.addItem(item)
        root.addWidget(self.plot, 1)

        controls = QtWidgets.QGridLayout()
        magnitude_title = QtWidgets.QLabel("Color = |displacement|")
        magnitude_title.setToolTip(
            "Color encodes the actual fitted Moving→Reference displacement magnitude "
            "at each sampled Moving-image position."
        )
        controls.addWidget(magnitude_title, 0, 0)
        gradient = QtWidgets.QFrame()
        gradient.setFixedSize(78, 10)
        gradient.setStyleSheet(
            "QFrame {"
            "background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #440154, stop:0.5 #21918c, stop:1 #fde725);"
            "border: 1px solid rgba(160,160,160,90);"
            "}"
        )
        controls.addWidget(gradient, 0, 1)
        self.magnitude_range = QtWidgets.QLabel("0 → — px")
        controls.addWidget(self.magnitude_range, 0, 2)
        vector_legend = QtWidgets.QLabel("Vector: Moving ● → mapped ○")
        vector_legend.setToolTip(
            "Vectors start at sampled Moving-image coordinates. The light endpoint "
            "is the position after applying the fitted transform and the display-only "
            "vector scale."
        )
        controls.addWidget(vector_legend, 0, 3)
        controls.addWidget(QtWidgets.QLabel("Vector scale:"), 0, 4)
        self.vector_scale = QtWidgets.QDoubleSpinBox()
        self.vector_scale.setRange(1.0, 1000.0)
        self.vector_scale.setDecimals(1)
        self.vector_scale.setSingleStep(1.0)
        self.vector_scale.setValue(self.DEFAULT_VECTOR_SCALE)
        self.vector_scale.setSuffix("×")
        self.vector_scale.setToolTip(
            "Display-only magnification of transform displacement vectors. "
            "Color and reported magnitudes remain unscaled."
        )
        self.vector_scale.valueChanged.connect(self._updatePlot)
        controls.addWidget(self.vector_scale, 0, 5)
        self.reset_button = QtWidgets.QPushButton("Reset View")
        self.reset_button.clicked.connect(self.resetView)
        controls.addWidget(self.reset_button, 0, 6)
        controls.setColumnStretch(3, 1)
        self.summary = QtWidgets.QLabel("Transform field unavailable")
        self.summary.setWordWrap(True)
        controls.addWidget(self.summary, 1, 0, 1, 7)
        root.addLayout(controls)
        self.clear()

    def clear(self) -> None:
        self._source_points = None
        self._displacements = None
        self._bounds = None
        self.vectors.setData([], [])
        self.origins.setData([], [])
        self.endpoints.setData([], [])
        self.magnitude_range.setText("0 → — px")
        self.summary.setText("Fit a transform to inspect its displacement field.")
        self.vector_scale.setEnabled(False)
        self.reset_button.setEnabled(False)

    def setData(self, owner: "TransformCalibrationWidget") -> None:
        result = owner.correspondenceResult
        transform = owner.transform
        if transform is None:
            self.clear()
            return
        if result is not None:
            source = np.asarray(result.source_points, dtype=np.float64)
            if source.ndim != 2 or source.shape[1] != 2 or len(source) == 0:
                self.clear()
                return
            low = np.min(source, axis=0)
            high = np.max(source, axis=0)
        else:
            model = getattr(transform, "model", None)
            if hasattr(model, "domain_min") and hasattr(model, "domain_max"):
                low = np.asarray(model.domain_min, dtype=np.float64)
                high = np.asarray(model.domain_max, dtype=np.float64)
            elif transform.source_context.image_shape is not None:
                height, width = tuple(transform.source_context.image_shape)[-2:]
                low = np.array([0.0, 0.0], dtype=np.float64)
                high = np.array([max(0.0, height - 1.0), max(0.0, width - 1.0)])
            else:
                self.clear()
                return
        ys = np.linspace(low[0], high[0], 19) if high[0] > low[0] else np.array([low[0]])
        xs = np.linspace(low[1], high[1], 19) if high[1] > low[1] else np.array([low[1]])
        yy, xx = np.meshgrid(ys, xs, indexing="ij")
        samples = np.column_stack([yy.ravel(), xx.ravel()])
        mapped = np.asarray(transform.map_points(samples), dtype=np.float64)
        self._source_points = samples
        self._displacements = mapped - samples
        self._bounds = (float(low[0]), float(high[0]), float(low[1]), float(high[1]))
        self._updatePlot()
        self.vector_scale.setEnabled(True)
        self.reset_button.setEnabled(True)
        self.resetView()

    def _updatePlot(self, *_args: Any) -> None:
        source = self._source_points
        displacement = self._displacements
        if source is None or displacement is None:
            self.vectors.setData([], [])
            self.origins.setData([], [])
            self.endpoints.setData([], [])
            return
        magnitudes = np.linalg.norm(displacement, axis=1)
        maximum = float(np.max(magnitudes)) if magnitudes.size else 0.0
        normalized = magnitudes / maximum if maximum > 0.0 else np.zeros_like(magnitudes)
        brushes = [pg.mkBrush(color) for color in self._colormap.map(normalized, mode="qcolor")]
        scale = float(self.vector_scale.value())
        endpoints = source + scale * displacement
        count = len(source)
        x = np.empty(2 * count, dtype=np.float64)
        y = np.empty(2 * count, dtype=np.float64)
        x[0::2], x[1::2] = source[:, 1], endpoints[:, 1]
        y[0::2], y[1::2] = source[:, 0], endpoints[:, 0]
        self.vectors.setData(x=x, y=y, connect="pairs")
        self.origins.setData(x=source[:, 1], y=source[:, 0], brush=brushes, pen=None)
        self.endpoints.setData(
            x=endpoints[:, 1], y=endpoints[:, 0], brush=pg.mkBrush("#dddddd"), pen=None
        )
        median = float(np.median(magnitudes)) if magnitudes.size else 0.0
        self.magnitude_range.setText(f"0 → {maximum:.4g} px")
        self.summary.setText(
            f"{count} sampled positions · actual displacement median {median:.4g} px · "
            f"max {maximum:.4g} px · vectors shown at {scale:g}×"
        )

    def resetView(self, *_args: Any) -> None:
        if self._bounds is None:
            return
        y0, y1, x0, x1 = self._bounds
        dy = max(y1 - y0, 1.0)
        dx = max(x1 - x0, 1.0)
        self.plot.setXRange(x0 - 0.05 * dx, x1 + 0.05 * dx, padding=0.0)
        self.plot.setYRange(y0 - 0.05 * dy, y1 + 0.05 * dy, padding=0.0)


class _TransformViewer(QtWidgets.QWidget):
    """Switchable correspondence / transform-field inspection surface."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.stack = QtWidgets.QStackedWidget()
        self.correspondence = _TransformCorrespondenceViewer()
        self.field = _TransformFieldViewer()
        self.stack.addWidget(self.correspondence)
        self.stack.addWidget(self.field)
        root.addWidget(self.stack, 1)

    def setView(self, view: str) -> None:
        self.stack.setCurrentWidget(
            self.field if str(view) == "field" else self.correspondence
        )

    def setData(self, owner: "TransformCalibrationWidget") -> None:
        self.correspondence.setData(owner)
        self.field.setData(owner)



def _formatTransformDetails(transform: Any, fit: Any) -> str:
    """Human-readable transform summary; raw coefficients/matrix remain secondary."""
    estimator = None
    if fit is not None:
        estimator = getattr(fit, "estimator", None)
    if not estimator:
        estimator = dict(getattr(transform, "provenance", {}) or {}).get("estimator")
    estimator = str(estimator or getattr(transform, "kind", "transform"))
    lines = [f"Model: {estimator}"]

    matrix = transform.as_matrix()
    if matrix is not None and np.asarray(matrix).shape == (3, 3):
        matrix = np.asarray(matrix, dtype=np.float64)
        linear = matrix[:2, :2]
        row_axis = linear[:, 0]
        col_axis = linear[:, 1]
        row_scale = float(np.linalg.norm(row_axis))
        col_scale = float(np.linalg.norm(col_axis))
        denom = max(row_scale * col_scale, np.finfo(float).eps)
        axis_angle = float(
            np.degrees(np.arccos(np.clip(np.dot(row_axis, col_axis) / denom, -1.0, 1.0)))
        )
        # Image coordinates use +row downward and +col rightward.  This angle is
        # therefore the clockwise rotation of the mapped +col axis on screen.
        rotation = float(np.degrees(np.arctan2(col_axis[0], col_axis[1])))
        lines.extend(
            [
                f"Translation: row {matrix[0, 2]:+.4f} px · col {matrix[1, 2]:+.4f} px",
                f"Axis scale: row {row_scale:.6f} · col {col_scale:.6f}",
                f"Mapped +col rotation: {rotation:+.4f} deg",
                f"Mapped axis angle: {axis_angle:.4f} deg (90 deg = no shear)",
                f"Linear determinant: {float(np.linalg.det(linear)):+.6f}",
                "",
                "Homogeneous matrix (row, col):",
            ]
        )
        lines.extend(
            "  ".join(f"{value:+10.6f}" for value in row) for row in matrix
        )
        return "\n".join(lines)

    model = getattr(transform, "model", None)
    degree = getattr(model, "degree", None)
    if degree is not None:
        coefficients = np.asarray(getattr(model, "coefficients"), dtype=np.float64)
        domain_min = np.asarray(getattr(model, "domain_min"), dtype=np.float64)
        domain_max = np.asarray(getattr(model, "domain_max"), dtype=np.float64)
        lines.extend(
            [
                f"Degree: {int(degree)} · {coefficients.shape[1]} terms/output",
                "Calibration coverage (Moving coordinates):",
                f"  row {domain_min[0]:.3f} .. {domain_max[0]:.3f} px",
                f"  col {domain_min[1]:.3f} .. {domain_max[1]:.3f} px",
                "",
                "Live Apply: unavailable for polynomial transforms in this version.",
                "Use the Transform field view to inspect the fitted nonlinear mapping.",
            ]
        )
        return "\n".join(lines)

    lines.append("No affine matrix is available for this transform model.")
    return "\n".join(lines)


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
        selector.addSpacing(12)
        selector.addWidget(QtWidgets.QLabel("View:"))
        self.localizationViewCombo = QtWidgets.QComboBox()
        self.localizationViewCombo.addItem("Localization", "localization")
        self.localizationViewCombo.addItem("Raw image", "raw")
        selector.addWidget(self.localizationViewCombo)
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
        action_row = QtWidgets.QHBoxLayout()
        self.runSelectedButton = QtWidgets.QPushButton("Run localization")
        self.searchMissingButton = QtWidgets.QPushButton("Search missing sites")
        action_row.addWidget(self.runSelectedButton)
        action_row.addWidget(self.searchMissingButton)
        right_layout.addLayout(action_row)
        self.localizationStatus = QtWidgets.QLabel()
        self.localizationStatus.setWordWrap(True)
        right_layout.addWidget(self.localizationStatus)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([760, 440])
        root.addWidget(splitter, 1)

        self.imageCombo.currentIndexChanged.connect(self._localizationSelectionChanged)
        self.localizationViewCombo.currentIndexChanged.connect(self._refreshLocalizationPanel)
        self.methodCombo.currentIndexChanged.connect(self._methodChanged)
        self.localizationForm.sigValuesChanged.connect(self._localizationParametersChanged)
        self.runSelectedButton.clicked.connect(self._runSelectedLocalization)
        self.searchMissingButton.clicked.connect(self._searchMissingSites)
        return page

    def _buildTransformTab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(page)
        view_row = QtWidgets.QHBoxLayout()
        view_row.addWidget(QtWidgets.QLabel("View:"))
        self.transformViewCombo = QtWidgets.QComboBox()
        self.transformViewCombo.addItem("Correspondence", "correspondence")
        self.transformViewCombo.addItem("Transform field", "field")
        view_row.addWidget(self.transformViewCombo)
        view_row.addStretch(1)
        root.addLayout(view_row)
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
        self.transformDetailsView = QtWidgets.QPlainTextEdit()
        self.transformDetailsView.setReadOnly(True)
        self.transformDetailsView.setMaximumHeight(230)
        right_layout.addWidget(self.transformDetailsView)
        # Compatibility alias used by a few early workspace tests/callers.
        self.matrixView = self.transformDetailsView
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([760, 440])
        root.addWidget(splitter, 1)

        self.transformViewCombo.currentIndexChanged.connect(self._transformViewChanged)
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
            self.workspaceModelCombo.setItemData(
                self.workspaceModelCombo.count() - 1,
                self.owner.modelCombo.itemData(index, QtCore.Qt.ToolTipRole),
                QtCore.Qt.ToolTipRole,
            )
        index = self.workspaceModelCombo.findData(self.owner.selectedModel())
        if index >= 0:
            self.workspaceModelCombo.setCurrentIndex(index)
        del blocker

    def _refreshLocalizationPanel(self) -> None:
        role = self._selectedRole()
        result = self.owner.localizationResult(role)
        image = self.owner.image(role)
        failure_diagnostics = self.owner.localizationFailureDiagnostics(role)
        self.localizationViewer.setData(
            image,
            result,
            failure_diagnostics=failure_diagnostics,
            key=(
                role,
                None if image is None else id(image),
                None if image is None else tuple(image.shape),
            ),
            view=str(self.localizationViewCombo.currentData() or "localization"),
        )
        if not self._skipLocalizationFormSync:
            self.localizationForm.setParameters(
                self.owner.localizationParameterSpecs(), self.owner.localizationParameters(role)
            )
        if image is None:
            text = "Image not acquired."
        elif self.owner.localizationError(role):
            text = f"Localization failed: {self.owner.localizationError(role)}"
            diagnostics = failure_diagnostics
            if diagnostics is not None:
                detected = diagnostics.metadata.get("detected_count")
                crop_shape = diagnostics.metadata.get("crop_shape")
                details = []
                if detected is not None:
                    details.append(f"detected {int(detected)} spots")
                if crop_shape is not None:
                    details.append(
                        f"crop {int(crop_shape[1])}x{int(crop_shape[0])} px"
                    )
                if details:
                    text += "\n" + " · ".join(details)
            if result is not None:
                text += (
                    "\nPrevious successful localization is stale; "
                    "failed-attempt detections are displayed."
                )
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
        missing = 0 if result is None else int(result.metadata.get("missing_count", 0) or 0)
        strategy = self.owner.correspondenceSpec()
        self.searchMissingButton.setVisible(strategy.supports_missing_recovery)
        self.searchMissingButton.setEnabled(
            image is not None
            and result is not None
            and not self.owner.localizationIsStale(role)
            and missing > 0
        )

    def _refreshTransformPanel(self) -> None:
        self.transformViewer.setView(
            str(self.transformViewCombo.currentData() or "correspondence")
        )
        self.transformViewer.setData(self.owner)
        if not self._skipTransformFormSync:
            self.transformForm.setParameters(
                self.owner.transformParameterSpecs(), self.owner.transformParameters()
            )
        self.fitButton.setEnabled(self.owner.correspondenceResult is not None)
        transform = self.owner.transform
        if transform is None:
            self.transformStatus.setText(self.owner.statusLabel.text())
            self.transformDetailsView.clear()
        else:
            residuals = transform.residuals
            fit = self.owner.fitResult
            if residuals is None:
                self.transformStatus.setText("Transform fitted.")
            else:
                self.transformStatus.setText(
                    f"{residuals.n_points} pairs · {residuals.n_inliers} inliers · "
                    f"RMS {residuals.rms:.3f} {residuals.unit} · "
                    f"median {residuals.median:.3f} {residuals.unit} · "
                    f"max {residuals.max:.3f} {residuals.unit}"
                )
            self.transformDetailsView.setPlainText(_formatTransformDetails(transform, fit))

    def _localizationSelectionChanged(self, *_args: Any) -> None:
        self._refreshLocalizationPanel()

    def _transformViewChanged(self, *_args: Any) -> None:
        self._refreshTransformPanel()

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

    def _searchMissingSites(self) -> None:
        self.owner.recoverMissingForRole(self._selectedRole())

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
