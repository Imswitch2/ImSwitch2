"""Interactive PSF / bead resolution panel for ImProcess.

Producing panel: Fit runs ``psf-resolution`` on the selected result through
the generic run->publish pipeline (sigRunRequested ->
ResultProcessorController -> sigResultProduced). ``self.processor`` is the
processor the controller runs.

The panel reads top to bottom, in the order the work happens:

1. **Setup** (an image is selected): what the data is, where the beads come
   from, the optics, the outputs. Calibration and detection details are
   collapsed.
2. **Beads**: Preview detects, fits and selects the beads without publishing
   anything and marks every candidate in the viewer (green = used, other
   colours = why not). The histogram's shaded range is the lateral FWHM
   selection: dragging it (or editing the bounds) re-selects live, no
   refitting. Fit then publishes the measurement with that selection.
3. **Result** (a measurement is selected, e.g. right after a fit): the
   widths, the averaged PSF, the aberrations and the warnings at a glance,
   the statistics in tabs, and the same bead list and selection; changing
   the selection there and pressing Re-select makes a new measurement.

A fit publishes two results: the measurement (one entry holding the beads,
their statistics and the aberrations) and the averaged PSF as an ordinary
image. The measurement is selected afterwards, so the card shows it.
"""

from __future__ import annotations

import numpy as np
from qtpy import QtCore, QtGui, QtWidgets

from imswitch.improcess.layer_selection import PSF_PREVIEW_LAYER_NAME as PREVIEW_LAYER_NAME
from imswitch.improcess.processors import PSFBeadSelectProcessor, PSFResolutionProcessor
from imswitch.improcess.processors.psf_resolution._params import (
    CALIBRATION_GROUP,
    SELECTION_FIELDS,
    CollapsibleSection,
    build_form,
)
from imswitch.improcess.processors.psf_resolution.result import STATE_COLORS, STATE_TAGS

# The preview overlay (PREVIEW_LAYER_NAME) is one of layer_selection's
# annotation layers, so it never becomes the image a tool measures.

_SELECTION_KEYS = {f.key for f in SELECTION_FIELDS}
_MUTED = "#9a9a9a"
_WARN = "#e0a030"


class PSFResolutionWidget(QtWidgets.QWidget):
    """Measure the PSF from the beads of the selected image; show a measurement."""

    sigRunRequested = QtCore.Signal(object, dict)

    def __init__(self, napariViewer, roiManagerWidget=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._roiManagerWidget = roiManagerWidget
        self._currentResult = None
        #: The bead run the Beads section shows: a preview, or the selected
        #: measurement's run re-selected with the current bounds.
        self._beadRun = None
        self._previewing = False
        self.fitProcessor = PSFResolutionProcessor()
        self.selectProcessor = PSFBeadSelectProcessor()
        self.processor = self.fitProcessor

        self._idleText = "Select an image of beads to measure its PSF, or a PSF measurement to inspect it."
        self._readyText = "Preview shows which beads a fit would use; Fit publishes the measurement."
        self._incompatibleText = "The selected result is neither an image nor a PSF measurement."

        self.dataLabel = QtWidgets.QLabel("")
        self.dataLabel.setWordWrap(True)

        # --- 1. setup ------------------------------------------------------ #
        spec = [f for f in self.fitProcessor.param_spec() if f.key not in _SELECTION_KEYS]
        self.form = build_form(self, spec, collapsed=(CALIBRATION_GROUP, "Advanced"))
        self.sourceCombo = self.form.controls["source"]
        self.previewButton = QtWidgets.QPushButton("Preview beads")
        self.previewButton.setToolTip(
            "Detect, fit and select the beads without publishing anything; every candidate is "
            "marked in the viewer, coloured by whether and why it was rejected."
        )
        self.clearPreviewButton = QtWidgets.QPushButton("Clear")
        self.clearPreviewButton.setToolTip("Remove the preview markers.")
        self.fitButton = QtWidgets.QPushButton("Fit")
        self.fitButton.setToolTip("Measure the PSF with the current selection and publish the measurement "
                                  "and the averaged PSF.")
        self.previewButton.setEnabled(False)
        self.fitButton.setEnabled(False)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.previewButton)
        buttons.addWidget(self.clearPreviewButton)
        buttons.addStretch()
        # Fit comes after the bead selection, which it uses.
        self.fitRow = QtWidgets.QWidget(self)
        fitRow = QtWidgets.QHBoxLayout(self.fitRow)
        fitRow.setContentsMargins(0, 0, 0, 0)
        fitRow.addStretch()
        fitRow.addWidget(self.fitButton)
        self.requirementLabel = QtWidgets.QLabel("")
        self.requirementLabel.setWordWrap(True)
        self.requirementLabel.setStyleSheet(f"color:{_WARN};")
        self.requirementLabel.setVisible(False)

        self.setupBox = QtWidgets.QWidget(self)
        setup = QtWidgets.QVBoxLayout(self.setupBox)
        setup.setContentsMargins(0, 0, 0, 0)
        setup.addWidget(self.form)
        setup.addLayout(buttons)
        setup.addWidget(self.requirementLabel)

        # --- 3. result card (shown above the beads) ------------------------ #
        self.resultBox = QtWidgets.QWidget(self)
        card = QtWidgets.QVBoxLayout(self.resultBox)
        card.setContentsMargins(0, 0, 0, 0)
        self.headlineLabel = QtWidgets.QLabel("")
        self.headlineLabel.setWordWrap(True)
        self.headlineLabel.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.headlineLabel.setTextFormat(QtCore.Qt.RichText)
        self.thumbnails = _Thumbnails(self.resultBox)
        self.reportLabel = QtWidgets.QLabel("")
        self.reportLabel.setWordWrap(True)
        self.reportLabel.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.reportLabel.setStyleSheet("font-size:8pt;")
        self.statsTabs = QtWidgets.QTabWidget(self.resultBox)
        self.summaryTable = _table(["Metric", "Median ± MAD / value", "Averaged PSF", "Theory"])
        self.zernikeTable = _table(["Mode", "nm RMS", "± nm", "mλ"])
        self.statsTabs.addTab(self.summaryTable, "Summary")
        self.statsTabs.addTab(self.zernikeTable, "Aberrations")
        self.statsTabs.setMaximumHeight(240)
        card.addWidget(self.headlineLabel)
        card.addWidget(self.thumbnails)
        card.addWidget(self.reportLabel)
        card.addWidget(self.statsTabs)
        self.resultBox.setVisible(False)

        # --- 2. beads ------------------------------------------------------- #
        self.beadsSection = CollapsibleSection("Bead selection", parent=self)
        beads = self.beadsSection.form
        self.countLabel = QtWidgets.QLabel("")
        self.countLabel.setWordWrap(True)
        self.histogram, self.region = self._make_histogram()
        self.selectForm = build_form(self, SELECTION_FIELDS, collapsed=("Selection",))
        self.selectForm.sections["Selection"].setTitle("Selection bounds")
        self.autoButton = QtWidgets.QPushButton("Automatic range")
        self.autoButton.setToolTip("Back to the automatic ranges (median ± k MAD) and thresholds.")
        self.applySelectionButton = QtWidgets.QPushButton("Re-select")
        self.applySelectionButton.setToolTip(
            "Measure again with this selection (no refitting); publishes a new measurement."
        )
        selectButtons = QtWidgets.QHBoxLayout()
        selectButtons.addWidget(self.autoButton)
        selectButtons.addStretch()
        selectButtons.addWidget(self.applySelectionButton)
        self.legendLabel = QtWidgets.QLabel(_legend_html())
        self.legendLabel.setWordWrap(True)
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
        self.previewReportLabel = QtWidgets.QLabel("")
        self.previewReportLabel.setWordWrap(True)
        self.previewReportLabel.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.previewReportLabel.setStyleSheet("font-size:8pt;")
        beads.setContentsMargins(0, 0, 0, 4)
        beads.addRow(self.countLabel)
        if self.histogram is not None:
            beads.addRow(self.histogram)
        beads.addRow(self.selectForm)
        beads.addRow(selectButtons)
        beads.addRow(self.previewReportLabel)
        beads.addRow(self.legendLabel)
        beads.addRow(self.beadTable)
        self.beadsSection.setVisible(False)

        #: The controller's status (and any message of the panel's own).
        self.summaryLabel = QtWidgets.QLabel(self._idleText)
        self.summaryLabel.setWordWrap(True)
        self.summaryLabel.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.summaryLabel.setStyleSheet(f"font-size:8pt; color:{_MUTED};")

        scroll_body = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(scroll_body)
        body.setContentsMargins(4, 4, 4, 4)
        body.addWidget(self.dataLabel)
        body.addWidget(self.setupBox)
        body.addWidget(self.resultBox)
        body.addWidget(self.beadsSection)
        body.addWidget(self.fitRow)
        body.addWidget(self.summaryLabel)
        body.addStretch()
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setWidget(scroll_body)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

        self.fitButton.clicked.connect(self.run)
        self.previewButton.clicked.connect(self.preview)
        self.clearPreviewButton.clicked.connect(self.clearPreview)
        self.beadTable.itemSelectionChanged.connect(self._jumpToSelectedBead)
        self.applySelectionButton.clicked.connect(self.applySelection)
        self.autoButton.clicked.connect(self.resetSelection)
        for key in ("pixel_size_nm", "z_step_nm"):
            self.form.controls[key].valueChanged.connect(self._updateDataLabel)
        for key in ("na", "wavelength_nm"):
            self.form.controls[key].valueChanged.connect(self._updateRequirements)
        self.form.controls["fit_aberrations"].toggled.connect(self._updateRequirements)
        for key in _SELECTION_KEYS:
            self.selectForm.controls[key].valueChanged.connect(self._selectionChanged)
        if self.region is not None:
            self.region.sigRegionChangeFinished.connect(self._regionToSpins)

    # ------------------------------------------------------------------ #
    # Result-processor widget contract
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        """Measure the PSF of the selected image."""
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
        self.summaryLabel.setText("Fitting…")
        QtWidgets.QApplication.processEvents()
        with _busy():
            self.sigRunRequested.emit(self._currentResult, params)

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
        self._previewing = True
        self._beadRun = run
        self._loadHistogram(run)
        self._showBeads(run)
        self.summaryLabel.setText(self._readyText)

    def clearPreview(self) -> None:
        self._previewing = False
        self._removePreviewLayer()
        if not self._isMeasurement(self._currentResult):
            self._beadRun = None
            self.beadTable.setRowCount(0)
            self.beadsSection.setVisible(False)

    def applySelection(self) -> None:
        """Re-select the beads of the selected measurement (a new measurement)."""
        if not self._isMeasurement(self._currentResult):
            self.summaryLabel.setText("Select a PSF measurement to re-select its beads.")
            return
        self.processor = self.selectProcessor
        self.summaryLabel.setText("Re-selecting…")
        with _busy():
            self.sigRunRequested.emit(self._currentResult, self.selectForm.get_values())

    def resetSelection(self) -> None:
        """Back to the automatic selection (all bounds 0, default thresholds)."""
        self._setSelection({f.key: f.default for f in SELECTION_FIELDS})
        self._selectionChanged()

    def parameterValues(self) -> dict:
        """The fit parameters (setup and selection), with the ROI Manager's
        ROIs for the ROI source.

        Keys match ``PSFResolutionProcessor``'s parameter contract (``rois``
        being its one extra key)."""
        params = {**self.form.get_values(), **self.selectForm.get_values()}
        if params.get("source") == "rois":
            if self._roiManagerWidget is None:
                raise ValueError("ROI Manager panel is not enabled.")
            rois = self._roiManagerWidget.rois()
            if not rois:
                raise ValueError("ROI Manager has no ROIs.")
            params["rois"] = rois
        return params

    def setCurrentResult(self, result) -> None:
        """Show the setup for an image, the result card for a measurement."""
        if result is not self._currentResult:
            # A preview describes the result it was computed on.
            self._previewing = False
            self._beadRun = None
            self._removePreviewLayer()
            self.beadTable.setRowCount(0)
            if self._isMeasurement(result) or self._isMeasurement(self._currentResult):
                # A measurement brings its own selection; leaving one restores
                # the automatic selection rather than keeping its bounds.
                self._setSelection(self._selectionOf(result))
        self._currentResult = result
        measurement = self._isMeasurement(result)
        fitting = not measurement and self._fitAccepts(result)
        self.setupBox.setVisible(fitting)
        self.fitRow.setVisible(fitting)
        self.fitButton.setEnabled(fitting)
        self.previewButton.setEnabled(fitting)
        self.resultBox.setVisible(measurement)
        self.applySelectionButton.setVisible(measurement)
        self.processor = self.selectProcessor if measurement else self.fitProcessor
        self._updateDataLabel()
        if measurement:
            self._beadRun = result.run
            self._showCard(result)
            self._loadHistogram(result.run)
            self._showBeads(result.run)
            self.summaryLabel.setText("")
        elif fitting:
            self.beadsSection.setVisible(False)
            self.summaryLabel.setText(self._readyText)
        else:
            self.beadsSection.setVisible(False)
            self.summaryLabel.setText(self._incompatibleText if result is not None else self._idleText)

    def setStatusText(self, text: str) -> None:
        """The controller's run status."""
        self.summaryLabel.setText(text)

    def setRoiManagerWidget(self, roiManagerWidget) -> None:
        """Wire (or rewire) the ROI Manager dependency at runtime."""
        self._roiManagerWidget = roiManagerWidget

    # ------------------------------------------------------------------ #
    # Data / calibration line
    # ------------------------------------------------------------------ #
    def _updateDataLabel(self, *_args) -> None:
        from imswitch.improcess.processors.psf_resolution.processor import input_layout

        self._updateRequirements()
        result = self._currentResult
        if self._isMeasurement(result):
            self.dataLabel.setText(
                f"<span style='color:{_MUTED}'>PSF measurement of <b>{_html(result.source_name)}</b>: "
                f"{_html(result.run.layout.describe())}</span>"
            )
            return
        if result is None or not self._fitAccepts(result):
            self.dataLabel.setText("")
            return
        try:
            layout = input_layout(result, self.form.get_values())
        except Exception as exc:
            self.dataLabel.setText(f"Data: {exc}")
            return
        uncalibrated = layout.pixel_size is None
        color = _WARN if uncalibrated or layout.note else _MUTED
        text = f"<span style='color:{color}'>Data: {layout.describe()}"
        if uncalibrated:
            text += (" Physical widths, the diffraction-limit comparison and aberrations need the "
                     "pixel size (and z step): fix the metadata or open 'Calibration override'.")
        self.dataLabel.setText(text + "</span>")
        section = getattr(self.form, "sections", {}).get(CALIBRATION_GROUP)
        if section is not None and uncalibrated and not section.isExpanded():
            section.setExpanded(True)

    def _updateRequirements(self, *_args) -> None:
        """Say what an aberration fit still needs, while the box is ticked."""
        from imswitch.improcess.processors.psf_resolution.processor import (
            aberration_requirements,
            input_layout,
        )

        values = self.form.get_values()
        wanted = bool(values.get("fit_aberrations"))
        missing = ""
        result = self._currentResult
        if wanted and result is not None and self._fitAccepts(result) and not self._isMeasurement(result):
            try:
                missing = aberration_requirements(input_layout(result, values), values)
            except Exception:
                missing = ""
        self.requirementLabel.setText(f"⚠ Aberrations will not be estimated: {missing}." if missing else "")
        self.requirementLabel.setVisible(bool(missing))
        for key in ("na", "wavelength_nm"):
            unset = wanted and not values.get(key)
            self.form.controls[key].setStyleSheet(f"border: 1px solid {_WARN};" if unset else "")

    # ------------------------------------------------------------------ #
    # Beads: selection, list, markers
    # ------------------------------------------------------------------ #
    def _selectionChanged(self, *_args) -> None:
        """Re-select the shown beads with the current bounds (no refitting)."""
        from imswitch.improcess.processors.psf_resolution.processor import reselect

        if self._beadRun is None:
            self._spinsToRegion()
            return
        self._beadRun = reselect(self._beadRun, self.selectForm.get_values())
        self._showBeads(self._beadRun)

    def _showBeads(self, run) -> None:
        """Count, list and (for a preview) mark the beads of ``run``."""
        from imswitch.improcess.processors.psf_resolution.result import psf_report

        self.beadsSection.setVisible(True)
        n, total = int(run.mask.sum()), len(run.analysis.beads)
        self.countLabel.setText(f"<b>{n}</b> of {total} bead candidates selected.")
        self._fillBeadTable(run)
        self._spinsToRegion()
        measurement = self._currentResult if self._isMeasurement(self._currentResult) else None
        if measurement is not None:
            changed = not np.array_equal(run.mask, measurement.mask)
            self.applySelectionButton.setEnabled(changed)
            self.previewReportLabel.setText(
                "The selection differs from this measurement's: Re-select measures again with it."
                if changed else ""
            )
            return
        text = psf_report(run.summary(), count=False)
        if self._previewing and not self._drawPreview(run) and self._viewer is not None:
            text += "\n(No image layer of this result is shown, so the beads are not marked.)"
        self.previewReportLabel.setText(text)

    def _fillBeadTable(self, run) -> None:
        analysis = run.analysis
        unit = analysis.unit
        stack_axis = run.layout.stack_axis
        self.beadTable.setHorizontalHeaderItem(
            2, QtWidgets.QTableWidgetItem("plane" if stack_axis is not None else "z")
        )
        order = sorted(range(len(analysis.beads)), key=lambda i: (run.reasons[i] != "", i))
        self.beadTable.blockSignals(True)
        self.beadTable.setRowCount(len(order))
        for row, index in enumerate(order):
            bead, reason = analysis.beads[index], run.reasons[index]
            z = bead.get("z", bead.get("z_det", bead.get("plane")))
            y = bead.get("y", bead.get("y_det"))
            x = bead.get("x", bead.get("x_det"))
            lat = bead.get("fwhm_lat_hm", bead.get("fwhm_lat"))
            axial = bead.get("fwhm_z_hm", bead.get("fwhm_z"))
            widths = "" if lat is None else _fmt(lat, unit) + ("" if axial is None else f" / {_fmt(axial, unit)}")
            cells = [
                str(bead["id"]), "selected" if not reason else STATE_TAGS.get(reason, reason),
                "" if z is None else f"{z:.1f}", f"{y:.1f}", f"{x:.1f}", widths,
            ]
            for column, value in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(value)
                item.setData(QtCore.Qt.UserRole, index)
                if column == 1:
                    item.setForeground(QtGui.QBrush(QtGui.QColor(STATE_COLORS.get(reason, "#ff4d4d"))))
                    item.setToolTip(_reason_text(reason, bead))
                self.beadTable.setItem(row, column, item)
        self.beadTable.blockSignals(False)
        self.beadTable.resizeColumnsToContents()

    def _drawPreview(self, run) -> bool:
        """Mark every candidate on the image layer of the current result."""
        self._removePreviewLayer()
        viewer = self._viewer
        layer = self._sourceLayer()
        if viewer is None or layer is None or not run.analysis.beads:
            return False
        points = np.array([self._displayCoords(run, bead, layer) for bead in run.analysis.beads], dtype=float)
        colors = [STATE_COLORS.get(reason, "#ff4d4d") for reason in run.reasons]
        labels = [
            f"{bead['id']}" if not reason else f"{bead['id']} {STATE_TAGS.get(reason, reason)}"
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
        elif run.layout.stack_axis is not None:
            coords[run.layout.stack_axis] = float(bead.get("plane", 0))
        transpose = _view_transpose(self._currentResult, layer, ndim)
        displayed = [coords[axis] for axis in transpose]
        layer_ndim = int(getattr(layer, "ndim", len(displayed)))
        return displayed[-layer_ndim:] if layer_ndim <= len(displayed) else [0.0] * (layer_ndim - len(displayed)) + displayed

    def _jumpToSelectedBead(self) -> None:
        run = self._beadRun
        items = self.beadTable.selectedItems()
        if run is None or not items or self._viewer is None:
            return
        index = int(items[0].data(QtCore.Qt.UserRole))
        bead = run.analysis.beads[index]
        result = self._currentResult
        if self._isMeasurement(result):
            # The measurement's own layers: the analysed image at the origin.
            _image, _labels, scales, _unit = result._display_image()
            axes = run.analysis.axes[-len(scales):]
            world = np.array([float(bead.get(ax, bead.get(f"{ax}_det"))) for ax in axes]) * np.asarray(scales)
            markers = self._layerNamed(f"{result.name} beads")
        else:
            layer = self._sourceLayer()
            if layer is None:
                return
            data_coords = np.asarray(self._displayCoords(run, bead, layer), dtype=float)
            world = data_coords * np.asarray(layer.scale, dtype=float) + np.asarray(layer.translate, dtype=float)
            markers = self._previewLayer()
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
        if markers is not None:
            try:
                markers.selected_data = {index}
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

    def _layerNamed(self, name: str):
        try:
            for layer in self._viewer.layers:
                if getattr(layer, "name", "") == name:
                    return layer
        except Exception:
            pass
        return None

    def _previewLayer(self):
        return self._layerNamed(PREVIEW_LAYER_NAME)

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
    # Selection histogram and bounds
    # ------------------------------------------------------------------ #
    def _make_histogram(self):
        try:
            import pyqtgraph as pg
        except Exception:  # pragma: no cover - pyqtgraph is a dependency
            return None, None
        plot = pg.PlotWidget()
        plot.setMinimumHeight(120)
        plot.setMaximumHeight(160)
        plot.setLabel("left", "Beads")
        plot.setMouseEnabled(x=False, y=False)
        plot.setToolTip("Lateral FWHM of the fitted beads. Drag the shaded range to choose which are used.")
        region = pg.LinearRegionItem(values=(0.0, 1.0))
        plot.addItem(region)
        plot._curve = plot.plot([0.0, 1.0], [0.0], stepMode="center", fillLevel=0, brush=(120, 120, 120, 120))
        return plot, region

    def _loadHistogram(self, run) -> None:
        if self.histogram is None:
            return
        values = np.array([b["fwhm_lat"] for b in run.analysis.fitted()], dtype=np.float64)
        if values.size:
            counts, edges = np.histogram(values, bins=max(10, min(60, values.size // 2)))
            self.histogram._curve.setData(edges, counts)
            self.region.setBounds((0.0, 2.0 * float(edges[-1])))
        else:
            self.histogram._curve.setData([0.0, 1.0], [0.0])
        self.histogram.setLabel("bottom", f"Lateral FWHM, Gaussian fit ({run.analysis.unit})")
        self._spinsToRegion()

    def _selectionOf(self, result) -> dict:
        """The selection a measurement was made with (else the automatic one)."""
        params = result.params if self._isMeasurement(result) else {}
        return {f.key: params.get(f.key, f.default) for f in SELECTION_FIELDS}

    def _setSelection(self, values: dict) -> None:
        """Set the bounds without re-selecting once per field."""
        controls = self.selectForm.controls
        for control in controls.values():
            control.blockSignals(True)
        try:
            self.selectForm.set_values(values)
        finally:
            for control in controls.values():
                control.blockSignals(False)

    def _spinsToRegion(self, *_args) -> None:
        """Shade the lateral range in use: the bounds, or the automatic range."""
        if self.region is None:
            return
        lo = self.selectForm.controls["fwhm_lat_min"].value()
        hi = self.selectForm.controls["fwhm_lat_max"].value()
        if not hi > lo and self._beadRun is not None:
            from imswitch.improcess.analysis.bead_psf import resolve_selection

            lo, hi = resolve_selection(self._beadRun.analysis, self._beadRun.selection).fwhm_lat_range
        if np.isfinite(lo) and np.isfinite(hi) and hi > lo:
            self.region.blockSignals(True)
            self.region.setRegion((max(lo, 0.0), hi))
            self.region.blockSignals(False)

    def _regionToSpins(self, *_args) -> None:
        lo, hi = self.region.getRegion()
        self._setSelection({"fwhm_lat_min": max(float(lo), 0.0), "fwhm_lat_max": max(float(hi), 0.0)})
        self._selectionChanged()

    # ------------------------------------------------------------------ #
    # Result card
    # ------------------------------------------------------------------ #
    def _showCard(self, measurement) -> None:
        self.headlineLabel.setText(_headline_html(measurement))
        self.reportLabel.setText(measurement.report(headline=False))
        self.thumbnails.show_measurement(measurement)
        self._fillSummary(measurement)
        fit = measurement.aberrations
        self.statsTabs.setTabVisible(1, fit is not None)
        self.zernikeTable.setRowCount(0)
        if fit is not None:
            self._fillZernike(fit)

    def _fillSummary(self, measurement) -> None:
        rows = measurement.summary_rows()
        self.summaryTable.setRowCount(len(rows))
        for r, row in enumerate(rows):
            unit = row.get("unit") or ""
            if row.get("n"):
                value = f"{_num(row['median'])} ± {_num(row['mad'])} {unit} (n={row['n']})"
            elif row.get("value") is not None:
                value = f"{_num(row['value'])} {unit}".strip()
            else:
                value = "–"
            cells = [row["metric"], value, _num(row.get("averaged_psf")), _num(row.get("theory"))]
            for c, text in enumerate(cells):
                self.summaryTable.setItem(r, c, QtWidgets.QTableWidgetItem(text))
        self.summaryTable.resizeColumnsToContents()

    def _fillZernike(self, fit) -> None:
        rows = fit.rows()
        self.zernikeTable.setRowCount(len(rows))
        for r, row in enumerate(rows):
            cells = [f"Z{row['noll']} {row['mode']}", f"{row['coefficient_nm_rms']:+.1f}",
                     f"{row['error_nm_rms']:.1f}", f"{row['milliwaves']:+.0f}"]
            for c, text in enumerate(cells):
                self.zernikeTable.setItem(r, c, QtWidgets.QTableWidgetItem(text))
        self.zernikeTable.resizeColumnsToContents()

    # ------------------------------------------------------------------ #
    def _fitAccepts(self, result) -> bool:
        return self._accepts(self.fitProcessor, result)

    def _isMeasurement(self, result) -> bool:
        return self._accepts(self.selectProcessor, result)

    @staticmethod
    def _accepts(processor, result) -> bool:
        if result is None:
            return False
        try:
            return bool(processor.accepts(result))
        except Exception:
            return False


class _Thumbnails(QtWidgets.QWidget):
    """Small images of a measurement: the averaged PSF (XY, XZ), the
    wavefront and the aberration fit (data | model, XY and XZ)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.titles: list[str] = []
        try:
            import pyqtgraph as pg
        except Exception:  # pragma: no cover - pyqtgraph is a dependency
            self._pg = None
            return
        self._pg = pg
        self.view = pg.GraphicsLayoutWidget()
        self.view.setMinimumHeight(130)
        self.view.setMaximumHeight(170)
        layout.addWidget(self.view)

    def show_measurement(self, measurement) -> None:
        self.titles = []
        if self._pg is None:
            return
        self.view.clear()
        px = [float(v) for v in measurement.run.pixel_size]
        averaged = measurement.averaged
        if averaged is not None:
            image = np.asarray(averaged.image, dtype=np.float32)
            if image.ndim == 3:
                cz, cy, _cx = np.array(image.shape) // 2
                self._add("Averaged PSF XY", image[cz], (px[-2], px[-1]), "viridis")
                self._add("XZ", image[:, cy, :], (px[0], px[-1]), "viridis")
            else:
                self._add("Averaged PSF", image, (px[-2], px[-1]), "viridis")
        fit = measurement.aberrations
        if fit is not None:
            wavefront = fit.wavefront(65)
            bound = float(np.nanmax(np.abs(wavefront))) or 1.0
            self._add("Wavefront", wavefront, (1.0, 1.0), "CET-D1", (-bound, bound))
            data, model = np.asarray(fit.data, np.float32), np.asarray(fit.model, np.float32)
            fpx = [float(v) for v in (fit.pixel_size_nm or (1.0, 1.0, 1.0))]
            cz, cy, _cx = np.array(data.shape) // 2
            levels = (float(np.nanmin(data)), float(np.nanmax(data)))
            self._add("Data | model XY", _side_by_side(data[cz], model[cz]), (fpx[1], fpx[2]), "viridis", levels)
            self._add("XZ", _side_by_side(data[:, cy, :], model[:, cy, :]), (fpx[0], fpx[2]), "viridis", levels)
        self.setVisible(bool(self.titles))
        self.setToolTip("Centre slices; XZ views have z downwards. The wavefront spans the pupil, "
                        f"±{float(np.nanmax(np.abs(fit.wavefront(65)))):.0f} nm." if fit is not None
                        else "Centre slices; XZ views have z downwards.")

    def _add(self, title, image, pixel, cmap, levels=None) -> None:
        pg = self._pg
        image = np.nan_to_num(np.asarray(image, dtype=np.float32), nan=0.0)
        column = len(self.titles)
        box = self.view.addViewBox(row=0, col=column, lockAspect=True, enableMouse=False)
        box.invertY(True)
        item = pg.ImageItem(image, axisOrder="row-major")
        try:
            item.setColorMap(pg.colormap.get(cmap))
        except Exception:
            pass
        if levels is not None:
            item.setLevels(levels)
        item.setRect(QtCore.QRectF(0, 0, image.shape[1] * pixel[1], image.shape[0] * pixel[0]))
        box.addItem(item)
        box.autoRange(padding=0.02)
        self.view.addLabel(title, row=1, col=column, size="7pt")
        self.titles.append(title)


def _side_by_side(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    gap = np.full((a.shape[0], 1), float(np.nanmin(a)), dtype=np.float32)
    return np.concatenate([a, gap, b], axis=1)


def _table(headers: list[str]) -> QtWidgets.QTableWidget:
    table = QtWidgets.QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(20)
    table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _headline_html(measurement) -> str:
    """Lateral and axial width in large type, the breakdown and the
    diffraction limit beneath, then the aberration total."""
    from imswitch.improcess.processors.psf_resolution.result import _fmt_width

    unit, stats = measurement.unit, measurement.summary["stats"]
    n = measurement.summary["n_selected"]
    if not n:
        return f"<b>No bead was selected</b> ({measurement.summary['n_candidates']} candidates)."

    def width(key, part="median"):
        return _fmt_width(stats.get(key, {}).get(part), unit)

    big = [f"lateral {width('fwhm_lat_hm')}"]
    if measurement.analysis.ndim == 3:
        big.append(f"axial {width('fwhm_z_hm')}")
    parts = [f"{ax} {width(f'fwhm_{ax}_hm')} ± {width(f'fwhm_{ax}_hm', 'mad')}"
             for ax in measurement.analysis.axes[::-1]]
    lines = [
        f"<span style='font-size:13pt'><b>FWHM {' · '.join(big)}</b></span>",
        f"<span style='color:{_MUTED}'>{', '.join(parts)}: half maximum, median ± MAD of {n} beads</span>",
    ]
    theory = measurement.summary.get("theory_fwhm_nm")
    if theory:
        limit = f"lateral {_fmt_width(theory['lat'], 'nm')}"
        if measurement.analysis.ndim == 3 and np.isfinite(theory.get("z", np.nan)):
            limit += f", axial {_fmt_width(theory['z'], 'nm')}"
        ratios = " / ".join(f"{r:.2f}×" for r in measurement.summary.get("ratio_to_theory", {}).values())
        lines.append(f"<span style='color:{_MUTED}'>Diffraction limit {limit}"
                     + (f" (measured {ratios})" if ratios else "") + "</span>")
    fit = measurement.aberrations
    if fit is not None:
        lines.append(f"<b>Aberrations {fit.rms_nm:.0f} nm RMS · Strehl ≈ {fit.strehl:.2f}</b>")
    elif measurement.summary.get("aberrations_skipped"):
        lines.append(f"<span style='color:{_MUTED}'>Aberrations not estimated: "
                     f"{_html(measurement.summary['aberrations_skipped'])}.</span>")
    if measurement.warnings:
        lines.append(f"<span style='color:{_WARN}'>⚠ {len(measurement.warnings)} warning(s), listed below</span>")
    return "<br>".join(lines)


def _html(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _num(value) -> str:
    if value is None:
        return ""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not np.isfinite(value):
        return "–"
    return f"{value:.3g}" if abs(value) < 10 else f"{value:.0f}"


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
        f"<span style='color:{STATE_COLORS[key]}'>●</span>&nbsp;{label}" for key, label in entries
    )
