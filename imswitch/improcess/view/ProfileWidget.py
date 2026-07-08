"""Interactive line and rectangle profiles for ImProcess results."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets
from scipy.ndimage import map_coordinates

from imswitch.imcommon.view.guitools.naparitools import ViewerToolManager
from imswitch.improcess.layer_selection import active_image_layer
from imswitch.improcess.profile_helpers import (
    ProfileFit,
    GaussianFit,
    TwoGaussianFit,
    ExponentialFit,
    build_profile_record,
)


class ProfileWidget(QtWidgets.QWidget):
    """Draw line/rectangle ROIs on a napari viewer and plot their profiles."""

    sigResultPushed = QtCore.Signal(object, object)

    def __init__(self, napariViewer, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._toolManager = ViewerToolManager(napariViewer)
        self._fitters = {
            fit.id: fit
            for fit in (
                ProfileFit(),
                GaussianFit(),
                TwoGaussianFit(),
                ExponentialFit(),
            )
        }
        self._last_kind = None
        self._last_payload: list[tuple[str, np.ndarray, np.ndarray]] = []
        self._current_record_inputs = []

        self.modeButtons = QtWidgets.QButtonGroup(self)
        self.panButton = self._makeModeButton("Pan", "pan", checked=True)
        self.lineButton = self._makeModeButton("Line", "line")
        self.rectangleButton = self._makeModeButton("Rectangle", "rectangle")
        self.clearButton = QtWidgets.QPushButton("Clear")

        self.widthSpinBox = QtWidgets.QSpinBox()
        self.widthSpinBox.setRange(1, 99)
        self.widthSpinBox.setSingleStep(2)
        self.widthSpinBox.setValue(1)
        self.widthSpinBox.setToolTip("Perpendicular samples averaged for line profiles.")

        self.fitCombo = QtWidgets.QComboBox()
        for fit in self._fitters.values():
            self.fitCombo.addItem(fit.label, fit.id)

        self.pushButton = QtWidgets.QPushButton("Push to table")
        self.saveButton = QtWidgets.QPushButton("Save CSV...")

        self.fitSummary = QtWidgets.QLabel("")
        self.fitSummary.setWordWrap(True)
        self.fitSummary.setStyleSheet("color:#888; font-size:8pt;")

        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.addLegend()

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.addWidget(self.panButton)
        toolbar.addWidget(self.lineButton)
        toolbar.addWidget(self.rectangleButton)
        toolbar.addWidget(self.clearButton)
        toolbar.addSpacing(8)
        toolbar.addWidget(QtWidgets.QLabel("Width"))
        toolbar.addWidget(self.widthSpinBox)
        toolbar.addSpacing(8)
        toolbar.addWidget(QtWidgets.QLabel("Fit"))
        toolbar.addWidget(self.fitCombo)
        toolbar.addSpacing(8)
        toolbar.addWidget(self.pushButton)
        toolbar.addWidget(self.saveButton)
        toolbar.addStretch()

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(toolbar)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.fitSummary)
        self.setLayout(layout)

        self.modeButtons.buttonClicked.connect(self._modeChanged)
        self.clearButton.clicked.connect(self._clearShapes)
        self.widthSpinBox.valueChanged.connect(self._refresh)
        self.fitCombo.currentIndexChanged.connect(self._refresh)
        self.pushButton.clicked.connect(self._onPushToTable)
        self.saveButton.clicked.connect(self._onSaveCSV)
        self._toolManager.sigShapesChanged.connect(self._shapesChanged)
        try:
            self._viewer.dims.events.current_step.connect(lambda _event: self._refresh())
        except Exception:
            pass

        self._drawEmpty()

    def _makeModeButton(self, text: str, mode: str, checked: bool = False):
        button = QtWidgets.QPushButton(text)
        button.setCheckable(True)
        button.setProperty("profileMode", mode)
        button.setChecked(checked)
        self.modeButtons.addButton(button)
        return button

    def _modeChanged(self, button):
        mode = button.property("profileMode")
        self._toolManager.clear_shapes()
        self._toolManager.set_mode(mode)
        self._drawEmpty()

    def _clearShapes(self):
        self._toolManager.clear_shapes()
        self._drawEmpty()

    def _shapesChanged(self):
        mode = self._toolManager.get_mode()
        if mode == "line":
            self._plotLineProfile(self._findFirstShape("line"))
        elif mode == "rectangle":
            self._plotRectangleProfiles(self._findFirstShape("rectangle"))

    def _findFirstShape(self, shape_type: str):
        for index, stype in enumerate(self._toolManager.get_shape_types()):
            if stype == shape_type:
                if shape_type == "line":
                    return self._toolManager.get_line_endpoints(index)
                if shape_type == "rectangle":
                    return self._toolManager.get_rectangle_bounds(index)
        return None

    def _refresh(self):
        if self._last_kind == "line":
            self._plotLineProfile(self._findFirstShape("line"))
        elif self._last_kind == "rectangle":
            self._plotRectangleProfiles(self._findFirstShape("rectangle"))

    def _drawEmpty(self):
        self._last_kind = None
        self._last_payload = []
        self._current_record_inputs = []
        self.fitSummary.setText("")
        self.plot.clear()
        self.plot.setTitle("Draw a line or rectangle on the reconstruction view")
        self.plot.setLabel("bottom", f"Distance ({self._distanceUnit()})")
        self.plot.setLabel("left", "Intensity")

    def _plotLineProfile(self, endpoints):
        self._last_kind = "line"
        self.plot.clear()
        self.plot.setTitle("Line Profile")
        self.plot.setLabel("bottom", f"Distance ({self._distanceUnit()})")
        self.plot.setLabel("left", "Intensity")
        self._last_payload = []
        self._current_record_inputs = []

        if endpoints is None:
            self.fitSummary.setText("")
            return
        image = self._currentImage2D()
        if image is None:
            self.fitSummary.setText("No image layer selected.")
            return

        (wr0, wc0), (wr1, wc1) = endpoints
        # ROI vertices come from the Shapes layer in world coordinates, while the
        # image layer carries the physical scale (e.g. nm/px). Divide by the
        # scale to get pixel indices for sampling — otherwise, once the recon
        # scale stops being ~1, the endpoints land outside the array and the
        # profile reads all zeros ("no signal").
        row_scale, col_scale = self._visiblePixelScales()
        r0, c0 = wr0 / row_scale, wc0 / col_scale
        r1, c1 = wr1 / row_scale, wc1 / col_scale
        profile = self._computeLineProfile(image, r0, c0, r1, c1, self.widthSpinBox.value())
        if profile is None:
            self.fitSummary.setText("")
            return

        length_px = float(np.hypot(r1 - r0, c1 - c0))
        length_scaled = float(np.hypot((r1 - r0) * row_scale, (c1 - c0) * col_scale))
        x = np.linspace(0.0, length_scaled, profile.size)
        self.plot.plot(x, profile, pen=pg.mkPen("r", width=2), name="line")
        self._last_payload = [("line", x, profile)]
        
        unit = self._distanceUnit()
        self._current_record_inputs = [("line", x, profile, length_px, length_scaled, unit)]
        self._applyFits()

    def _plotRectangleProfiles(self, bounds):
        self._last_kind = "rectangle"
        self.plot.clear()
        self.plot.setTitle("Rectangle Projections")
        self.plot.setLabel("bottom", f"Distance ({self._distanceUnit()})")
        self.plot.setLabel("left", "Mean intensity")
        self._last_payload = []
        self._current_record_inputs = []

        if bounds is None:
            self.fitSummary.setText("")
            return
        image = self._currentImage2D()
        if image is None:
            self.fitSummary.setText("No image layer selected.")
            return

        r0, c0, r1, c1 = bounds
        # Bounds are world coordinates from the Shapes layer; convert to pixel
        # indices via the image scale before cropping (see _plotLineProfile).
        row_scale, col_scale = self._visiblePixelScales()
        r0, r1 = r0 / row_scale, r1 / row_scale
        c0, c1 = c0 / col_scale, c1 / col_scale
        rlo, rhi = sorted((int(round(r0)), int(round(r1))))
        clo, chi = sorted((int(round(c0)), int(round(c1))))
        h, w = image.shape
        rlo, rhi = max(0, rlo), min(h, rhi)
        clo, chi = max(0, clo), min(w, chi)
        if rlo >= rhi or clo >= chi:
            self.fitSummary.setText("")
            return

        roi = np.asarray(image[rlo:rhi, clo:chi], dtype=float)
        x_profile = roi.mean(axis=0)
        y_profile = roi.mean(axis=1)
        x = np.arange(x_profile.size) * col_scale
        y = np.arange(y_profile.size) * row_scale
        self.plot.plot(x, x_profile, pen=pg.mkPen("r", width=2), name="x")
        self.plot.plot(y, y_profile, pen=pg.mkPen("#00cc44", width=2), name="y")
        self._last_payload = [("x", x, x_profile), ("y", y, y_profile)]
        
        unit = self._distanceUnit()
        x_length_px = float(chi - clo)
        x_length_scaled = float(x_profile.size * col_scale)
        y_length_px = float(rhi - rlo)
        y_length_scaled = float(y_profile.size * row_scale)
        self._current_record_inputs = [
            ("rectangle-x", x, x_profile, x_length_px, x_length_scaled, unit),
            ("rectangle-y", y, y_profile, y_length_px, y_length_scaled, unit)
        ]
        self._applyFits()

    def _applyFits(self):
        fit_id = self.fitCombo.currentData()
        fitter = self._fitters.get(fit_id)
        if fitter is None or fitter.id == "none":
            self.fitSummary.setText("")
            updated_inputs = []
            for rec_input in self._current_record_inputs:
                if len(rec_input) == 6:
                    updated_inputs.append(rec_input + (None,))
                else:
                    updated_inputs.append((rec_input[0], rec_input[1], rec_input[2], 
                                         rec_input[3], rec_input[4], rec_input[5], None))
            self._current_record_inputs = updated_inputs
            return

        summaries = []
        updated_inputs = []
        for index, (name, x, y) in enumerate(self._last_payload):
            result = fitter.fit(x, y)
            if result is None:
                summaries.append(f"{name}: fit failed")
                if index < len(self._current_record_inputs):
                    rec_input = self._current_record_inputs[index]
                    if len(rec_input) == 6:
                        updated_inputs.append(rec_input + (None,))
                    else:
                        updated_inputs.append((rec_input[0], rec_input[1], rec_input[2], 
                                             rec_input[3], rec_input[4], rec_input[5], None))
                continue
            pen = pg.mkPen(pg.intColor(index + 3), width=2, style=QtCore.Qt.DashLine)
            self.plot.plot(result.x, result.y, pen=pen, name=f"{name} {result.name}")
            summaries.append(f"{name}: {result.summary}")
            if index < len(self._current_record_inputs):
                rec_input = self._current_record_inputs[index]
                if len(rec_input) == 6:
                    updated_inputs.append(rec_input + (result.metrics,))
                else:
                    updated_inputs.append((rec_input[0], rec_input[1], rec_input[2], 
                                         rec_input[3], rec_input[4], rec_input[5], result.metrics))
        self._current_record_inputs = updated_inputs
        self.fitSummary.setText(" | ".join(summaries))

    def _currentImage2D(self):
        layer = self._activeImageLayer()
        if layer is None:
            return None
        data = np.asarray(layer.data)
        if data.ndim < 2:
            return None
        if data.ndim == 2:
            return data
        try:
            step = tuple(int(v) for v in self._viewer.dims.current_step)
        except Exception:
            step = tuple(0 for _ in range(data.ndim))
        leading = []
        for axis in range(data.ndim - 2):
            leading.append(min(max(step[axis], 0), data.shape[axis] - 1))
        return data[tuple(leading)]

    def _activeImageLayer(self):
        return active_image_layer(self._viewer)

    def _visiblePixelScales(self) -> tuple[float, float]:
        layer = self._activeImageLayer()
        if layer is None:
            return 1.0, 1.0
        try:
            scale = tuple(float(v) for v in layer.scale)
        except Exception:
            return 1.0, 1.0
        if len(scale) < 2:
            return 1.0, 1.0
        return scale[-2], scale[-1]

    def _distanceUnit(self) -> str:
        layer = self._activeImageLayer()
        try:
            unit = layer.metadata.get("scale_unit", "px")
        except Exception:
            unit = "px"
        if unit == "um":
            return "µm"
        return str(unit or "px")

    def _onPushToTable(self):
        if not self._current_record_inputs:
            return
        from imswitch.improcess.view.ResultsTableWidget import merge_columns
        
        records = []
        for rec_input in self._current_record_inputs:
            if len(rec_input) == 7:
                kind, x, y, length_px, length_scaled, unit, fit_metrics = rec_input
            else:
                kind, x, y, length_px, length_scaled, unit = rec_input
                fit_metrics = None
            record = build_profile_record(kind, x, y, 
                                        length_px=length_px, 
                                        length_scaled=length_scaled, 
                                        unit=unit, 
                                        fit_metrics=fit_metrics)
            records.append(record)
        
        columns = []
        for record in records:
            columns = merge_columns(columns, record.keys())
        
        self.sigResultPushed.emit(columns, records)

    def _onSaveCSV(self):
        if not self._current_record_inputs:
            return
        from imswitch.improcess.view.ResultsTableWidget import merge_columns, records_to_csv
        
        records = []
        for rec_input in self._current_record_inputs:
            if len(rec_input) == 7:
                kind, x, y, length_px, length_scaled, unit, fit_metrics = rec_input
            else:
                kind, x, y, length_px, length_scaled, unit = rec_input
                fit_metrics = None
            record = build_profile_record(kind, x, y, 
                                        length_px=length_px, 
                                        length_scaled=length_scaled, 
                                        unit=unit, 
                                        fit_metrics=fit_metrics)
            records.append(record)
        
        columns = []
        for record in records:
            columns = merge_columns(columns, record.keys())
        
        filepath, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save CSV", "", "CSV (*.csv)"
        )
        if not filepath:
            return
        
        csv_text = records_to_csv(columns, records)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(csv_text)
        except Exception as e:
            self.fitSummary.setText(f"Error saving CSV: {e}")

    @staticmethod
    def _computeLineProfile(image, r0, c0, r1, c1, width=1):
        try:
            num_points = max(int(np.ceil(np.hypot(r1 - r0, c1 - c0))) + 1, 2)
            r_samples = np.linspace(r0, r1, num_points)
            c_samples = np.linspace(c0, c1, num_points)
            if width <= 1:
                return map_coordinates(
                    image,
                    [r_samples, c_samples],
                    order=1,
                    mode="constant",
                    cval=0,
                )

            drow = r1 - r0
            dcol = c1 - c0
            length = np.hypot(drow, dcol)
            if length <= 0:
                return None
            perp_r = -dcol / length
            perp_c = drow / length
            offsets = np.linspace(-(width - 1) / 2, (width - 1) / 2, width)
            sigma = max(width / 4.0, 1e-6)
            weights = np.exp(-(offsets ** 2) / (2 * sigma ** 2))
            weights /= weights.sum()

            profile = np.zeros(num_points, dtype=float)
            for weight, offset in zip(weights, offsets):
                profile += weight * map_coordinates(
                    image,
                    [r_samples + offset * perp_r, c_samples + offset * perp_c],
                    order=1,
                    mode="constant",
                    cval=0,
                )
            return profile
        except Exception:
            return None
