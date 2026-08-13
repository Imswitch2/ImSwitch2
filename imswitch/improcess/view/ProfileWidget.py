"""Interactive line and rectangle profiles for ImProcess results."""

from __future__ import annotations

import numpy as np

from imswitch.imcommon.algorithms.line_sampling import line_samples
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view.guitools.viewer_tools import ViewerToolService
from imswitch.improcess.layer_selection import active_image_layer
from imswitch.improcess.profile_helpers import (
    ProfileFit,
    GaussianFit,
    TwoGaussianFit,
    ExponentialFit,
    build_profile_record,
)
from imswitch.improcess.model.plotting import (
    PlotPayload,
    PlotSeries,
    build_delta_x_record,
)


def _roi_is_area(roi) -> bool:
    """True for a shape with an interior. A line has only its own profile."""
    from imswitch.imcommon.algorithms.roi_geometry import roi_capabilities

    try:
        return bool(roi_capabilities(roi.roi_type).is_area)
    except Exception:
        return False


class ProfileWidget(QtWidgets.QWidget):
    """Draw line/rectangle ROIs on a napari viewer and plot their profiles."""

    sigResultPushed = QtCore.Signal(object, object)
    sigPlotPushed = QtCore.Signal(object)
    """One PlotPayload sent to the Graph panel, to sit alongside others."""

    #: Stable owner key for the shared drawing tool (see ViewerToolService).
    TOOL_OWNER = "improcess.profile"

    def __init__(self, napariViewer, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._toolService = ViewerToolService.for_viewer(napariViewer)
        # Register only; the tool is acquired when the user picks a mode.
        self._toolToken = self._toolService.register(self.TOOL_OWNER)
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
        self._mode = "pan"
        #: What the current plot is called and how its axes are
        #: labelled, so a pushed payload describes the same thing the
        #: panel is showing.
        self._last_plot_title = "profile"
        self._last_plot_labels = ("Distance", "Intensity")
        self._last_payload: list[tuple[str, np.ndarray, np.ndarray]] = []
        #: Fitted curves drawn over the profile, kept so a pushed plot
        #: carries the fit the user is actually looking at.
        self._last_fit_curves: list[tuple[str, np.ndarray, np.ndarray]] = []
        self._current_record_inputs = []
        self._measurementRegion: pg.LinearRegionItem | None = None
        self._measurementValues: tuple[float, float] | None = None
        #: Late-bound by the main view; see ImProcessMainView's ROI wiring.
        self._roiManagerWidget = None
        self._rois: list = []

        # Profile something already measured, not only something drawn now.
        # The panel's own shapes are transient scratch; an ROI in the manager
        # is named, saved and re-measurable across reconstructions, and that
        # is what a profile is usually wanted for.
        self.sourceCombo = QtWidgets.QComboBox()
        self.sourceCombo.addItem(self.DRAWN, None)
        self.sourceCombo.setToolTip(
            "Profile the shape drawn here, or a named ROI from the ROI manager"
        )
        self.roiPlotLabel = QtWidgets.QLabel("Plot")
        self.roiPlotCombo = QtWidgets.QComboBox()
        for label, kind in self.AREA_PLOTS:
            self.roiPlotCombo.addItem(label, kind)
        self.roiPlotCombo.setToolTip(
            "What to plot for an ROI with an interior: intensity round its "
            "outline, or the mean along each axis over its own pixels"
        )
        self.roiPlotLabel.setVisible(False)
        self.roiPlotCombo.setVisible(False)

        self.modeButtons = QtWidgets.QButtonGroup(self)
        self.panButton = self._makeModeButton("Pan", "pan", checked=True)
        self.lineButton = self._makeModeButton("Line", "line")
        self.rectangleButton = self._makeModeButton("Rectangle", "rectangle")
        self.zProfileButton = self._makeModeButton("Z profile", "zprofile")
        self.zProfileButton.setToolTip(
            "Plot mean intensity through the stack axis over a rectangle "
            "(the whole frame when none is drawn) — ImageJ's Plot Z-axis Profile"
        )
        self.clearButton = QtWidgets.QPushButton("Clear")
        self.measureButton = QtWidgets.QPushButton("Measure Δx")
        self.measureButton.setCheckable(True)
        self.measureButton.setToolTip(
            "Show two draggable vertical markers and measure their horizontal distance"
        )

        self.widthSpinBox = QtWidgets.QSpinBox()
        self.widthSpinBox.setRange(1, 99)
        self.widthSpinBox.setSingleStep(2)
        self.widthSpinBox.setValue(1)
        self.widthSpinBox.setToolTip("Perpendicular samples averaged for line profiles.")

        self.fitCombo = QtWidgets.QComboBox()
        for fit in self._fitters.values():
            self.fitCombo.addItem(fit.label, fit.id)

        self.pushButton = QtWidgets.QPushButton("Push to table")
        self.pushGraphButton = QtWidgets.QPushButton("Push to graph")
        self.pushGraphButton.setToolTip(
            "Send this profile (and its fit) to the Graph panel, where it "
            "stays put — push a second one to compare two reconstructions"
        )
        self.pushGraphButton.setEnabled(False)
        self.saveButton = QtWidgets.QPushButton("Save CSV...")

        self.fitSummary = QtWidgets.QLabel("")
        self.fitSummary.setWordWrap(True)
        self.fitSummary.setStyleSheet("color:#888; font-size:8pt;")
        self.measurementSummary = QtWidgets.QLabel("")
        self.measurementSummary.setWordWrap(True)
        self.measurementSummary.setStyleSheet("color:#b8860b; font-size:8pt;")

        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.addLegend()

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.addWidget(QtWidgets.QLabel("Source"))
        toolbar.addWidget(self.sourceCombo)
        toolbar.addWidget(self.roiPlotLabel)
        toolbar.addWidget(self.roiPlotCombo)
        toolbar.addSpacing(8)
        toolbar.addWidget(self.panButton)
        toolbar.addWidget(self.lineButton)
        toolbar.addWidget(self.rectangleButton)
        toolbar.addWidget(self.zProfileButton)
        toolbar.addWidget(self.clearButton)
        toolbar.addWidget(self.measureButton)
        toolbar.addSpacing(8)
        toolbar.addWidget(QtWidgets.QLabel("Width"))
        toolbar.addWidget(self.widthSpinBox)
        toolbar.addSpacing(8)
        toolbar.addWidget(QtWidgets.QLabel("Fit"))
        toolbar.addWidget(self.fitCombo)
        toolbar.addSpacing(8)
        toolbar.addWidget(self.pushButton)
        toolbar.addWidget(self.pushGraphButton)
        toolbar.addWidget(self.saveButton)
        toolbar.addStretch()

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(toolbar)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.measurementSummary)
        layout.addWidget(self.fitSummary)
        self.setLayout(layout)

        self.sourceCombo.currentIndexChanged.connect(self._profileSourceChanged)
        self.roiPlotCombo.currentIndexChanged.connect(self._profileSourceChanged)
        self.modeButtons.buttonClicked.connect(self._modeChanged)
        self.clearButton.clicked.connect(self._clearShapes)
        self.measureButton.toggled.connect(self._measurementToggled)
        self.widthSpinBox.valueChanged.connect(self._refresh)
        self.fitCombo.currentIndexChanged.connect(self._refresh)
        self.pushButton.clicked.connect(self._onPushToTable)
        self.pushGraphButton.clicked.connect(self._onPushToGraph)
        self.saveButton.clicked.connect(self._onSaveCSV)
        self._toolService.on_shapes_changed(self._toolToken, self._shapesChanged)
        try:
            # Through the broker so release() tears this down too; connecting
            # straight to the viewer left the callback firing after close.
            self._toolService.on_viewer_event(
                self._toolToken,
                self._viewer.dims.events.current_step,
                lambda _event=None: self._refresh(),
            )
        except Exception:
            pass

        self._drawEmpty()

    def setCurrentResult(self, result) -> None:
        """Recompute the profile against the newly selected result.

        The ROI is drawn in the viewer and the pixels are read from whatever
        image layer is active, so switching reconstruction changes the answer
        — but nothing here notices a result change on its own, and a profile
        left over from the previous result looks exactly like a valid one.
        """
        self.refreshProfileSources()
        self._refresh()

    def showEvent(self, event):  # noqa: N802 - Qt naming
        # The ROI manager is bound after the panels are built, so a chooser
        # populated only in __init__ would stay empty until something else
        # refreshed it.
        self.refreshProfileSources()
        super().showEvent(event)

    def _setPlotLabels(self, title: str, x_label: str, y_label: str) -> None:
        """Title and label the plot, remembering both for pushed payloads."""
        self._last_plot_title = title
        self._last_plot_labels = (x_label, y_label)
        self.plot.setTitle(title[:1].upper() + title[1:])
        self.plot.setLabel("bottom", x_label)
        self.plot.setLabel("left", y_label)

    def _makeModeButton(self, text: str, mode: str, checked: bool = False):
        button = QtWidgets.QPushButton(text)
        button.setCheckable(True)
        button.setProperty("profileMode", mode)
        button.setChecked(checked)
        self.modeButtons.addButton(button)
        return button

    def _modeChanged(self, button):
        mode = button.property("profileMode")
        self._mode = mode
        # Re-acquire so shapes drawn from here on are attributed to this panel,
        # and clear only ours — this used to wipe the shared layer, taking the
        # ROI statistics panel's rectangle with it.
        self._toolToken = self._toolService.acquire(self.TOOL_OWNER)
        self._toolService.clear(self._toolToken)
        # A Z profile is measured over a rectangle, so it draws with the same
        # tool; only what gets plotted differs.
        self._toolService.set_mode(
            self._toolToken, "rectangle" if mode == "zprofile" else mode
        )
        self._drawEmpty()
        if mode == "zprofile":
            # Unlike the in-plane profiles this one is meaningful with no ROI
            # at all (the whole frame), as it is in ImageJ.
            self._plotZProfile(None)

    def _clearShapes(self):
        self._toolService.clear(self._toolToken)
        self._drawEmpty()
        if self._mode == "zprofile":
            self._plotZProfile(None)

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        """Release the drawing tool and our viewer callbacks on close."""
        try:
            self._toolService.release(self._toolToken)
        except Exception:
            pass
        super().closeEvent(event)

    def _shapesChanged(self):
        if self._mode == "zprofile":
            self._plotZProfile(self._findFirstShape("rectangle"))
            return
        mode = self._toolService.get_mode()
        if mode == "line":
            self._plotLineProfile(self._findFirstShape("line"))
        elif mode == "rectangle":
            self._plotRectangleProfiles(self._findFirstShape("rectangle"))

    def _findFirstShape(self, shape_type: str):
        for index, stype, _vertices in self._toolService.shapes(self._toolToken):
            if stype == shape_type:
                if shape_type == "line":
                    return self._toolService.get_line_endpoints(index)
                if shape_type == "rectangle":
                    return self._toolService.get_rectangle_bounds(index)
        return None

    def _refresh(self):
        roi = self.selectedROI()
        if roi is not None:
            self._plotROIProfile(roi)
            return
        if self._last_kind == "line":
            self._plotLineProfile(self._findFirstShape("line"))
        elif self._last_kind == "rectangle":
            self._plotRectangleProfiles(self._findFirstShape("rectangle"))
        elif self._last_kind == "zprofile":
            self._plotZProfile(self._findFirstShape("rectangle"))

    def _drawEmpty(self):
        self.measureButton.setChecked(False)
        self.measureButton.setEnabled(False)
        self._removeMeasurementRegion(clear_values=True)
        self._last_kind = None
        self._last_payload = []
        self._last_fit_curves = []
        self._current_record_inputs = []
        self.fitSummary.setText("")
        self.plot.clear()
        self._setPlotLabels(
            "Draw a line or rectangle on the reconstruction view",
            f"Distance ({self._distanceUnit()})",
            "Intensity",
        )

    def _plotLineProfile(self, endpoints):
        self._last_kind = "line"
        self._removeMeasurementRegion(clear_values=False)
        self.plot.clear()
        self._setPlotLabels(
            "line profile", f"Distance ({self._distanceUnit()})", "Intensity"
        )
        self._last_payload = []
        self._last_fit_curves = []
        self._current_record_inputs = []

        if endpoints is None:
            self._setMeasurementAvailable(False)
            self.fitSummary.setText("")
            return
        image = self._currentImage2D()
        if image is None:
            self._setMeasurementAvailable(False)
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
            self._setMeasurementAvailable(False)
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
        self._setMeasurementAvailable(True)

    # -- profiling an ROI from the ROI manager ----------------------------

    #: What can be plotted for an ROI that encloses an area. A line has only
    #: one answer and does not offer a choice.
    #: The entry meaning "whatever is drawn on the layer" -- the panel's
    #: original and default behaviour.
    DRAWN = "Drawn"

    AREA_PLOTS = (
        ("Outline", "outline"),
        ("Mean along X", "mean-x"),
        ("Mean along Y", "mean-y"),
    )

    def refreshProfileSources(self) -> None:
        """Re-offer the ROI manager's visible ROIs beside Drawn."""
        rois = []
        panel = getattr(self, "_roiManagerWidget", None)
        if panel is not None:
            try:
                rois = [roi for roi in panel.rois() if roi.visible]
            except Exception:
                rois = []
        self._rois = rois

        current = self.sourceCombo.currentData()
        self.sourceCombo.blockSignals(True)
        self.sourceCombo.clear()
        self.sourceCombo.addItem(self.DRAWN, None)
        for roi in rois:
            self.sourceCombo.addItem(f"{roi.name} ({roi.roi_type})", roi.uid)
        if current is not None:
            self.sourceCombo.setCurrentIndex(max(0, self.sourceCombo.findData(current)))
        self.sourceCombo.blockSignals(False)

    def selectedROI(self):
        uid = self.sourceCombo.currentData()
        if uid is None:
            return None
        return next((roi for roi in self._rois if roi.uid == uid), None)

    def _profileSourceChanged(self, _index=None) -> None:
        roi = self.selectedROI()
        # The plot chooser only means something for a shape with an interior.
        is_area = roi is not None and _roi_is_area(roi)
        self.roiPlotCombo.setVisible(is_area)
        self.roiPlotLabel.setVisible(is_area)
        if roi is None:
            # Back to Drawn: re-plot whatever is on the layer, as before.
            self._shapesChanged()
            return
        self._plotROIProfile(roi)

    def _plotROIProfile(self, roi) -> None:
        """Profile an ROI from the manager rather than a freshly drawn shape.

        ROI records are in *pixel* coordinates, so unlike the drawn shapes
        there is no world-to-pixel conversion here -- only a pixel-to-distance
        one for the axis.
        """
        from imswitch.imcommon.algorithms.line_sampling import polyline_samples
        from imswitch.imcommon.algorithms.roi_geometry import (
            roi_bounds as _roi_bounds,
        )
        from imswitch.imcommon.algorithms.roi_geometry import (
            roi_mask_local,
            roi_outline,
        )

        self._removeMeasurementRegion(clear_values=False)
        self.plot.clear()
        self._last_payload = []
        self._last_fit_curves = []
        self._current_record_inputs = []

        image = self._currentImage2D()
        if image is None:
            self._setMeasurementAvailable(False)
            self.fitSummary.setText("No image layer selected.")
            return

        # An ROI that misses the image entirely is reported rather than
        # sampled. Sampling outside the array returns zeros, and a flat zero
        # profile is indistinguishable from a real region with no signal.
        height, width = image.shape
        r0, r1, c0, c1 = (int(v) for v in _roi_bounds(roi))
        if r1 <= 0 or c1 <= 0 or r0 >= height or c0 >= width:
            self._setMeasurementAvailable(False)
            self.fitSummary.setText(
                f"{roi.name} lies outside this result ({height} x {width} px)."
            )
            return

        row_scale, col_scale = self._visiblePixelScales()
        unit = self._distanceUnit()
        # One scale for a path that runs in both directions at once; the mean
        # of the two is the honest single number when they differ.
        path_scale = (float(row_scale) + float(col_scale)) / 2.0
        plot_kind = self.roiPlotCombo.currentData() if _roi_is_area(roi) else "line"

        try:
            if plot_kind in ("line", "outline"):
                self._last_kind = "roi-line"
                parts = roi_outline(roi)
                if plot_kind == "line" and getattr(roi, "vertices", None):
                    # An open path: sampled end to end, not closed back on
                    # itself the way an outline is.
                    parts = [np.asarray(roi.vertices, dtype=float)]
                elif plot_kind == "outline":
                    parts = [np.vstack([part, part[:1]]) for part in parts if len(part)]
                profile = None
                for part in parts:
                    profile = polyline_samples(
                        image, part, width=self.widthSpinBox.value()
                    )
                    if profile is not None:
                        break
                if profile is None or profile.size == 0:
                    self._setMeasurementAvailable(False)
                    self.fitSummary.setText("This ROI has no path to sample.")
                    return
                x = np.arange(profile.size, dtype=float) * path_scale
                label = "outline" if plot_kind == "outline" else "line"
                self._setPlotLabels(
                    f"{roi.name} {label}", f"Distance ({unit})", "Intensity"
                )
                self.plot.plot(x, profile, pen=pg.mkPen("r", width=2), name=label)
                self._last_payload = [(label, x, profile)]
                self._current_record_inputs = [
                    (label, x, profile, float(profile.size), float(x[-1] if x.size else 0.0), unit)
                ]
            else:
                self._last_kind = "roi-mean"
                mask, (rows, cols) = roi_mask_local(roi, image.shape)
                if not mask.any():
                    self._setMeasurementAvailable(False)
                    self.fitSummary.setText("This ROI covers no pixels of the image.")
                    return
                window = np.asarray(image[rows, cols], dtype=float)
                # Averaged over the ROI's own pixels, not its bounding box:
                # for anything but a rectangle those are different numbers,
                # and the box is the one nobody asked for.
                inside = np.where(mask, window, np.nan)
                axis, scale, name = (
                    (0, col_scale, "mean x") if plot_kind == "mean-x"
                    else (1, row_scale, "mean y")
                )
                with np.errstate(invalid="ignore"):
                    profile = np.nanmean(inside, axis=axis)
                x = np.arange(profile.size, dtype=float) * scale
                self._setPlotLabels(
                    f"{roi.name} {name}", f"Distance ({unit})", "Mean intensity"
                )
                self.plot.plot(x, profile, pen=pg.mkPen("r", width=2), name=name)
                self._last_payload = [(name, x, profile)]
                self._current_record_inputs = [
                    (name, x, profile, float(profile.size),
                     float(x[-1] if x.size else 0.0), unit)
                ]
        except Exception as exc:
            self._setMeasurementAvailable(False)
            self.fitSummary.setText(f"Could not profile this ROI: {exc}")
            return

        self._applyFits()
        self._setMeasurementAvailable(True)

    def _plotRectangleProfiles(self, bounds):
        self._last_kind = "rectangle"
        self._removeMeasurementRegion(clear_values=False)
        self.plot.clear()
        self._setPlotLabels(
            "rectangle profile", f"Distance ({self._distanceUnit()})", "Mean intensity"
        )
        self._last_payload = []
        self._last_fit_curves = []
        self._current_record_inputs = []

        if bounds is None:
            self._setMeasurementAvailable(False)
            self.fitSummary.setText("")
            return
        image = self._currentImage2D()
        if image is None:
            self._setMeasurementAvailable(False)
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
            self._setMeasurementAvailable(False)
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
        self._setMeasurementAvailable(True)

    def _plotZProfile(self, bounds):
        """Mean intensity through the stack axis over a rectangle.

        ImageJ's *Plot Z-axis Profile*: the in-plane profiles answer "how does
        intensity vary across the field", this one answers "how does it vary
        through the stack" — bleaching over time, an axial PSF, a z-extent.
        With no rectangle drawn it measures the whole frame, as ImageJ does.
        """
        self._last_kind = "zprofile"
        self._removeMeasurementRegion(clear_values=False)
        self.plot.clear()
        self._last_payload = []
        self._last_fit_curves = []
        self._current_record_inputs = []

        layer = self._activeImageLayer()
        data = np.asarray(getattr(layer, "data", None)) if layer is not None else None
        if data is None or data.ndim < 3:
            self._setMeasurementAvailable(False)
            self.plot.setTitle("Z profile")
            self.fitSummary.setText("Select a stack (3D or more) to profile.")
            return

        axis, axis_label = self._stackAxis(data, layer)
        rows, cols = self._roiSliceForBounds(bounds, data.shape[-2:])
        if rows is None:
            self._setMeasurementAvailable(False)
            self.fitSummary.setText("")
            return

        # Every other non-spatial axis stays at what the viewer is showing, so
        # profiling Z on a TZYX stack profiles the timepoint on screen.
        index = [slice(None)] * data.ndim
        step = self._currentStep(data.ndim)
        for other in range(data.ndim - 2):
            if other != axis:
                index[other] = min(max(step[other], 0), data.shape[other] - 1)
        index[-2], index[-1] = rows, cols
        volume = np.asarray(data[tuple(index)], dtype=float)
        volume = volume.reshape(volume.shape[0], -1)

        means = np.nanmean(volume, axis=1)
        scale = self._axisScale(layer, axis)
        z = np.arange(means.size, dtype=float) * scale
        unit = self._distanceUnit() if scale != 1.0 else "slice"

        self._setPlotLabels(
            f"{axis_label} profile", f"{axis_label} ({unit})", "Mean intensity"
        )
        self.plot.plot(z, means, pen=pg.mkPen("#1f77b4", width=2), name="mean")
        self._last_payload = [("mean", z, means)]
        self._current_record_inputs = [
            (
                f"{axis_label.lower()}-profile",
                z,
                means,
                float(means.size),
                float(means.size * scale),
                unit,
            )
        ]
        self._applyFits()
        self._setMeasurementAvailable(True)

    def _stackAxis(self, data, layer) -> tuple[int, str]:
        """Axis to profile along, and its label.

        Prefers a real ``Z`` then ``T`` axis from the layer's labels so the
        plot says which axis it walked; falls back to the first non-spatial
        axis with more than one plane.
        """
        labels = []
        try:
            labels = [str(label) for label in (layer.metadata or {}).get("axis_labels", [])]
        except Exception:
            labels = []
        if len(labels) != data.ndim:
            labels = []
        candidates = range(max(data.ndim - 2, 1))
        for preferred in ("Z", "T"):
            for axis in candidates:
                if labels and labels[axis] == preferred and data.shape[axis] > 1:
                    return axis, preferred
        for axis in candidates:
            if data.shape[axis] > 1:
                return axis, labels[axis] if labels else "Z"
        return 0, labels[0] if labels else "Z"

    def _roiSliceForBounds(self, bounds, shape) -> tuple[slice | None, slice | None]:
        """Row/column slices for a rectangle, or the whole frame when none."""
        height, width = int(shape[0]), int(shape[1])
        if bounds is None:
            return slice(0, height), slice(0, width)
        r0, c0, r1, c1 = bounds
        row_scale, col_scale = self._visiblePixelScales()
        rlo, rhi = sorted((int(round(r0 / row_scale)), int(round(r1 / row_scale))))
        clo, chi = sorted((int(round(c0 / col_scale)), int(round(c1 / col_scale))))
        rlo, rhi = max(0, rlo), min(height, rhi)
        clo, chi = max(0, clo), min(width, chi)
        if rlo >= rhi or clo >= chi:
            return None, None
        return slice(rlo, rhi), slice(clo, chi)

    def _currentStep(self, ndim: int) -> tuple[int, ...]:
        try:
            return tuple(int(value) for value in self._viewer.dims.current_step)
        except Exception:
            return tuple(0 for _ in range(ndim))

    @staticmethod
    def _axisScale(layer, axis: int) -> float:
        try:
            scale = tuple(float(value) for value in layer.scale)
        except Exception:
            return 1.0
        return scale[axis] if 0 <= axis < len(scale) else 1.0

    def _setMeasurementAvailable(self, available: bool) -> None:
        self.measureButton.setEnabled(bool(available))
        self.pushGraphButton.setEnabled(bool(available))
        if not available:
            self.measureButton.setChecked(False)
            self._removeMeasurementRegion(clear_values=True)
        elif self.measureButton.isChecked():
            self._addMeasurementRegion()

    def _measurementToggled(self, enabled: bool) -> None:
        if enabled and self._last_payload:
            self._addMeasurementRegion()
            return
        self._removeMeasurementRegion(clear_values=True)

    def _addMeasurementRegion(self) -> None:
        x_range = self._profileXRange()
        if x_range is None:
            self._removeMeasurementRegion(clear_values=True)
            return
        self._removeMeasurementRegion(clear_values=False)
        x_min, x_max = x_range
        span = x_max - x_min
        defaults = (x_min + span / 3.0, x_min + 2.0 * span / 3.0)
        values = self._measurementValues or defaults
        values = tuple(min(max(float(value), x_min), x_max) for value in values)
        if values[1] <= values[0]:
            values = defaults

        region = pg.LinearRegionItem(
            values=values,
            orientation="vertical",
            brush=pg.mkBrush(255, 215, 0, 45),
            pen=pg.mkPen(255, 190, 0, width=2),
            hoverBrush=pg.mkBrush(255, 215, 0, 75),
            hoverPen=pg.mkPen(255, 225, 80, width=2),
            movable=True,
            bounds=(x_min, x_max),
            swapMode="sort",
        )
        region.setZValue(20)
        region.sigRegionChanged.connect(self._measurementChanged)
        self.plot.addItem(region)
        self._measurementRegion = region
        self._measurementChanged()

    def _removeMeasurementRegion(self, *, clear_values: bool) -> None:
        region = self._measurementRegion
        self._measurementRegion = None
        if region is not None:
            try:
                self.plot.removeItem(region)
            except Exception:
                pass
        if clear_values:
            self._measurementValues = None
        self.measurementSummary.clear()

    def _measurementChanged(self) -> None:
        region = self._measurementRegion
        if region is None:
            return
        x_1, x_2 = sorted(float(value) for value in region.getRegion())
        self._measurementValues = (x_1, x_2)
        self.measurementSummary.setText(
            f"x₁={x_1:.6g}  x₂={x_2:.6g}  Δx={x_2 - x_1:.6g}"
        )

    def _profileXRange(self) -> tuple[float, float] | None:
        finite_ranges = []
        for _name, x, _y in self._last_payload:
            values = np.asarray(x, dtype=float).ravel()
            values = values[np.isfinite(values)]
            if values.size:
                finite_ranges.append((float(values.min()), float(values.max())))
        if not finite_ranges:
            return None
        x_min = min(start for start, _end in finite_ranges)
        x_max = max(end for _start, end in finite_ranges)
        if x_max <= x_min:
            padding = max(abs(x_min) * 0.5, 0.5)
            return x_min - padding, x_max + padding
        return x_min, x_max

    def _applyFits(self):
        # Recomputed from scratch on every refresh, like the curves they
        # annotate — a fit left over from the previous result or fit type
        # would otherwise ride along into a pushed plot.
        self._last_fit_curves = []
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
            fit_label = f"{name} {result.name}"
            self.plot.plot(result.x, result.y, pen=pen, name=fit_label)
            self._last_fit_curves.append((fit_label, result.x, result.y))
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

    def _onPushToGraph(self):
        payload = self.buildPlotPayload()
        if payload is None:
            return
        self.sigPlotPushed.emit(payload)

    def buildPlotPayload(self):
        """This profile and its fit as a PlotPayload the Graph can hold.

        Titled after the source layer, which carries the result's name, so a
        profile pushed from one reconstruction and one from the next are
        distinguishable side by side — and pushing the same profile twice
        replaces its earlier version instead of piling up.
        """
        if not self._last_payload:
            return None
        series = [
            PlotSeries(name=str(name), x=np.asarray(x), y=np.asarray(y))
            for name, x, y in self._last_payload
        ]
        series.extend(
            PlotSeries(
                name=str(name),
                x=np.asarray(x),
                y=np.asarray(y),
                style={"dash": True},
            )
            for name, x, y in self._last_fit_curves
        )
        layer = self._activeImageLayer()
        source = str(getattr(layer, "name", "") or "profile")
        # Taken from the plot rather than re-derived: a Z profile runs along
        # the stack axis in its own units, and a payload that relabelled it
        # "Distance" would be a wrong axis on a pushed curve.
        x_label, y_label = self._last_plot_labels
        return PlotPayload(
            title=f"{source} — {self._last_plot_title}",
            x_label=x_label,
            y_label=y_label,
            series=series,
            metadata={"source_layer": source, "profile_kind": self._last_kind},
        )

    def _onPushToTable(self):
        if not self._current_record_inputs:
            return
        from imswitch.improcess.view.ResultsTableWidget import merge_columns

        records = self._buildOutputRecords()

        columns = []
        for record in records:
            columns = merge_columns(columns, record.keys())

        self.sigResultPushed.emit(columns, records)

    def _onSaveCSV(self):
        if not self._current_record_inputs:
            return
        from imswitch.improcess.view.ResultsTableWidget import merge_columns, records_to_csv

        records = self._buildOutputRecords()

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

    def _sourceName(self) -> str:
        """Name of the layer this profile was measured on."""
        layer = self._activeImageLayer()
        return str(getattr(layer, "name", "") or "image")

    def _buildOutputRecords(self) -> list[dict]:
        """Build profile/fit rows plus the optional manual Δx measurement."""
        source = self._sourceName()
        records = []
        for rec_input in self._current_record_inputs:
            if len(rec_input) == 7:
                kind, x, y, length_px, length_scaled, unit, fit_metrics = rec_input
            else:
                kind, x, y, length_px, length_scaled, unit = rec_input
                fit_metrics = None
            record = build_profile_record(
                kind,
                x,
                y,
                length_px=length_px,
                length_scaled=length_scaled,
                unit=unit,
                fit_metrics=fit_metrics,
            )
            # Which result the profile was measured on. The Results table
            # accumulates, so rows pushed from two reconstructions are
            # otherwise indistinguishable apart from the values themselves.
            records.append({"source": source, **record})

        if self._measurementValues is not None and self.measureButton.isChecked():
            unit = (
                str(self._current_record_inputs[0][5])
                if self._current_record_inputs else "px"
            )
            title = (
                "Line Profile" if self._last_kind == "line"
                else "Rectangle Projections"
            )
            records.append({
                "source": source,
                **build_delta_x_record(
                    title,
                    f"Distance ({unit})",
                    *self._measurementValues,
                    kind="profile-delta-x",
                ),
            })
        return records

    @staticmethod
    def _computeLineProfile(image, r0, c0, r1, c1, width=1):
        """This panel's profile, through the shared sampler.

        ``gaussian`` keeps this panel's long-standing across-width weighting;
        the ROI manager's line measurements use the uniform mean, which is what
        ImageJ's line width does. One sampler either way, so a plotted profile
        and a measured line mean cannot disagree about what the line covers.
        """
        try:
            return line_samples(
                image, r0, c0, r1, c1, width=width, weighting="gaussian"
            )
        except Exception:
            return None
