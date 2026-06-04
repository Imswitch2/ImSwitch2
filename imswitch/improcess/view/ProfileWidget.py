"""Interactive line and rectangle profiles for ImProcess results."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets
from scipy.ndimage import map_coordinates
from scipy.optimize import curve_fit

from imswitch.imcommon.view.guitools.naparitools import ViewerToolManager


@dataclass
class FitResult:
    name: str
    x: np.ndarray
    y: np.ndarray
    summary: str


class ProfileFit:
    """Base contract for profile fit backends."""

    id = "none"
    label = "No fit"

    def fit(self, x: np.ndarray, y: np.ndarray) -> FitResult | None:
        return None


class GaussianFit(ProfileFit):
    """Single Gaussian plus constant offset."""

    id = "gaussian"
    label = "Gaussian"

    @staticmethod
    def _model(x, offset, amplitude, center, sigma):
        return offset + amplitude * np.exp(-((x - center) ** 2) / (2 * sigma ** 2))

    def fit(self, x: np.ndarray, y: np.ndarray) -> FitResult | None:
        finite = np.isfinite(x) & np.isfinite(y)
        x = np.asarray(x[finite], dtype=float)
        y = np.asarray(y[finite], dtype=float)
        if x.size < 4:
            return None

        offset0 = float(np.nanmin(y))
        amplitude0 = float(np.nanmax(y) - offset0)
        if amplitude0 <= 0:
            return None
        center0 = float(x[np.nanargmax(y)])
        sigma0 = max(float((x.max() - x.min()) / 6.0), 1.0)

        try:
            popt, _ = curve_fit(
                self._model,
                x,
                y,
                p0=(offset0, amplitude0, center0, sigma0),
                bounds=(
                    [-np.inf, 0.0, float(x.min()), 1e-6],
                    [np.inf, np.inf, float(x.max()), np.inf],
                ),
                maxfev=10000,
            )
        except Exception:
            return None

        xx = np.linspace(float(x.min()), float(x.max()), max(200, x.size))
        yy = self._model(xx, *popt)
        _, amplitude, center, sigma = popt
        fwhm = 2.354820045 * abs(float(sigma))
        summary = (
            f"A={amplitude:.4g}, center={center:.4g}, "
            f"sigma={abs(float(sigma)):.4g}, FWHM={fwhm:.4g}"
        )
        return FitResult(self.label, xx, yy, summary)


class ProfileWidget(QtWidgets.QWidget):
    """Draw line/rectangle ROIs on a napari viewer and plot their profiles."""

    def __init__(self, napariViewer, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._toolManager = ViewerToolManager(napariViewer)
        self._fitters = {fit.id: fit for fit in (ProfileFit(), GaussianFit())}
        self._last_kind = None
        self._last_payload: list[tuple[str, np.ndarray, np.ndarray]] = []

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

        if endpoints is None:
            self.fitSummary.setText("")
            return
        image = self._currentImage2D()
        if image is None:
            self.fitSummary.setText("No image layer selected.")
            return

        (r0, c0), (r1, c1) = endpoints
        profile = self._computeLineProfile(image, r0, c0, r1, c1, self.widthSpinBox.value())
        if profile is None:
            self.fitSummary.setText("")
            return

        row_scale, col_scale = self._visiblePixelScales()
        length = float(np.hypot((r1 - r0) * row_scale, (c1 - c0) * col_scale))
        x = np.linspace(0.0, length, profile.size)
        self.plot.plot(x, profile, pen=pg.mkPen("r", width=2), name="line")
        self._last_payload = [("line", x, profile)]
        self._applyFits()

    def _plotRectangleProfiles(self, bounds):
        self._last_kind = "rectangle"
        self.plot.clear()
        self.plot.setTitle("Rectangle Projections")
        self.plot.setLabel("bottom", f"Distance ({self._distanceUnit()})")
        self.plot.setLabel("left", "Mean intensity")
        self._last_payload = []

        if bounds is None:
            self.fitSummary.setText("")
            return
        image = self._currentImage2D()
        if image is None:
            self.fitSummary.setText("No image layer selected.")
            return

        r0, c0, r1, c1 = bounds
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
        row_scale, col_scale = self._visiblePixelScales()
        x = np.arange(x_profile.size) * col_scale
        y = np.arange(y_profile.size) * row_scale
        self.plot.plot(x, x_profile, pen=pg.mkPen("r", width=2), name="x")
        self.plot.plot(y, y_profile, pen=pg.mkPen("#00cc44", width=2), name="y")
        self._last_payload = [("x", x, x_profile), ("y", y, y_profile)]
        self._applyFits()

    def _applyFits(self):
        fit_id = self.fitCombo.currentData()
        fitter = self._fitters.get(fit_id)
        if fitter is None or fitter.id == "none":
            self.fitSummary.setText("")
            return

        summaries = []
        for index, (name, x, y) in enumerate(self._last_payload):
            result = fitter.fit(x, y)
            if result is None:
                summaries.append(f"{name}: fit failed")
                continue
            pen = pg.mkPen(pg.intColor(index + 3), width=2, style=QtCore.Qt.DashLine)
            self.plot.plot(result.x, result.y, pen=pen, name=f"{name} {result.name}")
            summaries.append(f"{name}: {result.summary}")
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
        try:
            active = self._viewer.layers.selection.active
        except Exception:
            active = None
        if self._isImageLayer(active):
            return active
        for layer in self._viewer.layers:
            if self._isImageLayer(layer):
                return layer
        return None

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

    @staticmethod
    def _isImageLayer(layer):
        return (
            layer is not None
            and hasattr(layer, "data")
            and getattr(layer, "visible", True)
            and isinstance(layer.data, np.ndarray)
            and layer.data.ndim >= 2
            and not str(getattr(layer, "name", "")).startswith("_")
            and getattr(layer, "name", "") != "Viewer Tools"
        )

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
