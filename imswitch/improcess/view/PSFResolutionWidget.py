"""Interactive PSF / bead resolution panel for ImProcess.

Producing panel: the buttons run a processor on the selected result through
the generic run->publish pipeline (sigRunRequested ->
ResultProcessorController -> sigResultProduced). ``self.processor`` is the
processor the controller runs, so the panel points it at whichever action
the user took:

* an image is selected -> **Preview** runs the bead analysis in place and
  marks every candidate in the viewer (selected beads green, rejected ones
  coloured by reason) with a clickable bead list; **Fit** runs
  ``psf-resolution`` with the form's parameters (plus the ROI Manager's ROIs
  when that is the source) and reports the result here;
* a bead table (``psf-resolution``'s ``beads`` output) is selected ->
  **Apply selection** runs ``psf-bead-select``. The histogram shows the
  fitted lateral FWHMs; dragging the shaded range sets the lateral FWHM
  bounds, which stay editable in the form below it.

Only the essentials are open: where the beads come from, the optics and the
outputs. Selection, the calibration override (the pixel size and z step come
from the data) and the detection details are collapsed.
"""

from __future__ import annotations

import numpy as np
from qtpy import QtCore, QtGui, QtWidgets

from imswitch.improcess.layer_selection import PSF_PREVIEW_LAYER_NAME as PREVIEW_LAYER_NAME
from imswitch.improcess.processors import PSFBeadSelectProcessor, PSFResolutionProcessor

# The preview overlay (PREVIEW_LAYER_NAME) is one of layer_selection's
# annotation layers, so it never becomes the image a tool measures.

#: Overlay colour per bead state (``""`` = selected).
REASON_COLORS = {
    "": "#33dd55",
    "border": "#8c8c8c",
    "crowded": "#ff9f1c",
    "bright": "#d65bd6",
    "saturated": "#d65bd6",
    "fit_failed": "#ff4d4d",
    "fit_quality": "#ff4d4d",
    "ellipticity": "#ff4d4d",
    "fwhm_outlier": "#ff4d4d",
}
#: Short tag drawn next to a rejected bead.
REASON_TAGS = {
    "border": "edge",
    "crowded": "crowded",
    "bright": "bright",
    "saturated": "saturated",
    "fit_failed": "no fit",
    "fit_quality": "R²",
    "ellipticity": "elliptic",
    "fwhm_outlier": "FWHM",
}


class PSFResolutionWidget(QtWidgets.QWidget):
    """Fit bead PSFs on the selected image, or re-select a fitted bead table."""

    sigRunRequested = QtCore.Signal(object, dict)

    def __init__(self, napariViewer, roiManagerWidget=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._roiManagerWidget = roiManagerWidget
        self._currentResult = None
        self._previewRun = None
        self._collecting = False
        self._collected: list = []
        self.fitProcessor = PSFResolutionProcessor()
        self.selectProcessor = PSFBeadSelectProcessor()
        self.processor = self.fitProcessor

        self._selectText = (
            "Select an image (beads) to fit, or a bead table to re-select. "
            "Results appear in the results list and table."
        )
        self._readyText = "Ready. Preview shows which beads a fit would use."
        self._incompatibleText = "Selected result is not compatible with PSF resolution."
        self._selectionText = "Bead table selected: drag the range or edit the bounds, then apply."

        # --- fit mode ---------------------------------------------------- #
        self.dataLabel = QtWidgets.QLabel("")
        self.dataLabel.setWordWrap(True)
        self.form = self.fitProcessor.make_param_widget(self)
        self.sourceCombo = self.form.controls["source"]
        self.previewButton = QtWidgets.QPushButton("Preview beads")
        self.previewButton.setToolTip(
            "Detect, fit and select the beads without publishing anything; every candidate is "
            "marked in the viewer, coloured by whether and why it was rejected."
        )
        self.clearPreviewButton = QtWidgets.QPushButton("Clear")
        self.clearPreviewButton.setToolTip("Remove the preview markers.")
        self.fitButton = QtWidgets.QPushButton("Fit")
        self.fitButton.setToolTip("Run the analysis and publish its results.")
        self.previewButton.setEnabled(False)
        self.fitButton.setEnabled(False)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.previewButton)
        buttons.addWidget(self.clearPreviewButton)
        buttons.addStretch()
        buttons.addWidget(self.fitButton)

        self.legendLabel = QtWidgets.QLabel(_legend_html())
        self.legendLabel.setWordWrap(True)
        self.legendLabel.setVisible(False)
        self.beadTable = QtWidgets.QTableWidget(0, 6)
        self.beadTable.setHorizontalHeaderLabels(["#", "state", "z", "y", "x", "FWHM xy / z"])
        self.beadTable.verticalHeader().setVisible(False)
        self.beadTable.verticalHeader().setDefaultSectionSize(20)
        self.beadTable.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.beadTable.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.beadTable.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.beadTable.horizontalHeader().setStretchLastSection(True)
        self.beadTable.setMinimumHeight(140)
        self.beadTable.setMaximumHeight(240)
        self.beadTable.setToolTip("Click a bead to centre the viewer on it.")
        self.beadTable.setVisible(False)

        self.fitBox = QtWidgets.QWidget(self)
        fit_layout = QtWidgets.QVBoxLayout(self.fitBox)
        fit_layout.setContentsMargins(0, 0, 0, 0)
        fit_layout.addWidget(self.dataLabel)
        fit_layout.addWidget(self.form)
        fit_layout.addLayout(buttons)
        fit_layout.addWidget(self.legendLabel)
        fit_layout.addWidget(self.beadTable)

        # --- selection mode ---------------------------------------------- #
        self.selectionBox = QtWidgets.QWidget(self)
        selection_layout = QtWidgets.QVBoxLayout(self.selectionBox)
        selection_layout.setContentsMargins(0, 0, 0, 0)
        self.histogram, self.region = self._make_histogram()
        if self.histogram is not None:
            selection_layout.addWidget(self.histogram)
        self.selectForm = self.selectProcessor.make_param_widget(self)
        selection_layout.addWidget(self.selectForm)
        self.applySelectionButton = QtWidgets.QPushButton("Apply selection")
        selection_layout.addWidget(self.applySelectionButton)
        self.selectionBox.setVisible(False)

        self.summaryLabel = QtWidgets.QLabel(self._selectText)
        self.summaryLabel.setWordWrap(True)
        self.summaryLabel.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.summaryLabel.setStyleSheet("font-size:8pt;")

        scroll_body = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(scroll_body)
        body.setContentsMargins(4, 4, 4, 4)
        body.addWidget(self.summaryLabel)
        body.addWidget(self.fitBox)
        body.addWidget(self.selectionBox)
        body.addStretch()
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(scroll_body)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

        self.fitButton.clicked.connect(self.run)
        self.previewButton.clicked.connect(self.preview)
        self.clearPreviewButton.clicked.connect(self.clearPreview)
        self.beadTable.itemSelectionChanged.connect(self._jumpToSelectedBead)
        self.applySelectionButton.clicked.connect(self.applySelection)
        for key in ("pixel_size_nm", "z_step_nm"):
            self.form.controls[key].valueChanged.connect(self._updateDataLabel)
        lat_min = self.selectForm.controls["fwhm_lat_min"]
        lat_max = self.selectForm.controls["fwhm_lat_max"]
        lat_min.valueChanged.connect(self._spinsToRegion)
        lat_max.valueChanged.connect(self._spinsToRegion)
        if self.region is not None:
            self.region.sigRegionChangeFinished.connect(self._regionToSpins)

    # ------------------------------------------------------------------ #
    # Result-processor widget contract
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        """Fit the selected image."""
        if self._currentResult is None:
            self.summaryLabel.setText("No result selected. Load or create a result first.")
            return
        if not self._fitAccepts(self._currentResult):
            self.summaryLabel.setText(self._incompatibleText)
            return
        try:
            params = self.parameterValues()
        except ValueError as exc:
            self.summaryLabel.setText(str(exc))
            return
        self.processor = self.fitProcessor
        self._collecting, self._collected = True, []
        self.summaryLabel.setText("Fitting…")
        QtWidgets.QApplication.processEvents()
        with _busy():
            self.sigRunRequested.emit(self._currentResult, params)
        # The controller answers synchronously (setStatusText); without one
        # nothing is coming.
        self._collecting = False

    def preview(self) -> None:
        """Run the bead analysis on the selected image and mark the beads."""
        from imswitch.improcess.processors.psf_resolution.processor import run_bead_analysis

        if self._currentResult is None or not self._fitAccepts(self._currentResult):
            self.summaryLabel.setText("Select an image to preview its beads.")
            return
        try:
            params = self.parameterValues()
        except ValueError as exc:
            self.summaryLabel.setText(str(exc))
            return
        self.summaryLabel.setText("Detecting and fitting beads…")
        QtWidgets.QApplication.processEvents()
        try:
            with _busy():
                run = run_bead_analysis(self._currentResult, params)
        except Exception as exc:
            self.summaryLabel.setText(f"Preview failed: {exc}")
            return
        self._previewRun = run
        self._fillBeadTable(run)
        drawn = self._drawPreview(run)
        from imswitch.improcess.processors.psf_resolution.result import psf_report

        text = psf_report(run.summary())
        if not drawn and self._viewer is not None:
            text += "\n(No image layer of this result is shown, so the beads are not marked.)"
        self.summaryLabel.setText(text)

    def clearPreview(self) -> None:
        self._previewRun = None
        self._removePreviewLayer()
        self.beadTable.setRowCount(0)
        self.beadTable.setVisible(False)
        self.legendLabel.setVisible(False)

    def applySelection(self) -> None:
        """Re-select the beads of the selected bead table."""
        if self._currentResult is None or not self._selectAccepts(self._currentResult):
            self.summaryLabel.setText("Select a PSF bead table to re-select beads.")
            return
        self.processor = self.selectProcessor
        self._collecting, self._collected = True, []
        self.sigRunRequested.emit(self._currentResult, self.selectForm.get_values())
        self._collecting = False

    def parameterValues(self) -> dict:
        """The fit parameters, with the ROI Manager's ROIs for the ROI source.

        Keys match ``PSFResolutionProcessor``'s parameter contract (``rois``
        being its one extra key)."""
        params = self.form.get_values()
        if params.get("source") == "rois":
            if self._roiManagerWidget is None:
                raise ValueError("ROI Manager panel is not enabled.")
            rois = self._roiManagerWidget.rois()
            if not rois:
                raise ValueError("ROI Manager has no ROIs.")
            params["rois"] = rois
        return params

    def setCurrentResult(self, result) -> None:
        """Store the current result and switch between fit and selection mode."""
        from imswitch.improcess.processors.psf_resolution.result import PSFSummaryResult

        if self._collecting and isinstance(result, PSFSummaryResult):
            self._collected.append(result)
        if result is not self._currentResult:
            # A preview describes the result it was computed on.
            self.clearPreview()
        self._currentResult = result
        selecting = self._selectAccepts(result)
        fitting = self._fitAccepts(result)
        self.fitBox.setVisible(not selecting)
        self.selectionBox.setVisible(selecting)
        self.fitButton.setEnabled(fitting)
        self.previewButton.setEnabled(fitting)
        self.processor = self.selectProcessor if selecting else self.fitProcessor
        self._updateDataLabel()
        if self._collecting:
            return  # the run's report replaces the mode text when the run ends
        if result is None:
            self.summaryLabel.setText(self._selectText)
        elif selecting:
            self._loadBeadTable(result)
            self.summaryLabel.setText(self._selectionText)
        elif fitting:
            self.summaryLabel.setText(self._readyText)
        else:
            self.summaryLabel.setText(self._incompatibleText)

    def setStatusText(self, text: str) -> None:
        """The controller's run status; after a fit, preceded by the report."""
        if self._collecting:
            self._collecting = False
            if self._collected:
                text = f"{self._collected[-1].report()}\n\n{text}"
                self._collected = []
            if self._selectAccepts(self._currentResult):
                self._loadBeadTable(self._currentResult)
        self.summaryLabel.setText(text)

    def setRoiManagerWidget(self, roiManagerWidget) -> None:
        """Wire (or rewire) the ROI Manager dependency at runtime."""
        self._roiManagerWidget = roiManagerWidget

    # ------------------------------------------------------------------ #
    # Data / calibration line
    # ------------------------------------------------------------------ #
    def _updateDataLabel(self, *_args) -> None:
        from imswitch.improcess.processors.psf_resolution._params import CALIBRATION_GROUP
        from imswitch.improcess.processors.psf_resolution.processor import input_layout

        result = self._currentResult
        if result is None or not self._fitAccepts(result) or self._selectAccepts(result):
            self.dataLabel.setText("")
            return
        try:
            layout = input_layout(result, self.form.get_values())
        except Exception as exc:
            self.dataLabel.setText(f"Data: {exc}")
            return
        uncalibrated = layout.pixel_size is None
        color = "#e0a030" if uncalibrated or layout.note else "#9a9a9a"
        text = f"<span style='color:{color}'>Data: {layout.describe()}"
        if uncalibrated:
            text += (" Physical widths, the diffraction-limit comparison and aberrations need the "
                     "pixel size (and z step): fix the metadata or open 'Calibration override'.")
        self.dataLabel.setText(text + "</span>")
        section = getattr(self.form, "sections", {}).get(CALIBRATION_GROUP)
        if section is not None and uncalibrated and not section.isExpanded():
            section.setExpanded(True)

    # ------------------------------------------------------------------ #
    # Preview overlay and bead list
    # ------------------------------------------------------------------ #
    def _fillBeadTable(self, run) -> None:
        analysis = run.analysis
        unit = analysis.unit
        order = sorted(range(len(analysis.beads)), key=lambda i: (run.reasons[i] != "", i))
        self.beadTable.setRowCount(len(order))
        for row, index in enumerate(order):
            bead, reason = analysis.beads[index], run.reasons[index]
            z = bead.get("z", bead.get("z_det"))
            y = bead.get("y", bead.get("y_det"))
            x = bead.get("x", bead.get("x_det"))
            lat = bead.get("fwhm_lat_hm", bead.get("fwhm_lat"))
            axial = bead.get("fwhm_z_hm", bead.get("fwhm_z"))
            widths = "" if lat is None else _fmt(lat, unit) + ("" if axial is None else f" / {_fmt(axial, unit)}")
            cells = [
                str(bead["id"]), "selected" if not reason else REASON_TAGS.get(reason, reason),
                "" if z is None else f"{z:.1f}", f"{y:.1f}", f"{x:.1f}", widths,
            ]
            for column, value in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(value)
                item.setData(QtCore.Qt.UserRole, index)
                if column == 1:
                    item.setForeground(QtGui.QBrush(QtGui.QColor(REASON_COLORS.get(reason, "#ff4d4d"))))
                    item.setToolTip(_reason_text(reason, bead))
                self.beadTable.setItem(row, column, item)
        self.beadTable.resizeColumnsToContents()
        self.beadTable.setVisible(bool(order))
        self.legendLabel.setVisible(bool(order))

    def _drawPreview(self, run) -> bool:
        """Mark every candidate on the image layer of the current result."""
        self._removePreviewLayer()
        viewer = self._viewer
        layer = self._sourceLayer()
        if viewer is None or layer is None or not run.analysis.beads:
            return False
        points = np.array([self._displayCoords(run, bead, layer) for bead in run.analysis.beads], dtype=float)
        colors = [REASON_COLORS.get(reason, "#ff4d4d") for reason in run.reasons]
        labels = [
            f"{bead['id']}" if not reason else f"{bead['id']} {REASON_TAGS.get(reason, reason)}"
            for bead, reason in zip(run.analysis.beads, run.reasons)
        ]
        lateral_px = float(np.mean(run.analysis.expected_sigma_px[-2:])) * 2.3548
        try:
            previous = viewer.layers.selection.active
        except Exception:
            previous = None
        try:
            viewer.add_points(
                points,
                name=PREVIEW_LAYER_NAME,
                size=max(4.0, 4.0 * lateral_px),
                face_color="transparent",
                border_color=colors,
                border_width=0.12,
                border_width_is_relative=True,
                out_of_slice_display=True,
                features={"label": labels, "reason": [r or "selected" for r in run.reasons]},
                text={"string": "{label}", "size": 8, "color": "white", "anchor": "upper_left"},
                scale=tuple(layer.scale),
                translate=tuple(layer.translate),
                opacity=0.9,
                metadata={"psf_preview": True},
            )
        except Exception:
            return False
        if previous is not None:
            try:
                viewer.layers.selection.active = previous
            except Exception:
                pass
        return True

    def _displayCoords(self, run, bead, layer) -> list[float]:
        """A bead's position in the layer's (view-mode transposed) data axes."""
        labels = list(run.layout.labels)
        ndim = len(labels)
        coords = [0.0] * ndim
        coords[ndim - 2] = float(bead.get("y", bead["y_det"]))
        coords[ndim - 1] = float(bead.get("x", bead["x_det"]))
        if run.layout.is3d:
            coords[labels.index("Z")] = float(bead.get("z", bead["z_det"]))
        transpose = _view_transpose(self._currentResult, layer, ndim)
        displayed = [coords[axis] for axis in transpose]
        layer_ndim = int(getattr(layer, "ndim", len(displayed)))
        return displayed[-layer_ndim:] if layer_ndim <= len(displayed) else [0.0] * (layer_ndim - len(displayed)) + displayed

    def _jumpToSelectedBead(self) -> None:
        run = self._previewRun
        items = self.beadTable.selectedItems()
        if run is None or not items or self._viewer is None:
            return
        index = items[0].data(QtCore.Qt.UserRole)
        layer = self._sourceLayer()
        if layer is None:
            return
        bead = run.analysis.beads[int(index)]
        data_coords = np.asarray(self._displayCoords(run, bead, layer), dtype=float)
        world = data_coords * np.asarray(layer.scale, dtype=float) + np.asarray(layer.translate, dtype=float)
        viewer = self._viewer
        try:
            offset = viewer.dims.ndim - len(world)
            displayed = set(viewer.dims.displayed)
            for k, value in enumerate(world):
                if k + offset not in displayed:
                    viewer.dims.set_point(k + offset, float(value))
            viewer.camera.center = tuple(float(world[a - offset]) for a in viewer.dims.displayed)
        except Exception:
            pass
        preview = self._previewLayer()
        if preview is not None:
            try:
                preview.selected_data = {int(index)}
            except Exception:
                pass

    def _sourceLayer(self):
        """The image layer showing the current result (else the active image)."""
        viewer = self._viewer
        if viewer is None:
            return None
        uid = getattr(self._currentResult, "result_uid", None)
        try:
            layers = list(viewer.layers)
        except Exception:
            return None
        if uid:
            for layer in layers:
                if (getattr(layer, "metadata", {}) or {}).get("result_uid") == uid and _is_image(layer):
                    return layer
        from imswitch.improcess.layer_selection import active_image_layer

        return active_image_layer(viewer, exclude_names=(PREVIEW_LAYER_NAME,))

    def _previewLayer(self):
        try:
            for layer in self._viewer.layers:
                if getattr(layer, "name", "") == PREVIEW_LAYER_NAME:
                    return layer
        except Exception:
            pass
        return None

    def _removePreviewLayer(self) -> None:
        if self._viewer is None:
            return
        layer = self._previewLayer()
        if layer is not None:
            try:
                self._viewer.layers.remove(layer)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Selection mode
    # ------------------------------------------------------------------ #
    def _make_histogram(self):
        try:
            import pyqtgraph as pg
        except Exception:  # pragma: no cover - pyqtgraph is a dependency
            return None, None
        plot = pg.PlotWidget()
        plot.setMinimumHeight(140)
        plot.setLabel("left", "Beads")
        plot.setMouseEnabled(x=False, y=False)
        region = pg.LinearRegionItem(values=(0.0, 1.0))
        plot.addItem(region)
        plot._curve = plot.plot([0.0, 1.0], [0.0], stepMode="center", fillLevel=0, brush=(120, 120, 120, 120))
        return plot, region

    def _loadBeadTable(self, result) -> None:
        from imswitch.improcess.analysis.bead_psf import resolve_selection

        analysis = result.analysis
        values = np.array([b["fwhm_lat"] for b in analysis.fitted()], dtype=np.float64)
        selection = resolve_selection(analysis, result.selection)
        lo, hi = selection.fwhm_lat_range
        if self.histogram is not None and values.size:
            counts, edges = np.histogram(values, bins=max(10, min(60, values.size // 2)))
            self.histogram._curve.setData(edges, counts)
            self.histogram.setLabel("bottom", f"Lateral FWHM, Gaussian fit ({analysis.unit})")
            self.region.setBounds((0.0, 2.0 * float(edges[-1])))
        current = {
            "max_ellipticity": 0.0 if result.selection.max_ellipticity is None else selection.max_ellipticity,
            "min_r2": selection.min_r2,
            "range_mad": selection.range_mad,
        }
        if np.isfinite(lo) and np.isfinite(hi):
            current.update({"fwhm_lat_min": max(lo, 0.0), "fwhm_lat_max": max(hi, 0.0)})
        if selection.fwhm_z_range is not None and all(np.isfinite(selection.fwhm_z_range)):
            current.update({
                "fwhm_z_min": max(selection.fwhm_z_range[0], 0.0),
                "fwhm_z_max": max(selection.fwhm_z_range[1], 0.0),
            })
        self.selectForm.set_values(current)
        self._spinsToRegion()

    def _spinsToRegion(self, *_args) -> None:
        if self.region is None:
            return
        lo = self.selectForm.controls["fwhm_lat_min"].value()
        hi = self.selectForm.controls["fwhm_lat_max"].value()
        if hi > lo:
            self.region.blockSignals(True)
            self.region.setRegion((lo, hi))
            self.region.blockSignals(False)

    def _regionToSpins(self, *_args) -> None:
        lo, hi = self.region.getRegion()
        self.selectForm.set_values({"fwhm_lat_min": max(float(lo), 0.0), "fwhm_lat_max": max(float(hi), 0.0)})

    # ------------------------------------------------------------------ #
    def _fitAccepts(self, result) -> bool:
        return self._accepts(self.fitProcessor, result)

    def _selectAccepts(self, result) -> bool:
        return self._accepts(self.selectProcessor, result)

    @staticmethod
    def _accepts(processor, result) -> bool:
        if result is None:
            return False
        try:
            return bool(processor.accepts(result))
        except Exception:
            return False


class _busy:
    """Wait cursor for the duration of a synchronous analysis."""

    def __enter__(self):
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        return self

    def __exit__(self, *exc):
        QtWidgets.QApplication.restoreOverrideCursor()
        return False


def _is_image(layer) -> bool:
    data = getattr(layer, "data", None)
    return type(layer).__name__ == "Image" or (isinstance(data, np.ndarray) and data.ndim >= 2)


def _view_transpose(result, layer, ndim: int) -> tuple[int, ...]:
    """The axis order the layer shows ``result`` in (its view mode's transpose)."""
    name = (getattr(layer, "metadata", {}) or {}).get("view_mode")
    for mode in getattr(result, "view_modes", None) or ():
        if getattr(mode, "name", None) == name and len(mode.transpose) == ndim:
            return tuple(mode.transpose)
    return tuple(range(ndim))


def _fmt(value, unit: str) -> str:
    if value is None or not np.isfinite(value):
        return "–"
    return f"{value:.0f}" if unit == "nm" else f"{value:.2f}"


def _reason_text(reason: str, bead: dict) -> str:
    from imswitch.improcess.analysis.bead_psf import reason_label

    text = reason_label(reason)
    if reason in ("fit_quality", "ellipticity", "fwhm_outlier", "") and "r2" in bead:
        text += f" (R² {bead['r2']:.2f}"
        if "r2_z" in bead:
            text += f" / {bead['r2_z']:.2f}"
        text += f", ellipticity {bead['ellipticity']:.2f})"
    return text


def _legend_html() -> str:
    entries = [
        ("", "selected"), ("border", "edge / no data"), ("crowded", "crowded"),
        ("bright", "bright / saturated"), ("fit_quality", "rejected by fit or selection"),
    ]
    return " ".join(
        f"<span style='color:{REASON_COLORS[key]}'>●</span>&nbsp;{label}" for key, label in entries
    )
