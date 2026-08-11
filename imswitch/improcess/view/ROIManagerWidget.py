"""ImageJ-like ROI manager panel for ImProcess."""

from __future__ import annotations

import csv
import json
import uuid
from dataclasses import replace
from pathlib import Path

import numpy as np
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view.guitools.naparitools import NapariROISetOverlay
from imswitch.imcommon.view.guitools.viewer_tools import ViewerToolService
from imswitch.improcess.analysis.roi_frame_adapter import world_to_data
from imswitch.improcess.layer_selection import active_image_layer
from imswitch.imcommon.algorithms.roi_style import ROIStyle
from imswitch.improcess.analysis.roi_commands import (
    AddROI,
    ClearROIs,
    CommandLog,
    DeleteROI,
    ImportROIs,
    RemoveSliceInfo,
    RenameROI,
    ReplaceROIs,
    SetProperties,
    SetVisible,
    UpdateROI,
)
from imswitch.imcommon.algorithms.roi_geometry import roi_from_points
from imswitch.imcommon.algorithms.roi_ops import (
    ROIOperationError,
    combine,
    convex_hull,
    enlarge,
    labels_from_rois,
    make_band,
    make_inverse,
    rescale_to_frame,
    rois_from_labels,
    split,
    to_bounding_box,
    translate,
)
from imswitch.improcess.model.roi_mask_result import ROIMaskResult
from imswitch.improcess.analysis.roi_frame_adapter import (
    frame_from_layer,
    frame_from_result,
    plane_position,
    plane_scales,
)
from imswitch.imcommon.algorithms.spatial_frame import (
    OPT_IN_MEASURABLE,
    compatibility,
    is_auto_measurable,
)
from imswitch.imcommon.algorithms.roi_set import (
    MeasurementConfig,
    ROISet,
    compare_sets,
    merge_sets,
)
from imswitch.imcommon.algorithms.roi_set_io import (
    ROISetFormatError,
    read_set,
    write_set,
)
from imswitch.improcess.model.roi_persistence import (
    sets_from_payload,
    sets_payload,
)
from imswitch.imcommon.algorithms.roi_imagej import (
    available as imagej_available,
    read_imagej,
    write_imagej,
)
from imswitch.improcess.analysis.roi_manager import (
    ROIManagerModel,
    ROIRecord,
    measure_roi,
    roi_from_shape,
)
from imswitch.improcess.analysis.roi_measurements import (
    applicable_selection,
    column_label,
    selected_measurements,
)
from imswitch.improcess.analysis.roi_jobs import (
    ArrayPlaneSource,
    LazyPlaneSource,
    LiveDataError,
    MeasurementCache,
    MeasurementJob,
    MeasurementRunner,
    cache_key,
    planes_for_axis,
    stack_axis_labels,
)
from imswitch.improcess.analysis.roi_report import (
    IDENTITY_COLUMNS,
    measurement_row,
    multi_plot_payload,
    roi_belongs_on_plane,
    rows_to_columns,
    wide_form,
)
from .ResultsTableWidget import format_table_value
from .ROIMeasurementsDialog import ROIMeasurementsDialog
from .ROIPropertiesDialog import ROIPropertiesDialog, ROISpecifyDialog
from .ROIPreflightDialog import ROIPreflightDialog

#: Verdict for an ROI whose capture frame the set does not know — an ROI that
#: predates provenance, or one imported from a file. Deliberately not a
#: compatibility verdict: "we did not check" must not read as "it matched".
UNVERIFIED = "unverified"

#: Role holding the ROI's **uid** on column 0 of every row. Rows resolve
#: through this, never through their index or name: sorting reorders rows,
#: and a name is display text the user can edit or import a duplicate of, so
#: neither can be trusted to identify the ROI an action was aimed at.
ROI_KEY_ROLE = QtCore.Qt.UserRole

#: Role holding a value the table sorts on, so numeric columns sort
#: numerically rather than by their formatted text.
SORT_KEY_ROLE = QtCore.Qt.UserRole + 1


class _TableItem(QtWidgets.QTableWidgetItem):
    """Table item that sorts on a stored key rather than its display text."""

    def __lt__(self, other):
        mine = self.data(SORT_KEY_ROLE)
        theirs = other.data(SORT_KEY_ROLE) if isinstance(other, QtWidgets.QTableWidgetItem) else None
        if mine is None or theirs is None:
            return super().__lt__(other)
        try:
            return mine < theirs
        except TypeError:
            return super().__lt__(other)


class ROIManagerWidget(QtWidgets.QWidget):
    """Manage multiple rectangular ROIs and per-ROI statistics."""

    #: Rows for the shared Results dock: ``(columns, records)``.
    sigResultPushed = QtCore.Signal(object, object)
    #: A curve for the shared Graph panel.
    sigPlotPushed = QtCore.Signal(object)
    #: A label image built from the ROI set, for the reconstruction list.
    sigResultProduced = QtCore.Signal(object, str)
    #: The sets changed and are worth saving. The controller owns *where* they
    #: go; the panel only says that they moved.
    sigStateChanged = QtCore.Signal()
    #: Internal: a finished measurement job, carried from the worker thread to
    #: the GUI thread. A queued signal is the marshalling `MeasurementRunner`
    #: requires — its callbacks run on the worker, and touching a widget from
    #: there is a crash rather than a warning.
    sigJobFinished = QtCore.Signal(object)
    sigJobProgress = QtCore.Signal(int, int)
    sigJobDiscarded = QtCore.Signal(object)

    #: Stable owner key for the shared drawing tool (see ViewerToolService).
    TOOL_OWNER = "improcess.roi-manager"

    #: An image with no mutation token is snapshotted so a run measures one
    #: coherent moment (A-22). Above this the copy is refused instead: a
    #: snapshot larger than this is more likely to take the session down than
    #: to finish, and refusing says so rather than trying.
    MAX_SNAPSHOT_BYTES = 2 * 1024**3

    #: How long after the last edit an autosave fires. Long enough that
    #: drawing ten ROIs in a row costs one save, short enough that a crash
    #: loses seconds of work rather than a session's.
    AUTOSAVE_DEBOUNCE_MS = 5000

    #: How long a burst of viewer events is allowed to coalesce before the
    #: table is re-measured. Long enough that dragging a slider costs one
    #: measurement pass, short enough that a single step feels immediate.
    REFRESH_DEBOUNCE_MS = 120

    #: The columns that are not measurements: identity in front, the reason a
    #: row is empty at the end. Everything between them comes from the
    #: measurement registry and changes with Set Measurements.
    _FIXED_LEADING = ["Visible", "Name", "Type"]
    _FIXED_TRAILING = ["Note"]

    @property
    def _set(self) -> ROISet:
        """The active set. Assigning to it writes back into the collection."""
        return self._sets[self._activeIndex]

    @_set.setter
    def _set(self, value: ROISet) -> None:
        self._sets[self._activeIndex] = value

    def __init__(self, napariViewer, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        self._toolService = ViewerToolService.for_viewer(napariViewer)
        # Register only; the tool is acquired when Draw Rectangle is used.
        self._toolToken = self._toolService.register(self.TOOL_OWNER)
        self._model = ROIManagerModel()
        # Every model change goes through the log, so there is one audited
        # path and undo (P-U) has a history to work from.
        self._commands = CommandLog(self._model)
        self._stats_rows: list[dict[str, object]] = []
        # The sets. One is active; `self._set` is a property onto it, so
        # every existing `self._set = ...` writes back into the collection and
        # there is no second copy to keep in step.
        self._sets: list[ROISet] = [ROISet(name="ROIs")]
        self._activeIndex = 0
        # Rows already measured, keyed on everything that can change an answer
        # (roi_jobs.cache_key). Scrolling a stack and coming back is then free,
        # and an image with no trustworthy mutation token is never cached.
        self._cache = MeasurementCache()
        # One runner per panel, sharing the panel's cache: a Multi Measure that
        # crosses a plane the table already measured pays nothing for it.
        self._runner = MeasurementRunner(cache=self._cache)
        #: The last Multi Measure's rows and the axis it stepped, which is what
        #: Multi Plot draws.
        self._multiRows: list[dict] = []
        self._multiAxisLabel: str = ""
        #: Per-ROI failures from the running job. Appended from the worker
        #: (a list append is atomic) and read on the GUI thread once the run
        #: has ended, which is the only point either job callback fires.
        self._jobFailures: list[str] = []
        #: The loaded results, kept current through sigResultsChanged (C-11).
        self._availableResults: list = []
        #: Optional Segmentation panel, for seeding the measurement threshold.
        self._segmentationWidget = None
        self._refreshTimer = QtCore.QTimer(self)
        self._refreshTimer.setSingleShot(True)
        self._refreshTimer.setInterval(self.REFRESH_DEBOUNCE_MS)
        self._refreshTimer.timeout.connect(self.refresh_stats)
        self._autosaveTimer = QtCore.QTimer(self)
        self._autosaveTimer.setSingleShot(True)
        self._autosaveTimer.setInterval(self.AUTOSAVE_DEBOUNCE_MS)
        self._autosaveTimer.timeout.connect(self._autosave)
        # The committed ROI set, drawn read-only in the viewer (P-1).
        self._overlay = NapariROISetOverlay(napariViewer)

        self.addRectangleButton = QtWidgets.QPushButton("Draw Rectangle")
        self.addRectangleButton.setToolTip("Switch the viewer tool to rectangle drawing")
        self.addPointsButton = QtWidgets.QPushButton("Draw Points")
        self.addPointsButton.setToolTip(
            "Switch the viewer tool to placing points; Add Shape then captures "
            "them as one multipoint ROI"
        )
        self.captureButton = QtWidgets.QPushButton("Add Shape")
        self.captureButton.setToolTip("Add the first rectangle from the current shapes layer")
        self.renameButton = QtWidgets.QPushButton("Rename")
        self.duplicateButton = QtWidgets.QPushButton("Duplicate")
        self.deleteButton = QtWidgets.QPushButton("Delete")
        self.clearButton = QtWidgets.QPushButton("Clear")
        self.refreshButton = QtWidgets.QPushButton("Refresh Stats")
        self.showAllCheck = QtWidgets.QCheckBox("Show All")
        self.showAllCheck.setChecked(True)
        self.showAllCheck.setToolTip("Draw every visible ROI in the viewer")
        self.labelsCheck = QtWidgets.QCheckBox("Labels")
        self.labelsCheck.setToolTip("Label each drawn ROI with its name")
        self.associateSlicesCheck = QtWidgets.QCheckBox("Associate with slices")
        self.associateSlicesCheck.setToolTip(
            "Record the slice each ROI was drawn on. Off by default, as in "
            "ImageJ, where an ROI applies to every slice."
        )
        self.removeSliceInfoButton = QtWidgets.QPushButton("Remove Slice Info")
        self.removeSliceInfoButton.setToolTip(
            "Detach every ROI from the slice it was drawn on"
        )
        self.undoButton = QtWidgets.QPushButton("Undo")
        self.redoButton = QtWidgets.QPushButton("Redo")
        for button in (self.undoButton, self.redoButton):
            button.setEnabled(False)
        self.measurementsButton = QtWidgets.QPushButton("Measurements…")
        self.measurementsButton.setToolTip(
            "Choose which measurements are taken (ImageJ's Set Measurements)"
        )
        self.measureButton = QtWidgets.QPushButton("Measure")
        self.measureButton.setToolTip(
            "Push the selected ROI — or every ROI, if none is selected — to "
            "the Results table"
        )
        self.multiMeasureButton = QtWidgets.QPushButton("Multi Measure")
        self.multiMeasureButton.setToolTip(
            "Measure every ROI on every slice of a stack axis"
        )
        self.multiPlotButton = QtWidgets.QPushButton("Multi Plot")
        self.multiPlotButton.setToolTip(
            "Plot the last Multi Measure: one curve per ROI along the stack axis"
        )
        self.multiPlotButton.setEnabled(False)
        self.acrossResultsButton = QtWidgets.QPushButton("Across Results")
        self.acrossResultsButton.setToolTip(
            "Measure this ROI set on every loaded result, gated by spatial "
            "compatibility"
        )
        self.acrossResultsButton.setEnabled(False)
        self.cancelButton = QtWidgets.QPushButton("Cancel")
        self.cancelButton.setToolTip("Stop the running measurement")
        self.cancelButton.setVisible(False)
        self.progressBar = QtWidgets.QProgressBar()
        self.progressBar.setVisible(False)
        self.progressBar.setTextVisible(True)
        self.setCombo = QtWidgets.QComboBox()
        self.setCombo.setToolTip(
            "The active ROI set. Sets keep their own frames and measurement "
            "configuration, so regions drawn on two results do not mix."
        )
        self.setCombo.setMinimumWidth(120)
        self.setsButton = QtWidgets.QToolButton()
        self.setsButton.setText("Sets")
        self.setsButton.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.setsMenu = QtWidgets.QMenu(self.setsButton)
        self._buildSetsMenu()
        self.setsButton.setMenu(self.setsMenu)

        self.deselectButton = QtWidgets.QPushButton("Deselect")
        self.deselectButton.setToolTip(
            "Clear the selection, so actions apply to every ROI again"
        )
        self.updateButton = QtWidgets.QPushButton("Update")
        self.updateButton.setToolTip(
            "Replace the selected ROI's geometry with the shape now drawn, "
            "keeping its identity"
        )
        self.propertiesButton = QtWidgets.QPushButton("Properties…")
        self.specifyButton = QtWidgets.QPushButton("Specify…")
        self.specifyButton.setToolTip("Create an ROI at exact coordinates")
        self.sortButton = QtWidgets.QPushButton("Sort")
        self.sortButton.setToolTip("Order the list by name")
        self.filterEdit = QtWidgets.QLineEdit()
        self.filterEdit.setPlaceholderText("Filter by name…")
        self.filterEdit.setClearButtonEnabled(True)
        self.filterEdit.setMaximumWidth(160)
        self.filterEdit.setToolTip(
            "Hide rows whose name does not match. A view only — nothing is "
            "removed, and actions still apply to the selection."
        )

        self.moreButton = QtWidgets.QToolButton()
        self.moreButton.setText("More")
        self.moreButton.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.moreButton.setToolTip("Set and shape operations on the selected ROIs")
        self.moreMenu = QtWidgets.QMenu(self.moreButton)
        self._buildMoreMenu()
        self.moreButton.setMenu(self.moreMenu)
        self.exportCsvButton = QtWidgets.QPushButton("Export CSV")
        self.exportJsonButton = QtWidgets.QPushButton("Export JSON")
        self.fileButton = QtWidgets.QToolButton()
        self.fileButton.setText("File")
        self.fileButton.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.fileMenu = QtWidgets.QMenu(self.fileButton)
        self._buildFileMenu()
        self.fileButton.setMenu(self.fileMenu)

        # Grouped the way ImageJ groups them — draw, edit, measure, files —
        # rather than as one row of twenty buttons in the order they were
        # written. Two rows, because one would not fit a docked panel.
        controls = QtWidgets.QVBoxLayout()
        controls.setSpacing(2)
        for group in (
            (self.addRectangleButton, self.addPointsButton, self.captureButton,
             self.updateButton, None,
             self.renameButton, self.propertiesButton, self.specifyButton,
             self.duplicateButton, self.deleteButton, self.clearButton, None,
             self.undoButton, self.redoButton, self.deselectButton,
             self.sortButton, self.moreButton),
            (self.refreshButton, self.measurementsButton, self.measureButton,
             self.multiMeasureButton, self.multiPlotButton,
             self.acrossResultsButton, self.cancelButton, None,
             self.exportCsvButton, self.exportJsonButton, self.fileButton, None,
             self.setCombo, self.setsButton),
        ):
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(3)
            for widget in group:
                if widget is None:
                    separator = QtWidgets.QFrame()
                    separator.setFrameShape(QtWidgets.QFrame.VLine)
                    separator.setFrameShadow(QtWidgets.QFrame.Sunken)
                    row.addWidget(separator)
                    continue
                row.addWidget(widget)
            row.addStretch()
            controls.addLayout(row)

        options = QtWidgets.QHBoxLayout()
        options.setSpacing(3)
        options.addWidget(self.showAllCheck)
        options.addWidget(self.labelsCheck)
        options.addWidget(self.associateSlicesCheck)
        options.addWidget(self.removeSliceInfoButton)
        options.addStretch()
        options.addWidget(self.filterEdit)
        controls.addLayout(options)

        #: Measurement ids in column order, filled in by _applyColumns().
        self._columnIds: tuple[str, ...] = ()
        self.table = QtWidgets.QTableWidget(0, 0)
        self._applyColumns("px")
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        # Extended, not single: the set operations need two ROIs, and there is
        # no way to express "these two" with a single-selection table.
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)

        self.summaryLabel = QtWidgets.QLabel("No ROIs.")
        self.summaryLabel.setWordWrap(True)
        self.summaryLabel.setStyleSheet("color:#888; font-size:8pt;")

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(controls)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.progressBar)
        layout.addWidget(self.summaryLabel)
        self.setLayout(layout)

        self.addRectangleButton.clicked.connect(self._startRectangleDrawing)
        self.addPointsButton.clicked.connect(self._startPointDrawing)
        self.captureButton.clicked.connect(self.add_current_rectangle)
        self.renameButton.clicked.connect(self.rename_selected)
        self.duplicateButton.clicked.connect(self.duplicate_selected)
        self.deleteButton.clicked.connect(self.delete_selected)
        self.clearButton.clicked.connect(self.clear_rois)
        self.deselectButton.clicked.connect(self.deselect)
        self.updateButton.clicked.connect(self.update_selected)
        self.propertiesButton.clicked.connect(self.edit_properties)
        self.specifyButton.clicked.connect(self.specify_roi)
        self.sortButton.clicked.connect(self.sort_rois)
        self.filterEdit.textChanged.connect(self._filterChanged)
        self.table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._tableMenu)
        self.undoButton.clicked.connect(self.undo)
        self.redoButton.clicked.connect(self.redo)
        self.refreshButton.clicked.connect(self.refresh_stats)
        self.measurementsButton.clicked.connect(self.edit_measurements)
        self.measureButton.clicked.connect(self.measure)
        self.multiMeasureButton.clicked.connect(self.multi_measure)
        self.multiPlotButton.clicked.connect(self.multi_plot)
        self.acrossResultsButton.clicked.connect(self.measure_across_results)
        self.cancelButton.clicked.connect(self.cancel_measurement)
        # Queued by construction — the runner's callbacks fire on the worker
        # thread, and these slots touch widgets.
        self.sigJobFinished.connect(self._onJobFinished)
        self.sigJobProgress.connect(self._onJobProgress)
        self.sigJobDiscarded.connect(self._onJobDiscarded)
        self.exportCsvButton.clicked.connect(self.export_csv)
        self.exportJsonButton.clicked.connect(self.export_json)
        self.setCombo.currentIndexChanged.connect(self._setSelected)
        self._refreshSetCombo()
        self.table.itemChanged.connect(self._item_changed)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.showAllCheck.toggled.connect(self._overlay.set_visible)
        self.labelsCheck.toggled.connect(self._overlay.set_labels_visible)
        self.removeSliceInfoButton.clicked.connect(self.remove_slice_info)
        # The broker owns "what am I measuring", so the overlay lines up with
        # the same image the statistics are read from.
        self._toolService.sigTargetLayerChanged.connect(self._onTargetLayerChanged)
        try:
            # Clicking a drawn ROI selects its row. Registered on the overlay
            # and torn down with it, so it cannot outlive the panel.
            self._overlay.install_click_handler(self._on_overlay_clicked)
        except Exception:
            pass
        try:
            # Through the broker so release() tears this down too; connecting
            # straight to the viewer left the callback firing after close.
            self._toolService.on_viewer_event(
                self._toolToken,
                self._viewer.dims.events.current_step,
                lambda _event=None: self.request_refresh(),
            )
        except Exception:
            pass

        self.refresh_stats()

    def _startRectangleDrawing(self) -> None:
        # Re-acquire so the rectangle drawn next is attributed to this panel.
        self._toolToken = self._toolService.acquire(self.TOOL_OWNER)
        # This panel adds every shape drawn, so it must be allowed to hold
        # more than one at a time; the single-shape rule would silently keep
        # only the newest and capture one of three.
        self._toolService.set_multi_shape(self._toolToken, True)
        self._toolService.set_mode(self._toolToken, "rectangle")

    def _startPointDrawing(self) -> None:
        """Place points instead of drawing shapes (P-P, C-08)."""
        self._toolToken = self._toolService.acquire(self.TOOL_OWNER)
        self._toolService.set_multi_shape(self._toolToken, True)
        self._toolService.set_mode(self._toolToken, "point")

    def add_current_rectangle(self) -> None:
        try:
            # add() uniquifies on collision, so naming here as well would only
            # be a second chance to get it wrong.
            for roi in self._drawn_rois():
                self._commands.run(AddROI(roi))
            # Clears only this panel's scratch shape, leaving the Profile and
            # ROI statistics panels' shapes alone.
            self._toolService.clear(self._toolToken)
            self.refresh_stats()
        except Exception as exc:
            self.summaryLabel.setText(str(exc))

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        """Release the drawing tool and our viewer callbacks on close."""
        try:
            self._toolService.release(self._toolToken)
        except Exception:
            pass
        try:
            self._overlay.remove_click_handler()
            self._overlay.remove()
        except Exception:
            pass
        try:
            # A measurement thread that outlives its panel is the same class of
            # problem as a viewer callback that does.
            self._runner.shutdown()
        except Exception:
            pass
        super().closeEvent(event)

    def add_rois(self, rois: list[ROIRecord], *, on_conflict: str = "rename") -> int:
        """Add externally generated ROIs, all of them or none.

        One command rather than one per ROI: a two-hundred-ROI import that
        failed half way used to leave the user unable to tell which had
        arrived, and undoing it meant two hundred presses.
        """
        rois = list(rois)
        if not rois:
            return 0
        command = ImportROIs(rois=tuple(rois), on_conflict=on_conflict)
        try:
            self._commands.run(command)
        except Exception as exc:
            # Refresh first, then report: refresh_stats writes the summary
            # label itself, so saying it the other way round means the user
            # never sees why nothing arrived.
            self.refresh_stats()
            self.summaryLabel.setText(f"Import failed, nothing was added: {exc}")
            return 0
        self.refresh_stats()
        return command.added + command.replaced

    def active_set(self) -> ROISet:
        """The set the panel is showing, with its ROIs in it.

        Public because a processor restricted to a region has to record *which
        set at which revision* produced it, and reading the panel's private
        state to find out would make that provenance a coincidence.
        """
        self._syncSet()
        return self._set

    def rois(self, *, visible_only: bool = False) -> list[ROIRecord]:
        """Return a copy of currently managed ROIs for analysis widgets.

        Defaults to every ROI so existing consumers (PSF resolution,
        colocalization, segmentation) keep the behaviour they were written
        against; pass ``visible_only=True`` to honour the panel's checkboxes.
        """
        rois = self._model.rois
        return [roi for roi in rois if roi.visible] if visible_only else rois

    def rename_selected(self) -> None:
        roi = self._selected_roi()
        if roi is None:
            return
        new_name, ok = QtWidgets.QInputDialog.getText(
            self,
            "Rename ROI",
            "ROI name:",
            text=roi.name,
        )
        if ok and new_name.strip():
            try:
                self._commands.run(RenameROI(roi.name, new_name.strip()))
                self.refresh_stats()
            except Exception as exc:
                self.summaryLabel.setText(str(exc))

    def duplicate_selected(self) -> None:
        """Copy every selected ROI, each with an identity of its own."""
        from imswitch.imcommon.algorithms.roi import duplicated

        rois = self._selected_rois()
        if not rois:
            roi = self._selected_roi()
            if roi is None:
                return
            rois = [roi]
        # Expressed as Adds of copies so it is undoable like everything else.
        self._runOperation(
            "Duplicate",
            [
                duplicated(roi, name=self._model.unique_name(f"{roi.name}_copy"))
                for roi in rois
            ],
        )

    def delete_selected(self) -> None:
        """Delete every selected ROI.

        The whole selection, not the current row: the table became
        extended-selection for the set operations, and a Delete that then
        removed only one of five highlighted rows would be the surprising
        reading of "delete the selected ROIs".
        """
        rois = self._selected_rois()
        if not rois:
            roi = self._selected_roi()
            if roi is None:
                return
            rois = [roi]
        self._runOperation(
            "Delete", (), consumed=[roi.name for roi in rois]
        )

    def clear_rois(self) -> None:
        self._commands.run(ClearROIs())
        self.refresh_stats()

    def export_csv(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export ROI Statistics",
            "",
            "CSV files (*.csv)",
        )
        if path:
            self.write_csv(Path(path))

    def export_json(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export ROIs",
            "",
            "JSON files (*.json)",
        )
        if path:
            self.write_json(Path(path))

    def write_csv(self, path: Path, *, column: str | None = None) -> None:
        """Export the current table, or the last Multi Measure in wide form.

        Wide form — one row per plane, one column per ROI — is what ImageJ's
        Multi Measure writes, and it is derived here rather than kept beside
        the long form, so the two cannot drift apart (P-4.7).
        """
        if self._multiRows and self._multiAxisLabel:
            column = column or self._wideFormColumn()
            if column:
                fieldnames, rows = wide_form(
                    self._multiRows, column=column, axis_label=self._multiAxisLabel
                )
                self._write_rows(path, fieldnames, rows)
                return
        rows = self._stats_rows
        if not rows:
            rows = [record.to_dict() for record in self._model.rois]
        fieldnames = rows_to_columns(rows) if rows else ["name"]
        self._write_rows(path, fieldnames, rows)

    def _wideFormColumn(self) -> str:
        """Which measurement the wide-form export holds, asked once.

        A wide table has one value per (plane, ROI) cell, so it can carry
        exactly one measurement — silently picking the first would export a
        column the user never chose.
        """
        candidates = [
            key
            for key in self._multiRows[0]
            if key not in IDENTITY_COLUMNS
            and key != self._multiAxisLabel
            and isinstance(self._multiRows[0][key], (int, float))
            and not isinstance(self._multiRows[0][key], bool)
        ]
        if not candidates:
            return ""
        column, ok = QtWidgets.QInputDialog.getItem(
            self, "Export Multi Measure", "Measurement:", candidates, 0, False
        )
        return column if ok else ""

    @staticmethod
    def _write_rows(path: Path, fieldnames, rows) -> None:
        with Path(path).open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(fieldnames), extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    def write_json(self, path: Path) -> None:
        payload = {
            "rois": self._model.to_dicts(),
            "statistics": self._stats_rows,
        }
        with Path(path).open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    def setCurrentResult(self, result) -> None:
        """Re-measure the managed ROIs against the newly selected result.

        The ROI set is deliberately kept across results — measuring the same
        regions on several reconstructions is the point — but the numbers
        beside them have to follow the result they are measured on.
        """
        self.refresh_stats()

    def request_refresh(self) -> None:
        """Ask for a refresh soon, coalescing a burst into one pass.

        Dragging a dims slider emits ``current_step`` for every plane it
        crosses. Measuring each one makes the drag stutter and throws all but
        the last answer away, so the repeats collapse onto one timer.
        """
        self._refreshTimer.start()

    def edit_measurements(self) -> None:
        """ImageJ's Set Measurements, over the shared registry."""
        config = ROIMeasurementsDialog.edit(
            self._set.measurement_config,
            self,
            threshold_source=self._segmentationThreshold(),
        )
        if config is None:
            return
        self.set_measurement_config(config)

    def setSegmentationWidget(self, widget) -> None:
        """The Segmentation panel whose threshold seeds *Limit to threshold*.

        Optional and late-bound, because the panels are runtime-loaded in
        either order.
        """
        self._segmentationWidget = widget

    def _segmentationThreshold(self):
        """The Segmentation panel's manual threshold, when it has one.

        Only the value is borrowed, and only as a starting point in the dialog:
        the measurement threshold is recorded with the set, so it must be an
        explicit choice rather than a reading of another panel's state at the
        moment Measure happened to be pressed.
        """
        widget = getattr(self, "_segmentationWidget", None)
        spin = getattr(widget, "thresholdSpin", None)
        if spin is None:
            return None
        try:
            value = float(spin.value())
        except Exception:
            return None
        return (value, float("inf"))

    def set_measurement_config(self, config: MeasurementConfig) -> None:
        """Adopt a measurement configuration and re-measure against it."""
        # The revision is taken from the set, never from the incoming config:
        # the dialog builds a fresh one each time and would reset it to zero,
        # and the cache keys on it — two different configurations sharing a
        # revision is exactly how a stale row gets served as a fresh one.
        self._set = self._set.with_changes(
            measurement_config=config.with_changes(
                revision=self._set.measurement_config.revision + 1
            )
        )
        self._cache.clear()
        self.refresh_stats()

    def refresh_stats(self) -> None:
        self._refreshTimer.stop()
        image = self._current_image_2d()
        if image is None:
            self._stats_rows = []
            self._applyColumns("px")
            self._populate_table(None)
            self._refresh_overlay()
            self._refreshUndoButtons()
            self.summaryLabel.setText("No image layer selected.")
            return

        frame = self._current_frame()
        row_scale, col_scale, unit = plane_scales(frame)
        config = self._set.measurement_config
        # `None` means unconfigured and takes the defaults; an explicitly
        # empty tuple means the user unticked everything, which must stay
        # unticked rather than springing back to the defaults.
        selection = self._selection(unit)
        self._applyColumns(unit)

        frame_uid = frame.frame_uid if frame is not None else ""
        token = self._mutation_token()
        plane = self._plane_key(frame)

        def key_for(roi):
            return cache_key(frame_uid, token, plane, roi, config.revision)

        # compute_stats reports per-ROI failures in the record rather than
        # raising, so one unmeasurable ROI can no longer blank the table.
        records = self._model.compute_stats(
            image,
            selection=selection,
            row_scale=row_scale,
            col_scale=col_scale,
            unit=unit,
            threshold=config.threshold,
            line_width=config.line_width,
            geometry_match_for=lambda roi: self._geometry_match(frame, roi),
            cache=self._cache,
            cache_key_for=key_for,
        )
        self._stats_rows = [
            {**record.to_row(), **record.values} for record in records
        ]
        if self._syncSet():
            self._markDirty()
        self._populate_table(records)
        self._refresh_overlay()
        self._refreshUndoButtons()
        measured = sum(1 for record in records if record.measured)
        failed = sum(1 for record in records if record.error)
        summary = f"{len(records)} ROI(s), {measured} measured, image shape {image.shape}."
        if unit != "px":
            summary += f" Calibrated in {unit}."
        if failed:
            summary += f" {failed} could not be measured (see Note)."
        self.summaryLabel.setText(summary)

    def column_index(self, label: str) -> int:
        """The column showing ``label``, or -1.

        Columns follow the measurement selection, so a caller that wants "the
        area column" has to ask rather than count. Matching is on the label's
        stem so ``Area`` still finds ``Area (µm²)`` once the image is
        calibrated.
        """
        for col in range(self.table.columnCount()):
            header = self.table.horizontalHeaderItem(col)
            text = header.text() if header is not None else ""
            if text == label or text.startswith(f"{label} ("):
                return col
        return -1

    def _selection(self, unit: str = "px") -> tuple[str, ...]:
        """The measurement ids in force for a frame in ``unit``.

        One accessor, because the columns, the table and every push have to
        agree: a panel whose columns came from the defaults while its rows came
        from an explicit choice would render blank cells under real headers.
        Calibrated measurements drop out on an uncalibrated frame, here rather
        than at each call site, for the same reason.
        """
        return applicable_selection(
            self._set.measurement_config.selected, unit=unit
        )

    def _mutation_token(self) -> str:
        """A token for the pixels currently on screen, or '' when unknown.

        Supplied by whoever rendered the layer, never derived here: an array's
        identity and shape survive a rewrite of its contents, so a token made
        from them would certify data that had changed. Empty simply switches
        caching off, which is the safe direction.
        """
        layer = self._active_image_layer()
        metadata = getattr(layer, "metadata", None) or {}
        try:
            return str(metadata.get("mutation_token") or "")
        except Exception:
            return ""

    def _plane_key(self, frame) -> tuple[tuple[str, int], ...]:
        """The plane being measured, labelled, for the cache key.

        Always the plane on screen — unlike the ROI's stored ``position``,
        which is opt-in and absent for an ROI that applies to every slice. A
        cache that ignored this would answer plane 40 with plane 1's numbers.
        """
        if frame is None:
            return ()
        try:
            return plane_position(self._viewer, frame)
        except Exception:
            return ()

    def _syncSet(self) -> bool:
        """Keep the active ROISet holding the ROIs it is a set of.

        The set carried frames and the measurement configuration but never the
        records, so anything reading `_set.rois` — export, the state store,
        anything downstream — saw an empty set beside a full table. Returns
        whether anything moved, which is what decides an autosave.
        """
        rois = tuple(self._model.rois)
        if rois == self._set.rois:
            return False
        self._set = self._set.with_rois(rois)
        return True

    def _applyColumns(self, unit: str) -> None:
        """Set the table's columns from the current measurement selection.

        The unit lives in the header and nowhere else, so a cell is a number
        and a column says what the number is in — rather than every cell
        carrying a suffix that then has to be stripped to sort or export it.
        """
        entries = selected_measurements(self._selection(unit))
        ids = tuple(entry.id for entry in entries)
        labels = [
            *self._FIXED_LEADING,
            *(column_label(entry.id, unit) for entry in entries),
            *self._FIXED_TRAILING,
        ]
        current = [
            self.table.horizontalHeaderItem(col).text()
            if self.table.horizontalHeaderItem(col) is not None
            else ""
            for col in range(self.table.columnCount())
        ]
        self._columnIds = ids
        if current == labels:
            return
        # Rebuilding the header invalidates every row, so the rows are cleared
        # with it rather than left half-labelled by the previous selection.
        self.table.setRowCount(0)
        self.table.setColumnCount(len(labels))
        self.table.setHorizontalHeaderLabels(labels)

    def _populate_table(self, records) -> None:
        """Render one row per ROI in the model.

        Rows always come from the model, never from the measured subset: an
        unchecked ROI still needs the row that holds the checkbox to switch it
        back on. Measurement cells stay blank for anything not measured.
        """
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        try:
            by_uid = {record.roi.uid: record for record in (records or [])}
            rois = self._model.rois
            self.table.setRowCount(len(rois))
            for row, roi in enumerate(rois):
                record = by_uid.get(roi.uid)
                values = record.values if record is not None and record.measured else {}

                visible_item = _TableItem("")
                visible_item.setFlags(visible_item.flags() | QtCore.Qt.ItemIsUserCheckable)
                visible_item.setCheckState(QtCore.Qt.Checked if roi.visible else QtCore.Qt.Unchecked)
                visible_item.setData(ROI_KEY_ROLE, roi.uid)
                visible_item.setData(SORT_KEY_ROLE, bool(roi.visible))
                self.table.setItem(row, 0, visible_item)

                cells = [
                    roi.name,
                    roi.roi_type,
                    *(values.get(key) for key in self._columnIds),
                    "" if record is None else record.note,
                ]
                for col, value in enumerate(cells, start=1):
                    item = _TableItem(self._format_value(value))
                    item.setData(SORT_KEY_ROLE, value)
                    self.table.setItem(row, col, item)
        finally:
            self.table.blockSignals(False)
            self.table.setSortingEnabled(sorting)
        # Rows were rebuilt, so the filter has to be applied to the new ones.
        self._filterChanged(self.filterEdit.text())

    # ----------------------------------------------------------------------
    # P-4 — Measure, Multi Measure, Multi Plot
    # ----------------------------------------------------------------------

    def measure(self) -> None:
        """Push the selected ROI, or every ROI, to the Results table.

        ImageJ's rule: no selection means all. Measuring nothing because
        nothing happened to be clicked is the more surprising of the two.
        """
        selected = self._selected_uids()
        rois = [
            roi
            for roi in self._model.rois
            if (not selected or roi.uid in selected) and roi.visible
        ]
        if not rois:
            self.summaryLabel.setText("No visible ROI to measure.")
            return
        image = self._current_image_2d()
        if image is None:
            self.summaryLabel.setText("No image layer selected.")
            return

        frame = self._current_frame()
        row_scale, col_scale, unit = plane_scales(frame)
        config = self._set.measurement_config
        selection = self._selection(unit)
        plane = self._plane_key(frame)
        source = self._source_name()

        frame_uid = frame.frame_uid if frame is not None else ""
        rows, failed, skipped = [], [], 0
        for roi in rois:
            if not roi_belongs_on_plane(roi, plane):
                skipped += 1
                continue
            try:
                values = measure_roi(
                    image, roi,
                    selection=selection,
                    row_scale=row_scale, col_scale=col_scale, unit=unit,
                    threshold=config.threshold,
                    geometry_match=self._geometry_match(frame, roi),
                    line_width=config.line_width,
                )
            except Exception as exc:
                # One unmeasurable ROI costs its own row, not the batch.
                failed.append(f"{roi.name}: {exc}")
                continue
            rows.append(
                measurement_row(
                    roi, values, source=source, plane=plane,
                    frame_uid=frame_uid, label=config.display_label,
                )
            )

        if rows:
            self.sigResultPushed.emit(rows_to_columns(rows), rows)
        message = f"Measured {len(rows)} ROI(s)."
        if skipped:
            message += f" {skipped} belong to another slice."
        if failed:
            message += f" {len(failed)} could not be measured: {failed[0]}"
        self.summaryLabel.setText(message)

    def multi_measure(self, axis_label: str | None = None) -> None:
        """Measure every visible ROI on every slice of a stack axis.

        Long form (Q-02a): one row per (ROI, plane). Run on P-J's worker, so a
        500-plane stack does not freeze the window, and cancellable.
        """
        frame = self._current_frame()
        axes = stack_axis_labels(frame)
        if not axes:
            self.summaryLabel.setText("This image has no stack axis to step.")
            return
        if axis_label is None:
            axis_label = axes[0]
            if len(axes) > 1:
                choice, ok = QtWidgets.QInputDialog.getItem(
                    self, "Multi Measure", "Step along:", list(axes), 0, False
                )
                if not ok:
                    return
                axis_label = choice
        if axis_label not in axes:
            self.summaryLabel.setText(f"{axis_label!r} is not a stack axis here.")
            return

        rois = [roi for roi in self._model.rois if roi.visible]
        if not rois:
            self.summaryLabel.setText("No visible ROI to measure.")
            return

        try:
            source = self._plane_source(frame)
        except Exception as exc:
            self.summaryLabel.setText(str(exc))
            return

        planes = planes_for_axis(frame, axis_label, positions=self._plane_key(frame))
        config = self._set.measurement_config
        job = MeasurementJob.create(
            rois,
            source,
            planes,
            measurements=tuple(self._selection(plane_scales(frame)[2])),
            config_revision=config.revision,
            frame_uid=frame.frame_uid if frame is not None else "",
        )
        self._multiAxisLabel = axis_label
        self._startJob(job, frame)

    def multi_plot(self) -> None:
        """Plot the last Multi Measure: one curve per ROI along the stack axis."""
        if not self._multiRows:
            self.summaryLabel.setText("Run Multi Measure first.")
            return
        columns = [
            key
            for key in self._multiRows[0]
            if key not in IDENTITY_COLUMNS
            and key != self._multiAxisLabel
            and isinstance(self._multiRows[0][key], (int, float))
            and not isinstance(self._multiRows[0][key], bool)
        ]
        if not columns:
            self.summaryLabel.setText("Nothing numeric to plot.")
            return
        column, ok = QtWidgets.QInputDialog.getItem(
            self, "Multi Plot", "Plot:", columns, 0, False
        )
        if not ok:
            return
        try:
            payload = multi_plot_payload(
                self._multiRows,
                column=column,
                axis_label=self._multiAxisLabel,
                unit=str(self._multiRows[0].get("spatial_unit", "") or ""),
            )
        except Exception as exc:
            self.summaryLabel.setText(str(exc))
            return
        self.sigPlotPushed.emit(payload)

    def cancel_measurement(self) -> None:
        self._runner.cancel()
        self.summaryLabel.setText("Cancelling…")

    def setAvailableResults(self, results, selected=None) -> None:  # noqa: N802
        """The loaded results, for *Measure across results*.

        Kept up to date through `sigResultsChanged`, so loading a
        reconstruction while the panel is open reaches it (C-11).
        """
        self._availableResults = list(results or [])
        self.acrossResultsButton.setEnabled(len(self._availableResults) > 1)

    def _geometry_match(self, frame, roi) -> str:
        """How comparable one ROI is to the frame it is about to be measured on.

        Reported on every path, not only Across Results. Defaulting to
        ``"exact"`` — which is what this used to do — states as fact something
        nothing checked: an ROI captured on one reconstruction and measured on
        the next would claim an exact match it had never been tested for.

        An ROI whose capture frame is unknown gets ``UNVERIFIED`` rather than a
        verdict, because "we did not check" and "we checked and it matched" are
        different claims and only one of them is safe to read as agreement.
        """
        if frame is None:
            return UNVERIFIED
        source = self._set.frame(getattr(roi, "frame_uid", "") or "")
        if source is None:
            return UNVERIFIED
        return compatibility(source, frame, positions=getattr(roi, "position", ()))

    def preflight_entries(self, results=None):
        """`(result_name, roi_name, verdict)` for every ROI × result pair.

        The A-13 ladder decides; nothing here decides anything. Returned rather
        than acted on so the same judgement can be tested, shown in the
        preflight, and used to filter the run.
        """
        target = self._current_frame()
        entries = []
        for name, result in self._resultPairs(results):
            frame = frame_from_result(result)
            for roi in self._model.rois:
                if not roi.visible:
                    continue
                source = self._set.frame(roi.frame_uid) or target
                if source is None or frame is None:
                    entries.append((name, roi.name, "incompatible"))
                    continue
                entries.append(
                    (name, roi.name, compatibility(source, frame, positions=roi.position))
                )
        return entries

    def measure_across_results(self, *, confirm=None) -> None:
        """Measure this ROI set on every selected result (P-4.4).

        Refuses anything below `pixel-compatible` unless the user opts in row
        by row through the preflight (Q-08). ``confirm`` is the opt-in hook,
        injected so the decision can be exercised without a modal dialog.
        """
        entries = self.preflight_entries()
        if not entries:
            self.summaryLabel.setText("No results to measure against.")
            return

        auto = [entry for entry in entries if is_auto_measurable(entry[2])]
        if len(auto) != len(entries):
            confirm = confirm or (lambda items: ROIPreflightDialog.confirm(items, self))
            chosen = confirm(entries)
            if chosen is None:
                self.summaryLabel.setText("Measurement cancelled.")
                return
            allowed = [
                entry
                for entry in chosen
                if is_auto_measurable(entry[2]) or entry[2] in OPT_IN_MEASURABLE
            ]
        else:
            allowed = auto
        if not allowed:
            # Naming the verdicts rather than saying "nothing to measure":
            # "incompatible" is a specific answer the user can act on, and a
            # generic one reads like a bug in the panel.
            reasons = ", ".join(sorted({entry[2] for entry in entries}))
            self.summaryLabel.setText(f"Nothing measurable: every pair is {reasons}.")
            return

        by_result: dict[str, set] = {}
        verdicts: dict[tuple[str, str], str] = {}
        for result_name, roi_name, verdict in allowed:
            by_result.setdefault(result_name, set()).add(roi_name)
            verdicts[(result_name, roi_name)] = verdict

        config = self._set.measurement_config
        rows, failed = [], 0
        # Synchronous, unlike Multi Measure: this reads **one plane per
        # (ROI, result)** rather than sweeping a stack, so the work is bounded
        # by the number of loaded results. The plane reads are lazy, so a large
        # result costs one plane, not all of it. The progress bar and the wait
        # cursor are here because "bounded" is not the same as "instant".
        total = len(allowed)
        self.progressBar.setVisible(True)
        self.progressBar.setRange(0, max(1, total))
        self.progressBar.setValue(0)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            rows, failed = self._measureAcross(by_result, verdicts, config)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.progressBar.setVisible(False)

        if rows:
            self.sigResultPushed.emit(rows_to_columns(rows), rows)
        message = f"Measured {len(rows)} row(s) across {len(by_result)} result(s)."
        if failed:
            message += f" {failed} could not be measured."
        skipped = len(entries) - len(allowed)
        if skipped:
            message += f" {skipped} skipped as incompatible or not opted into."
        self.summaryLabel.setText(message)

    def _measureAcross(self, by_result, verdicts, config):
        """The across-results sweep itself: one plane per (ROI, result)."""
        rows, failed, done = [], 0, 0
        for name, result in self._resultPairs():
            wanted = by_result.get(name)
            if not wanted:
                continue
            frame = frame_from_result(result)
            row_scale, col_scale, unit = plane_scales(frame)
            selection = self._selection(unit)
            for roi in self._model.rois:
                if roi.name not in wanted:
                    continue
                # The ROI's own recorded slice, which is the one the preflight
                # validated against this result. Falling back to index zero
                # here — as this did — reported an ROI captured at Z=12 as
                # measured on Z=0, with a verdict earned by Z=12.
                plane = self._planeForROI(roi, frame)
                try:
                    image = self._resultPlane(result, plane)
                    values = measure_roi(
                        image, roi,
                        selection=selection,
                        row_scale=row_scale, col_scale=col_scale, unit=unit,
                        threshold=config.threshold,
                        geometry_match=verdicts[(name, roi.name)],
                        line_width=config.line_width,
                    )
                except Exception:
                    failed += 1
                    continue
                rows.append(
                    measurement_row(
                        roi, values, source=name, plane=plane,
                        frame_uid=frame.frame_uid if frame is not None else "",
                        label=config.display_label,
                    )
                )
                done += 1
                self.progressBar.setValue(done)
        return rows, failed

    def _resultPairs(self, results=None):
        """`(display name, result)` for the results this panel may measure."""
        pairs = []
        for item in results if results is not None else self._availableResults:
            if isinstance(item, tuple) and len(item) == 2:
                name, result = item
            else:
                name, result = getattr(item, "name", "result"), item
            pairs.append((str(name), result))
        return pairs

    @staticmethod
    def _planeForROI(roi, frame) -> tuple[tuple[str, int], ...]:
        """The slice of ``frame`` this ROI should be measured on.

        The ROI's own recorded position wins, because that is what the
        compatibility check validated. An ROI with no position keeps ImageJ's
        "applies to every slice" and is measured on the first — stated here
        rather than implied, and reported in the row's plane columns either
        way, so a row always says which slice it came from.
        """
        if frame is None:
            return ()
        bound = dict(getattr(roi, "position", ()) or ())
        displayed = set(frame.plane_axes)
        return tuple(
            (axis.label, int(bound.get(axis.label, 0)))
            for axis in frame.axes
            if axis.label not in displayed
        )

    @staticmethod
    def _resultPlane(result, plane):
        """One plane of a result, without materialising the rest of it.

        Read through :class:`LazyPlaneSource`, which indexes the handle and
        converts only what comes back. ``np.asarray`` on the result — which is
        what this did — pulls a whole lazy or on-disk stack into memory to
        take one slice out of it, defeating chunked access exactly where it
        matters most.
        """
        data = getattr(result, "data", None)
        if data is None:
            raise ValueError("result has no data to measure")
        shape = tuple(int(v) for v in np.shape(data))
        if len(shape) < 2:
            raise ValueError("result has no plane to measure")
        labels = [str(v) for v in (getattr(result, "axis_labels", None) or [])]
        if len(labels) != len(shape):
            labels = [f"D{i}" for i in range(len(shape) - 2)] + ["Y", "X"]
        # Through the same reader a job would use, so an across-results plane
        # and a Multi Measure plane are indexed by one rule, not two.
        source = LazyPlaneSource(
            handle=data,
            axis_labels=tuple(labels),
            plane_axes=(labels[-2], labels[-1]),
        )
        return source.read_plane(tuple(plane))

    def _source_name(self) -> str:
        """The result these numbers came from.

        The Results dock is an accumulating log; without this, rows measured on
        two reconstructions differ only in their values.
        """
        layer = self._active_image_layer()
        metadata = getattr(layer, "metadata", None) or {}
        try:
            name = metadata.get("source_result")
        except Exception:
            name = None
        return str(name or getattr(layer, "name", "") or "image")

    def _plane_source(self, frame):
        """A pure plane reader for the current layer (A-22).

        The mutation token comes from the renderer, never from the array: it is
        what decides whether a row may be cached, and inventing one here would
        certify data that could have changed.

        **No token means the pixels can move under the run**, which is the case
        A-22 calls snapshot-or-refuse. Publishing anyway is what the empty token
        used to allow: the end-of-run staleness check compares `"" == ""` and
        passes, so plane 1 from before a live update and plane 40 from after it
        would be published as one measurement. So: snapshot, or — when the
        snapshot would not fit — refuse and say why.
        """
        layer = self._active_image_layer()
        if layer is None:
            raise ValueError("No image layer selected.")
        labels = [axis.label for axis in frame.axes] if frame is not None else []
        data = np.asarray(layer.data)
        if len(labels) != data.ndim:
            labels = [f"D{i}" for i in range(data.ndim - 2)] + ["Y", "X"]
        plane_axes = tuple(frame.plane_axes) if frame is not None else ("Y", "X")
        token = self._mutation_token()
        live = not token
        if live and int(getattr(data, "nbytes", 0)) > self.MAX_SNAPSHOT_BYTES:
            raise LiveDataError(
                "This image can change while it is measured and is too large "
                f"to copy ({data.nbytes / 2**30:.1f} GiB). Measure a saved "
                "result, or measure one plane at a time."
            )
        return ArrayPlaneSource(
            array=data,
            axis_labels=tuple(labels),
            plane_axes=plane_axes,
            mutation_token=token,
            live=live,
        )

    def _startJob(self, job, frame) -> None:
        row_scale, col_scale, unit = plane_scales(frame)
        config = self._set.measurement_config
        selection = tuple(job.measurements)
        source = self._source_name()
        threshold = config.threshold
        line_width = config.line_width

        frame_uid = frame.frame_uid if frame is not None else ""
        matches = {roi.uid: self._geometry_match(frame, roi) for roi in job.rois}

        def measure(image, roi, plane):
            if not roi_belongs_on_plane(roi, plane):
                # Bound to another slice. None is a skip, not a failure: an
                # ROI recorded at Z=12 measured on every plane of the stack is
                # the thing "Associate with slices" exists to prevent.
                return None
            # Values only: this is what gets cached, and a cached row that
            # carried a name would still carry it after a rename.
            return measure_roi(
                image, roi,
                selection=selection,
                row_scale=row_scale, col_scale=col_scale, unit=unit,
                threshold=threshold,
                geometry_match=matches.get(roi.uid, UNVERIFIED),
                line_width=line_width,
            )

        def decorate(values, roi, plane):
            return measurement_row(
                roi, values, source=source, plane=plane,
                frame_uid=frame_uid, label=config.display_label,
            )

        self._jobFailures = []
        self._setBusy(True, job.total_steps)
        self._runner.submit(
            job,
            measure,
            decorate=decorate,
            on_done=self.sigJobFinished.emit,
            on_progress=self.sigJobProgress.emit,
            on_discarded=self.sigJobDiscarded.emit,
            on_error=lambda roi, plane, exc: self._jobFailures.append(
                f"{getattr(roi, 'name', 'run')}: {exc}"
            ),
        )

    def _setBusy(self, busy: bool, total: int = 0) -> None:
        self.progressBar.setVisible(busy)
        self.cancelButton.setVisible(busy)
        self.multiMeasureButton.setEnabled(not busy)
        self.measureButton.setEnabled(not busy)
        if busy:
            self.progressBar.setRange(0, max(1, int(total)))
            self.progressBar.setValue(0)

    def _onJobProgress(self, done: int, total: int) -> None:
        self.progressBar.setRange(0, max(1, int(total)))
        self.progressBar.setValue(int(done))

    def _onJobFinished(self, result) -> None:
        rows = list(result.rows)
        failures = list(self._jobFailures)
        self._setBusy(False)
        self._multiRows = rows
        self.multiPlotButton.setEnabled(bool(rows))
        if not rows:
            self.summaryLabel.setText(
                f"Nothing measured. {failures[0]}" if failures else "Nothing measured."
            )
            return
        self.sigResultPushed.emit(rows_to_columns(rows), rows)
        message = (
            f"Multi Measure: {len(rows)} row(s) along {self._multiAxisLabel}."
        )
        if failures:
            message += f" {len(failures)} could not be measured: {failures[0]}"
        self.summaryLabel.setText(message)

    def _onJobDiscarded(self, result) -> None:
        """A cancelled, superseded or failed run.

        The progress goes; the numbers already on screen stay. A failed run
        says so — reporting a crash as "cancelled" tells the user they did
        something they did not do.
        """
        failures = list(self._jobFailures)
        self._setBusy(False)
        if getattr(result, "failed", False):
            reason = failures[-1] if failures else "see log"
            self.summaryLabel.setText(f"Measurement failed: {reason}")
        elif result.cancelled:
            self.summaryLabel.setText("Measurement cancelled.")

    # ----------------------------------------------------------------------
    # P-U — undo, redo and autosave
    # ----------------------------------------------------------------------

    def undo(self) -> None:
        if not self._commands.can_undo:
            return
        label = self._commands.undo_label
        self._commands.undo()
        self.refresh_stats()
        self.summaryLabel.setText(f"Undone: {label}.")

    def redo(self) -> None:
        if not self._commands.can_redo:
            return
        label = self._commands.redo_label
        self._commands.redo()
        self.refresh_stats()
        self.summaryLabel.setText(f"Redone: {label}.")

    def _refreshUndoButtons(self) -> None:
        """Say what would be undone, not just that something would be.

        "Undo" alone makes the user try it to find out; "Undo Delete ROI" lets
        them decide first.
        """
        self.undoButton.setEnabled(self._commands.can_undo)
        self.redoButton.setEnabled(self._commands.can_redo)
        self.undoButton.setToolTip(
            f"Undo {self._commands.undo_label}" if self._commands.can_undo
            else "Nothing to undo"
        )
        self.redoButton.setToolTip(
            f"Redo {self._commands.redo_label}" if self._commands.can_redo
            else "Nothing to redo"
        )

    def _markDirty(self) -> None:
        """Ask for an autosave soon (crash recovery, C-13/A-25).

        Debounced, and into the **existing state store** rather than a new
        file: an autosave that wrote on every keystroke would serialise the
        whole set each time, which is the cost the storage policy exists to
        avoid in the first place.
        """
        self._autosaveTimer.start()

    def _autosave(self) -> None:
        try:
            self.sigStateChanged.emit()
        except Exception:
            pass

    # ----------------------------------------------------------------------
    # P-6 — saving, loading and ImageJ interop
    # ----------------------------------------------------------------------

    def roiState(self) -> dict:
        """Every set, plus the panel options, ready for persistence.

        The sets are the state; the checkboxes ride along because they change
        what a capture records and would otherwise reset every session.
        """
        self._commitActiveSet()
        return sets_payload(
            self._sets,
            self._activeIndex,
            {
                "show_all": self.showAllCheck.isChecked(),
                "labels": self.labelsCheck.isChecked(),
                "associate_slices": self.associateSlicesCheck.isChecked(),
            },
        )

    def setRoiState(self, payload: dict) -> None:
        """Restore sets and options saved by :meth:`roiState`.

        An ROI whose frame is not in the payload is dropped rather than
        restored: it would be a region with no plane to belong to, and
        measuring it would report numbers against an image nothing connects it
        to. The dropping happens in `sets_from_payload`, which is the one
        restore path.
        """
        sets, active, options, dropped = sets_from_payload(payload)
        self._sets = list(sets) or [ROISet(name="ROIs")]
        self._activeIndex = max(0, min(active, len(self._sets) - 1))
        self.showAllCheck.setChecked(bool(options.get("show_all", True)))
        self.labelsCheck.setChecked(bool(options.get("labels", False)))
        self.associateSlicesCheck.setChecked(
            bool(options.get("associate_slices", False))
        )
        self._loadActiveSet()
        if dropped:
            self.summaryLabel.setText(
                f"{dropped} restored ROI(s) had no stored frame and were "
                "left out."
            )

    def _buildFileMenu(self) -> None:
        self.fileMenu.addAction("Save ROI set…", self.save_set)
        self.fileMenu.addAction("Open ROI set…", self.open_set)
        self.fileMenu.addSeparator()
        self.imagejImportAction = self.fileMenu.addAction(
            "Import ImageJ ROIs…", self.import_imagej
        )
        self.imagejExportAction = self.fileMenu.addAction(
            "Export ImageJ ROIs…", self.export_imagej
        )
        if not imagej_available():
            # Disabled with the reason on it, rather than absent: a missing
            # menu entry looks like the feature does not exist.
            for action in (self.imagejImportAction, self.imagejExportAction):
                action.setEnabled(False)
                action.setToolTip(
                    "Needs the optional 'roifile' package "
                    "(pip install roifile)"
                )
            self.fileMenu.setToolTipsVisible(True)

    def save_set(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save ROI set", f"{self._set.name}.json", "ROI sets (*.json)"
        )
        if not path:
            return
        self._commitActiveSet()
        try:
            write_set(Path(path), self._set)
        except Exception as exc:
            self.summaryLabel.setText(f"Could not save: {exc}")
            return
        self.summaryLabel.setText(
            f"Saved {len(self._set.rois)} ROI(s) to {Path(path).name}."
        )

    def open_set(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open ROI set", "", "ROI sets (*.json)"
        )
        if not path:
            return
        try:
            loaded = read_set(Path(path))
        except ROISetFormatError as exc:
            self.summaryLabel.setText(str(exc))
            return
        except Exception as exc:
            self.summaryLabel.setText(f"Could not open: {exc}")
            return
        # Into a set of its own, never over the active one: opening a file is
        # not a reason to discard what is on screen.
        self._commitActiveSet()
        self._sets.append(
            replace(loaded, name=self._uniqueSetName(loaded.name))
        )
        self._activeIndex = len(self._sets) - 1
        self._loadActiveSet()
        self.summaryLabel.setText(
            f"Opened {len(loaded.rois)} ROI(s) from {Path(path).name}."
        )

    def import_imagej(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Import ImageJ ROIs", "", "ImageJ ROIs (*.roi *.zip)"
        )
        if not path:
            return
        try:
            rois, report = read_imagej(Path(path))
        except Exception as exc:
            self.summaryLabel.setText(str(exc))
            return
        if not rois:
            self.summaryLabel.setText("That file contained no ROIs.")
            return
        self.add_rois(rois)
        self.summaryLabel.setText(f"Imported {report.summary}. {self._reportText(report)}")

    def export_imagej(self) -> None:
        rois = self._selected_rois() or self._model.rois
        if not rois:
            self.summaryLabel.setText("No ROIs to export.")
            return
        suffix = ".roi" if len(rois) == 1 else ".zip"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Export ImageJ ROIs",
            f"RoiSet{suffix}",
            "ImageJ ROIs (*.roi *.zip)",
        )
        if not path:
            return
        try:
            report = write_imagej(Path(path), rois)
        except Exception as exc:
            self.summaryLabel.setText(str(exc))
            return
        self.summaryLabel.setText(f"Exported {report.summary}. {self._reportText(report)}")

    @staticmethod
    def _reportText(report) -> str:
        """The first loss, spelled out.

        Interop is lossy in ways that are invisible in the result — a group
        that did not survive, a hole that was traced away — so at least one is
        said out loud rather than left in a report nobody opens.
        """
        if report.losses:
            return report.losses[0]
        if report.warnings:
            return report.warnings[0]
        return ""

    # ----------------------------------------------------------------------
    # P-S — more than one named set
    # ----------------------------------------------------------------------

    def _buildSetsMenu(self) -> None:
        for text, handler in (
            ("New set…", self.new_set),
            ("Duplicate set", self.duplicate_set),
            ("Rename set…", self.rename_set),
            ("Delete set", self.delete_set),
            (None, None),
            ("Merge from…", self.merge_set),
            ("Compare with…", self.compare_set),
        ):
            if text is None:
                self.setsMenu.addSeparator()
                continue
            self.setsMenu.addAction(text, handler)

    def _refreshSetCombo(self) -> None:
        self.setCombo.blockSignals(True)
        try:
            self.setCombo.clear()
            for roi_set in self._sets:
                self.setCombo.addItem(f"{roi_set.name} ({len(roi_set.rois)})")
            self.setCombo.setCurrentIndex(self._activeIndex)
        finally:
            self.setCombo.blockSignals(False)

    def _commitActiveSet(self) -> None:
        """Write the model's ROIs into the active set before leaving it."""
        self._syncSet()

    def _loadActiveSet(self) -> None:
        """Make the model show the active set, and forget the other set's history.

        The undo log is cleared on a switch rather than carried across.  Its
        commands hold ROI *names*, which mean different things in different
        sets, so an undo after a switch would either fail or — worse — succeed
        against the wrong ROI. A cross-set history is P-U's problem, and
        pretending to have one here would be the expensive kind of wrong.
        """
        self._model.set_rois(list(self._set.rois))
        self._commands = CommandLog(self._model)
        self._cache.clear()
        self._multiRows, self._multiAxisLabel = [], ""
        self.multiPlotButton.setEnabled(False)
        self._refreshSetCombo()
        self.refresh_stats()

    def _setSelected(self, index: int) -> None:
        if not 0 <= index < len(self._sets) or index == self._activeIndex:
            return
        self._commitActiveSet()
        self._activeIndex = index
        self._loadActiveSet()

    def _uniqueSetName(self, base: str, *, ignore: int | None = None) -> str:
        """A set name not already in use.

        ``ignore`` excludes one set from the check, so renaming a set to the
        name it already has is a no-op rather than a rename to "A_1".
        """
        names = {
            roi_set.name
            for index, roi_set in enumerate(self._sets)
            if index != ignore
        }
        if base not in names:
            return base
        index = 1
        while f"{base}_{index}" in names:
            index += 1
        return f"{base}_{index}"

    def new_set(self, name: str = "") -> None:
        """Start an empty set and switch to it."""
        if not name:
            name, ok = QtWidgets.QInputDialog.getText(
                self, "New ROI set", "Name:", text=self._uniqueSetName("ROIs")
            )
            if not ok or not name.strip():
                return
            name = name.strip()
        self._commitActiveSet()
        self._sets.append(ROISet(name=self._uniqueSetName(name)))
        self._activeIndex = len(self._sets) - 1
        self._loadActiveSet()

    def duplicate_set(self) -> None:
        """Copy the active set, keeping every ROI's identity.

        Deliberately *not* new uids: the point of duplicating a set is to try a
        variant of it, and comparing the two afterwards only says anything if
        the ROIs on both sides are recognisably the same ones.
        """
        self._commitActiveSet()
        source = self._set
        copy = replace(
            source,
            uid=str(uuid.uuid4()),
            name=self._uniqueSetName(f"{source.name}_copy"),
        )
        self._sets.append(copy)
        self._activeIndex = len(self._sets) - 1
        self._loadActiveSet()

    def rename_set(self) -> None:
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Rename ROI set", "Name:", text=self._set.name
        )
        if not ok or not name.strip():
            return
        self._set = self._set.with_changes(
            name=self._uniqueSetName(name.strip(), ignore=self._activeIndex)
        )
        self._refreshSetCombo()

    def delete_set(self) -> None:
        if len(self._sets) == 1:
            self.summaryLabel.setText(
                "This is the only set; clear its ROIs instead of deleting it."
            )
            return
        self._sets.pop(self._activeIndex)
        self._activeIndex = min(self._activeIndex, len(self._sets) - 1)
        self._loadActiveSet()

    def _chooseOtherSet(self, title: str) -> int:
        others = [
            (index, roi_set)
            for index, roi_set in enumerate(self._sets)
            if index != self._activeIndex
        ]
        if not others:
            self.summaryLabel.setText("There is only one set.")
            return -1
        names = [roi_set.name for _index, roi_set in others]
        name, ok = QtWidgets.QInputDialog.getItem(self, title, "Set:", names, 0, False)
        if not ok:
            return -1
        return others[names.index(name)][0]

    def merge_set(self, index: int | None = None, on_conflict: str = "") -> None:
        """Fold another set into this one, reporting what happened.

        The policy is asked for only when there is a real conflict — the same
        ROI edited two ways — because that is the only case where the answer
        can lose work.
        """
        self._commitActiveSet()
        if index is None:
            index = self._chooseOtherSet("Merge from")
        if index is None or index < 0:
            return

        source = self._sets[index]
        _preview, report = merge_sets(self._set, source, on_conflict="skip")
        if report.conflicts and not on_conflict:
            choice, ok = QtWidgets.QInputDialog.getItem(
                self,
                "Merge conflicts",
                f"{len(report.conflicts)} ROI(s) exist in both sets with "
                "different geometry. Keep:",
                ["this set's version", "the other set's version", "both"],
                0,
                False,
            )
            if not ok:
                return
            on_conflict = {
                "this set's version": "skip",
                "the other set's version": "replace",
                "both": "keep-both",
            }[choice]

        merged, report = merge_sets(
            self._set, source, on_conflict=on_conflict or "skip"
        )
        self._set = merged
        self._model.set_rois(list(merged.rois))
        self._commands = CommandLog(self._model)
        self._refreshSetCombo()
        self.refresh_stats()
        message = f"Merged {source.name!r}: {report.summary}."
        if report.renamed:
            old, new = report.renamed[0]
            message += f" Renamed {old!r} to {new!r}"
            if len(report.renamed) > 1:
                message += f" and {len(report.renamed) - 1} more"
            message += "."
        self.summaryLabel.setText(message)

    def compare_set(self, index: int | None = None) -> None:
        self._commitActiveSet()
        if index is None:
            index = self._chooseOtherSet("Compare with")
        if index is None or index < 0:
            return
        other = self._sets[index]
        report = compare_sets(self._set, other)
        self.summaryLabel.setText(
            f"{self._set.name!r} vs {other.name!r}: {report.summary}."
        )

    # ----------------------------------------------------------------------
    # P-5 — set and shape operations
    # ----------------------------------------------------------------------

    def _buildMoreMenu(self) -> None:
        """The *More* menu: every P-5 operation, in the order ImageJ groups them."""
        entries = [
            ("AND (intersection)", lambda: self.combine_selected("and")),
            ("OR (union)", lambda: self.combine_selected("or")),
            ("XOR", lambda: self.combine_selected("xor")),
            ("Subtract", lambda: self.combine_selected("subtract")),
            None,
            ("Split", self.split_selected),
            ("Enlarge…", self.enlarge_selected),
            ("Make Band…", self.make_band_selected),
            ("To Bounding Box", self.bounding_box_selected),
            ("Convex Hull", self.convex_hull_selected),
            ("Make Inverse", self.make_inverse_selected),
            ("Translate…", self.translate_selected),
            None,
            ("Rescale to this result…", self.rescale_selected_to_current_frame),
            None,
            ("Create Selection from labels", self.create_selection),
            ("Create Mask", self.create_mask),
        ]
        for entry in entries:
            if entry is None:
                self.moreMenu.addSeparator()
                continue
            text, handler = entry
            self.moreMenu.addAction(text, handler)

    def _selected_rois(self) -> list[ROIRecord]:
        """Every selected ROI, in model order.

        Model order rather than click order: an AND is symmetric but Subtract
        is not, and "the first one I happened to click" is not something the
        table shows anywhere.
        """
        uids = set(self._selected_uids())
        return [roi for roi in self._model.rois if roi.uid in uids]

    def _runOperation(self, label, produced, *, consumed=()) -> None:
        """Apply an operation's result through the command log."""
        self._commands.run(
            ReplaceROIs(
                consumed=tuple(consumed), produced=tuple(produced), label=label
            )
        )
        self.refresh_stats()

    def _operation(self, label, function, *, needs: int = 1, consume: bool = False):
        """Run ``function`` over the selection, reporting a refusal as text.

        Operations refuse for reasons the user can act on — two different
        planes, an empty intersection, an image too large to invert — so the
        message is shown rather than swallowed or raised into the event loop.
        """
        rois = self._selected_rois()
        if len(rois) < needs:
            self.summaryLabel.setText(
                f"Select {needs} ROI(s) first." if needs > 1 else "Select an ROI first."
            )
            return
        try:
            produced = function(rois)
        except ROIOperationError as exc:
            self.summaryLabel.setText(str(exc))
            return
        except Exception as exc:
            self.summaryLabel.setText(f"{label} failed: {exc}")
            return
        if not produced:
            self.summaryLabel.setText(f"{label} produced nothing.")
            return
        self._runOperation(
            label, produced, consumed=[roi.name for roi in rois] if consume else ()
        )
        self.summaryLabel.setText(f"{label}: {len(produced)} ROI(s).")

    def combine_selected(self, op: str) -> None:
        self._operation(
            op.upper(), lambda rois: [combine(rois, op)], needs=2
        )

    def split_selected(self) -> None:
        # Consuming: the parts *are* the original, partitioned, so keeping both
        # would double every measurement of that region.
        self._operation("Split", lambda rois: split(rois[0]), consume=True)

    def enlarge_selected(self) -> None:
        pixels, ok = QtWidgets.QInputDialog.getInt(
            self, "Enlarge", "Pixels (negative shrinks):", 1, -999, 999
        )
        if ok:
            self._operation("Enlarge", lambda rois: [enlarge(rois[0], pixels)])

    def make_band_selected(self) -> None:
        width, ok = QtWidgets.QInputDialog.getInt(
            self, "Make Band", "Band width (px):", 5, 1, 999
        )
        if ok:
            self._operation("Make Band", lambda rois: [make_band(rois[0], width)])

    def bounding_box_selected(self) -> None:
        self._operation(
            "To Bounding Box", lambda rois: [to_bounding_box(roi) for roi in rois]
        )

    def convex_hull_selected(self) -> None:
        self._operation(
            "Convex Hull", lambda rois: [convex_hull(roi) for roi in rois]
        )

    def make_inverse_selected(self) -> None:
        image = self._current_image_2d()
        if image is None:
            self.summaryLabel.setText("No image layer selected.")
            return
        self._operation(
            "Make Inverse", lambda rois: [make_inverse(rois[0], image.shape)]
        )

    def translate_selected(self) -> None:
        drow, ok = QtWidgets.QInputDialog.getInt(self, "Translate", "Rows:", 0, -9999, 9999)
        if not ok:
            return
        dcol, ok = QtWidgets.QInputDialog.getInt(self, "Translate", "Columns:", 0, -9999, 9999)
        if not ok:
            return
        # Moving replaces the ROIs rather than adding copies: a translated ROI
        # is the same ROI somewhere else, and its uid says so.
        self._operation(
            "Translate",
            lambda rois: [translate(roi, drow, dcol) for roi in rois],
            consume=True,
        )

    def rescale_selected_to_current_frame(self) -> None:
        """P-5.5 — rewrite ROIs into the frame currently on screen.

        The explicit counterpart to A-13's refusal to measure through a
        transform: reprojection happens once, when asked for, and the result
        says which frame it now belongs to.
        """
        target = self._current_frame()
        if target is None:
            self.summaryLabel.setText("No image layer selected.")
            return

        def rescale(rois):
            out = []
            for roi in rois:
                source = self._set.frame(getattr(roi, "frame_uid", "") or "")
                if source is None:
                    raise ROIOperationError(
                        f"{roi.name!r} has no recorded frame, so there is "
                        "nothing to map it from"
                    )
                if source.frame_uid == target.frame_uid:
                    continue
                out.append(rescale_to_frame(roi, source, target))
            if not out:
                raise ROIOperationError("already in this frame.")
            return out

        self._operation("Rescale", rescale, consume=True)
        self._set = self._set.with_frame(target)

    # ----------------------------------------------------------------------
    # P-6.4 — between an ROI set and an image
    # ----------------------------------------------------------------------

    def create_selection(self) -> None:
        """ImageJ's *Create Selection*: the label image on screen becomes ROIs.

        Reads the plane currently displayed, so a labels result and a plain
        mask both work, and a stack yields the slice being looked at.
        """
        image = self._current_image_2d()
        if image is None:
            self.summaryLabel.setText("No image layer selected.")
            return
        array = np.asarray(image)
        if array.dtype.kind == "f" and not np.array_equal(array, array.astype(int)):
            self.summaryLabel.setText(
                "This looks like an intensity image, not labels. Segment it "
                "first, or threshold it to a mask."
            )
            return

        frame = self._current_frame()
        try:
            rois = rois_from_labels(
                array.astype(np.int32),
                name_prefix=self._set.name,
                position=self._plane_key(frame),
                frame_uid=frame.frame_uid if frame is not None else "",
            )
        except ROIOperationError as exc:
            self.summaryLabel.setText(str(exc))
            return
        if not rois:
            self.summaryLabel.setText("That image has no labelled regions.")
            return
        if frame is not None:
            self._set = self._set.with_frame(frame)
        self.add_rois(rois)
        self.summaryLabel.setText(f"Created {len(rois)} ROI(s) from labels.")

    def create_mask(self) -> None:
        """Publish the ROI set as a label image (A-17 full-frame exception).

        A `ProcessingResult`, not a viewer layer: it goes into the
        reconstruction list like every other result, so it can be saved,
        processed and measured by the same machinery as anything else (C-01).
        """
        image = self._current_image_2d()
        if image is None:
            self.summaryLabel.setText("No image layer selected.")
            return
        rois = self._selected_rois() or [
            roi for roi in self._model.rois if roi.visible
        ]
        if not rois:
            self.summaryLabel.setText("No visible ROI to draw.")
            return
        try:
            labels = labels_from_rois(rois, np.asarray(image).shape)
        except ROIOperationError as exc:
            self.summaryLabel.setText(str(exc))
            return

        frame = self._current_frame()
        row_scale, col_scale, unit = plane_scales(frame)
        result = ROIMaskResult(
            name=f"{self._set.name} mask",
            data=labels,
            axis_labels=["Y", "X"],
            axis_scales=[row_scale, col_scale],
            scale_unit=unit,
            roi_names=[roi.name for roi in rois],
        )
        self.sigResultProduced.emit(result, result.name)
        self.summaryLabel.setText(
            f"Published a label image of {len(rois)} ROI(s)."
        )

    # ----------------------------------------------------------------------
    # P-7 — the rest of ImageJ's ROI Manager
    # ----------------------------------------------------------------------

    def deselect(self) -> None:
        """Clear the selection.

        ImageJ has this as a button because so much else depends on it: with a
        selection, Measure and the operations act on it; without, on everything.
        Clicking empty space in a table does not reliably clear it.
        """
        self.table.clearSelection()
        self._selection_changed()

    def update_selected(self) -> None:
        """ImageJ's *Update*: replace the selected ROI's geometry with what is drawn.

        Its identity is kept — this is the same region, moved or redrawn — so
        measurements pushed earlier still refer to it, and its revision bumps
        so nothing serves a cached number for the new shape.
        """
        roi = self._selected_roi()
        if roi is None:
            return
        try:
            drawn = self._drawn_rois()
        except Exception as exc:
            self.summaryLabel.setText(str(exc))
            return
        if len(drawn) != 1:
            self.summaryLabel.setText(
                "Draw exactly one shape to update this ROI with."
            )
            return

        replacement = drawn[0]
        changes = {
            "roi_type": replacement.roi_type,
            "bounds": replacement.bounds,
            "vertices": replacement.vertices,
            "mask": replacement.mask,
        }
        try:
            self._commands.run(UpdateROI(roi.name, changes))
        except Exception as exc:
            self.summaryLabel.setText(f"Could not update: {exc}")
            return
        self._toolService.clear(self._toolToken)
        self.refresh_stats()
        self.summaryLabel.setText(f"Updated {roi.name!r} to the drawn shape.")

    def sort_rois(self) -> None:
        """ImageJ's *Sort*: order the list by name.

        The model's order, not the table's: sorting a column is a view, and
        this is the thing that gets exported, saved and measured in order.
        """
        ordered = sorted(self._model.rois, key=lambda roi: roi.name)
        if list(ordered) == list(self._model.rois):
            self.summaryLabel.setText("Already in name order.")
            return
        self._runOperation(
            "Sort", ordered, consumed=[roi.name for roi in self._model.rois]
        )

    def specify_roi(self) -> None:
        """ImageJ's *Specify…*: an ROI at exact numeric coordinates."""
        image = self._current_image_2d()
        shape = np.asarray(image).shape if image is not None else None
        chosen = ROISpecifyDialog.specify(shape, self)
        if chosen is None:
            return
        roi_type, bounds = chosen
        frame = self._current_frame()
        if frame is not None:
            self._set = self._set.with_frame(frame)
        self.add_rois([
            ROIRecord(
                name=self._model.unique_name(roi_type.capitalize()),
                roi_type=roi_type,
                bounds=bounds,
                source="specified",
                position=self._current_position(frame),
                frame_uid=frame.frame_uid if frame is not None else "",
            )
        ])

    def edit_properties(self) -> None:
        """ImageJ's *Properties…*, over one ROI or the whole selection."""
        rois = self._selected_rois()
        if not rois:
            roi = self._selected_roi()
            if roi is None:
                return
            rois = [roi]

        changes = ROIPropertiesDialog.edit(rois, self)
        if not changes:
            return

        style_changes = changes.pop("_style", None)
        name = changes.pop("name", None)
        if style_changes:
            # Merged onto each ROI's own style, not replacing it: a batch that
            # set only the group must not flatten five different colours into
            # the first one's.
            for roi in rois:
                base = roi.style or ROIStyle()
                self._commands.run(
                    SetProperties(
                        names=(roi.name,),
                        changes={"style": replace(base, **style_changes)},
                        label="Set style",
                    )
                )
        if changes:
            self._commands.run(
                SetProperties(names=tuple(r.name for r in rois), changes=changes)
            )
        if name and len(rois) == 1:
            try:
                self._commands.run(RenameROI(rois[0].name, name))
            except Exception as exc:
                self.summaryLabel.setText(str(exc))
        self.refresh_stats()

    def _filterChanged(self, text: str) -> None:
        """Hide rows whose name does not contain ``text``.

        A *view* filter: it hides rows, it does not change the set. Deleting
        "everything" while a filter is on would otherwise delete things the
        user cannot see, which is the way this feature usually goes wrong.
        """
        needle = text.strip().lower()
        column = self.column_index("Name")
        for row in range(self.table.rowCount()):
            item = self.table.item(row, column) if column >= 0 else None
            name = item.text().lower() if item is not None else ""
            self.table.setRowHidden(row, bool(needle) and needle not in name)

    def _tableMenu(self, position) -> None:
        """Right-click menu: the actions that act on a row."""
        menu = QtWidgets.QMenu(self.table)
        menu.addAction("Rename…", self.rename_selected)
        menu.addAction("Properties…", self.edit_properties)
        menu.addAction("Duplicate", self.duplicate_selected)
        menu.addAction("Delete", self.delete_selected)
        menu.addSeparator()
        menu.addAction("Measure", self.measure)
        menu.addAction("Deselect", self.deselect)
        menu.exec_(self.table.viewport().mapToGlobal(position))

    def remove_slice_info(self) -> None:
        """Detach every ROI from the slice it was captured on (ImageJ parity)."""
        self._commands.run(RemoveSliceInfo())
        self.refresh_stats()

    def _onTargetLayerChanged(self, layer) -> None:
        """Follow the broker's target: overlay alignment and statistics both."""
        try:
            self._overlay.set_target_layer(layer)
        except Exception:
            pass
        self.refresh_stats()

    def _on_overlay_clicked(self, uid: str, _modifiers=()) -> None:
        """Selection follows the click, in both directions."""
        self.select_roi_by_uid(uid)

    def _refresh_overlay(self) -> None:
        """Redraw the committed ROI set, aligned with the measured image."""
        try:
            self._overlay.set_rois(
                self._model.rois,
                selected_uids=self._selected_uids(),
                default_style=self._set.default_style,
            )
        except Exception:
            # The overlay is a convenience; it must never take the panel down.
            pass

    def _selected_uids(self) -> tuple[str, ...]:
        uids = []
        for item in self.table.selectedItems():
            key = self.table.item(item.row(), 0)
            uid = key.data(ROI_KEY_ROLE) if key is not None else None
            if uid:
                uids.append(str(uid))
        return tuple(dict.fromkeys(uids))

    def _selection_changed(self) -> None:
        try:
            self._overlay.set_selection(self._selected_uids())
        except Exception:
            pass

    def select_roi_by_uid(self, uid: str) -> None:
        """Select the row for an ROI clicked in the viewer."""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(ROI_KEY_ROLE) == uid:
                self.table.selectRow(row)
                return

    def _item_changed(self, item: QtWidgets.QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        uid = item.data(ROI_KEY_ROLE)
        roi = self._model.get_by_uid(str(uid)) if uid else None
        if roi is None:
            return
        self._commands.run(
            SetVisible(roi.name, item.checkState() == QtCore.Qt.Checked)
        )
        self.refresh_stats()

    def _drawn_points(self) -> list[ROIRecord]:
        """Every point currently placed, as **one** multipoint ROI.

        One record rather than one per point: a fiducial set is a thing, and
        splitting it into forty ROIs would make counting them, measuring their
        spacing, or naming the set as a whole impossible. Split it afterwards
        if the individual points are what is wanted.
        """
        try:
            points = self._toolService.points(self._toolToken)
        except Exception:
            return []
        if not points:
            return []
        pixels = self._world_to_pixels(np.asarray(points, dtype=float))
        frame = self._current_frame()
        if frame is not None:
            self._set = self._set.with_frame(frame)
        return [
            roi_from_points(
                pixels,
                name=self._model.unique_name("Points"),
                position=self._current_position(frame),
                frame_uid=frame.frame_uid if frame is not None else "",
            )
        ]

    def _drawn_rois(self) -> list[ROIRecord]:
        """Every shape this panel owns, as ROI records.

        All of them, not just the first: the drawing layer can hold several,
        and silently capturing one of five was indistinguishable from the
        others having failed.

        Only shapes this panel owns — a rectangle drawn for the Profile panel
        is not ours to capture.
        """
        frame = self._current_frame()
        if frame is not None:
            # Registered once per set, not copied onto every record.
            self._set = self._set.with_frame(frame)
        position = self._current_position(frame)
        rois: list[ROIRecord] = []
        for _index, shape_type, shape in self._toolService.shapes(self._toolToken):
            try:
                vertices = self._world_to_pixels(np.asarray(shape, dtype=np.float64))
                rois.append(
                    roi_from_shape(
                        vertices,
                        shape_type=shape_type,
                        name=self._model.unique_name("ROI"),
                        position=position,
                        frame_uid=frame.frame_uid if frame is not None else "",
                    )
                )
            except ValueError:
                # A degenerate or unsupported shape is skipped; the others in
                # the same capture still make it in.
                continue
        rois.extend(self._drawn_points())
        if not rois:
            raise ValueError("Draw a shape or place a point first.")
        return rois

    def _current_frame(self):
        """The plane the ROIs about to be captured belong to."""
        layer = self._active_image_layer()
        if layer is None:
            return None
        try:
            return frame_from_layer(layer, self._viewer)
        except Exception:
            return None

    def _current_position(self, frame) -> tuple[tuple[str, int], ...]:
        """Axis-labelled slice position, recorded only when asked for.

        ImageJ's default is that an ROI applies to every slice; associating it
        with the one it was drawn on is opt-in, and this matches that.
        """
        if frame is None or not self.associateSlicesCheck.isChecked():
            return ()
        try:
            return plane_position(self._viewer, frame)
        except Exception:
            return ()

    def _world_to_pixels(self, vertices: np.ndarray) -> np.ndarray:
        """Drawn shape vertices (world) → image pixel coordinates.

        Inverted through napari's own mapping. Dividing by ``layer.scale`` —
        which is what this did — is only correct for a layer with no offset and
        no rotation: it silently ignored ``translate``, so an ROI drawn on a
        translated layer was recorded at the wrong pixels, and ignored rotation
        entirely.
        """
        layer = self._active_image_layer()
        if layer is None:
            return vertices
        try:
            return np.asarray(
                [world_to_data(layer, vertex[:2]) for vertex in vertices],
                dtype=np.float64,
            )
        except Exception:
            self._logger_message("Could not map the drawn shape onto the image.")
            return vertices

    def _logger_message(self, message: str) -> None:
        self.summaryLabel.setText(message)

    def _selected_roi(self) -> ROIRecord | None:
        """The ROI behind the selected row, resolved by key rather than index.

        Row order stops matching model order the moment the table is sorted, so
        indexing the model by row number would rename or delete a different ROI
        than the one the user clicked.
        """
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        uid = item.data(ROI_KEY_ROLE) if item is not None else None
        roi = self._model.get_by_uid(str(uid)) if uid else None
        if roi is None:
            self.summaryLabel.setText("Select an ROI first.")
            return None
        return roi

    def _current_image_2d(self):
        layer = self._active_image_layer()
        if layer is None:
            return None
        data = np.asarray(layer.data)
        if data.ndim < 2:
            return None
        if data.ndim == 2:
            return data
        step = self._current_step(data.ndim)
        indexer = []
        for axis, size in enumerate(data.shape):
            if axis >= data.ndim - 2:
                indexer.append(slice(None))
            else:
                indexer.append(min(max(step[axis], 0), size - 1))
        return np.asarray(data[tuple(indexer)])

    def _current_step(self, ndim: int) -> tuple[int, ...]:
        try:
            step = tuple(int(v) for v in self._viewer.dims.current_step)
        except Exception:
            step = ()
        if len(step) < ndim:
            step = (*step, *(0 for _ in range(ndim - len(step))))
        return step

    def _active_image_layer(self):
        """The image being measured, from the broker.

        The broker holds this so every panel measures the same image. Falling
        back to ``active_image_layer()`` on each call is what allowed clicking
        the ROI overlay — which makes it napari's active layer — to silently
        redirect measurement to whatever image happened to be first.
        """
        target = self._toolService.target_image_layer
        if target is not None:
            return target
        # First use: seed the broker from the viewer, once.
        target = active_image_layer(self._viewer)
        if target is not None:
            self._toolService.target_image_layer = target
        return target

    def _format_value(self, value) -> str:
        """Format a cell, honouring the configured precision.

        Decimal places and scientific notation are ImageJ's, and are a display
        choice only: the value behind the cell — the one that sorts, and the
        one that is exported — is never rounded. Anything not a real number
        falls back to the shared Results-table formatting, so a bounds tuple or
        a note renders here exactly as it does there.
        """
        config = self._set.measurement_config
        # Counts are counts: a pixel area of 16 is not 16.000, so only real
        # numbers take the configured precision.
        if isinstance(value, bool) or not isinstance(value, (float, np.floating)):
            return format_table_value(value)
        number = float(value)
        if not np.isfinite(number):
            return format_table_value(value)
        if config.scientific:
            return f"{number:.{config.decimals}e}"
        return f"{number:.{config.decimals}f}"
