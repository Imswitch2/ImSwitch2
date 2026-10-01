"""Interactive PSF / bead resolution panel for ImProcess.

Producing panel: the buttons run a processor on the selected result through
the generic run->publish pipeline (sigRunRequested ->
ResultProcessorController -> sigResultProduced). ``self.processor`` is the
processor the controller runs, so the panel points it at whichever action
the user took:

* an image is selected -> **Fit** runs ``psf-resolution`` with the form's
  parameters (plus the ROI Manager's ROIs when that is the source);
* a bead table (``psf-resolution``'s ``beads`` output) is selected ->
  **Apply selection** runs ``psf-bead-select``. The histogram shows the
  fitted lateral FWHMs; dragging the shaded range sets the lateral FWHM
  bounds, which stay editable in the form below it.
"""

from __future__ import annotations

import numpy as np
from qtpy import QtCore, QtWidgets

from imswitch.improcess.processors import PSFBeadSelectProcessor, PSFResolutionProcessor


class PSFResolutionWidget(QtWidgets.QWidget):
    """Fit bead PSFs on the selected image, or re-select a fitted bead table."""

    sigRunRequested = QtCore.Signal(object, dict)

    def __init__(self, napariViewer, roiManagerWidget=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._roiManagerWidget = roiManagerWidget
        self._currentResult = None
        self.fitProcessor = PSFResolutionProcessor()
        self.selectProcessor = PSFBeadSelectProcessor()
        self.processor = self.fitProcessor

        self._selectText = (
            "Select an image (beads) to fit, or a bead table to re-select. "
            "Results appear in the results list and table."
        )
        self._readyText = "Ready to fit PSF resolution."
        self._incompatibleText = "Selected result is not compatible with PSF resolution."
        self._selectionText = "Bead table selected: drag the range or edit the bounds, then apply."

        # --- fit mode ---------------------------------------------------- #
        self.form = self.fitProcessor.make_param_widget(self)
        self.sourceCombo = self.form.controls["source"]
        self.fitButton = QtWidgets.QPushButton("Fit")
        self.fitButton.setEnabled(False)
        self.fitBox = QtWidgets.QWidget(self)
        fit_layout = QtWidgets.QVBoxLayout(self.fitBox)
        fit_layout.setContentsMargins(0, 0, 0, 0)
        fit_layout.addWidget(self.form)
        fit_layout.addWidget(self.fitButton)

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
        self.summaryLabel.setStyleSheet("color:#888; font-size:8pt;")

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
        self.applySelectionButton.clicked.connect(self.applySelection)
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
        self.sigRunRequested.emit(self._currentResult, params)

    def applySelection(self) -> None:
        """Re-select the beads of the selected bead table."""
        if self._currentResult is None or not self._selectAccepts(self._currentResult):
            self.summaryLabel.setText("Select a PSF bead table to re-select beads.")
            return
        self.processor = self.selectProcessor
        self.sigRunRequested.emit(self._currentResult, self.selectForm.get_values())

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
        self._currentResult = result
        selecting = self._selectAccepts(result)
        fitting = self._fitAccepts(result)
        self.fitBox.setVisible(not selecting)
        self.selectionBox.setVisible(selecting)
        self.fitButton.setEnabled(fitting)
        self.processor = self.selectProcessor if selecting else self.fitProcessor
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
        self.summaryLabel.setText(text)

    def setRoiManagerWidget(self, roiManagerWidget) -> None:
        """Wire (or rewire) the ROI Manager dependency at runtime."""
        self._roiManagerWidget = roiManagerWidget

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
            self.histogram.setLabel("bottom", f"Lateral FWHM ({analysis.unit})")
            self.region.setBounds((0.0, 2.0 * float(edges[-1])))
        current = {
            "max_ellipticity": selection.max_ellipticity,
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
