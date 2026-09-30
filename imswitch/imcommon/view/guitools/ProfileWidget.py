"""Interactive line, rectangle, stack and time profiles of napari image layers.

One widget for both applications: ImProcess's *Profile* panel and imcontrol's
*Line Profile* dock are this class. They used to be two implementations — the
imcontrol one a matplotlib canvas with a width box and nothing else — and every
fix made to one had to be remembered for the other. The hosts now differ only
in the options they construct it with (see ``__init__``):

* ImProcess keeps its pushes to the Results table and the Graph panel;
* imcontrol follows the live image as frames arrive and adds *Intensity vs T*,
  a self-updating trace of the mean intensity inside the drawn rectangle.

What is measured is explicit. With several image layers in a viewer — several
detectors in imcontrol, several results in ImProcess — the *Layer* chooser
says which one the curve comes from, can pin one by name, or can plot every
visible layer at once; the plot title always names the layer measured.
"""

from __future__ import annotations

import time
import warnings
import weakref

import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.algorithms.layer_selection import (
    active_image_layer,
    is_image_layer,
)
from imswitch.imcommon.algorithms.line_sampling import line_samples
from imswitch.imcommon.algorithms.profile_fits import (
    ExponentialFit,
    GaussianFit,
    ProfileFit,
    TwoGaussianFit,
    build_profile_record,
)
from imswitch.imcommon.model.plotting import (
    PlotPayload,
    PlotSeries,
    build_delta_x_record,
)
from imswitch.imcommon.model.result_records import (
    merge_columns,
    records_to_csv,
    series_to_csv,
)
from imswitch.imcommon.view.guitools.viewer_tools import ViewerToolService

#: Layer-chooser entries that are not a layer name.
_ACTIVE = "__active__"
_ALL = "__all__"

#: Modes whose region is drawn with the rectangle tool.
_RECTANGLE_MODES = ("rectangle", "zprofile", "timetrace")


def _roi_is_area(roi) -> bool:
    """True for a shape with an interior. A line has only its own profile."""
    from imswitch.imcommon.algorithms.roi_geometry import roi_capabilities

    try:
        return bool(roi_capabilities(roi.roi_type).is_area)
    except Exception:
        return False


def _qt_deleted(obj) -> bool:
    """True once Qt has destroyed ``obj``'s C++ side.

    The tool broker keeps calling a shape handler for as long as the panel's
    Python object lives, which can be longer than the panel. It calls from a
    Qt slot, out of which the RuntimeError of touching a destroyed widget
    escapes unhandled. (napari's own events need no such check: napari catches
    that error and disconnects the handler.)
    """
    try:
        from qtpy import sip

        return bool(sip.isdeleted(obj))
    except Exception:
        return False


def _weak(obj):
    """A callable returning ``obj`` while it lives — weakly when it can be."""
    try:
        return weakref.ref(obj)
    except TypeError:
        return lambda: obj


class _FlowLayout(QtWidgets.QLayout):
    """Left to right, wrapping onto a new row when the width runs out.

    The same controls sit in a wide ImProcess dock and in imcontrol's narrow
    left column; a fixed row would push the narrow one into a horizontal
    scrollbar. Hidden items take no room, so mode-specific controls can come
    and go.
    """

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items: list = []
        self._spacing = int(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):  # noqa: N802 - Qt naming
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802 - Qt naming
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802 - Qt naming
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802 - Qt naming
        return QtCore.Qt.Orientations(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        return self._arrange(QtCore.QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):  # noqa: N802 - Qt naming
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self):  # noqa: N802 - Qt naming
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802 - Qt naming
        size = QtCore.QSize()
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QtCore.QSize(
            margins.left() + margins.right(), margins.top() + margins.bottom()
        )

    def _arrange(self, rect, *, apply: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, row_height = area.x(), area.y(), 0
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            if row_height and x + hint.width() > area.right() + 1:
                x = area.x()
                y += row_height + self._spacing
                row_height = 0
            if apply:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), hint))
            x += hint.width() + self._spacing
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y() + margins.bottom()


class _LayerCombo(QtWidgets.QComboBox):
    """A combo box that re-reads the viewer's layers just before it opens.

    Layers are added, renamed, hidden and — in imcontrol, whenever a live
    layer changes dimensionality — recreated; listing them at the moment the
    user looks is the one time the list is guaranteed current.
    """

    sigAboutToShowPopup = QtCore.Signal()

    def showPopup(self):  # noqa: N802 - Qt naming
        self.sigAboutToShowPopup.emit()
        super().showPopup()


class _PlotWidget(pg.PlotWidget):
    """A plot that asks for a modest size rather than pyqtgraph's 640 x 480.

    imcontrol sizes its docks from what each panel asks for, and the default
    would give this panel twice the height of the plot it replaced. It still
    stretches into whatever room the dock has.
    """

    def sizeHint(self):  # noqa: N802 - Qt naming
        return QtCore.QSize(420, 280)


class ProfileWidget(QtWidgets.QWidget):
    """Draw line/rectangle ROIs on a napari viewer and plot their profiles."""

    sigResultPushed = QtCore.Signal(object, object)
    sigPlotPushed = QtCore.Signal(object)
    """One PlotPayload sent to the Graph panel, to sit alongside others."""
    sigModeChanged = QtCore.Signal(str)
    """The drawing mode, whenever it changes — for a host with tool buttons of its own."""

    #: Stable owner key for the shared drawing tool (see ViewerToolService).
    TOOL_OWNER = "profile"
    #: What the empty plot asks for.
    EMPTY_HINT = "Draw a line or rectangle in the viewer"
    #: The drawing modes offered, in button order. ``timetrace`` is the live
    #: *Intensity vs T* trace; hosts whose images do not change on their own
    #: leave it out.
    MODES = ("pan", "line", "rectangle", "zprofile")

    #: Button text and tooltip per mode.
    MODE_BUTTONS = {
        "pan": ("Pan", "Stop drawing; pan and zoom the viewer"),
        "line": ("Line", "Draw a line and plot the intensity along it"),
        "rectangle": (
            "Rectangle",
            "Draw a rectangle and plot its mean intensity along x and along y",
        ),
        "zprofile": (
            "Z profile",
            "Plot mean intensity through the stack axis over a rectangle "
            "(the whole frame when none is drawn) — ImageJ's Plot Z-axis Profile",
        ),
        "timetrace": (
            "Intensity vs T",
            "Plot the mean intensity inside a rectangle (the whole frame when "
            "none is drawn) against time, sampled at the interval set here while "
            "the image updates",
        ),
    }

    #: How often a live host re-reads the image for the profile on show.
    LIVE_REFRESH_MS = 250
    #: Samples an *Intensity vs T* trace keeps; ten hours at 1 Hz.
    TRACE_MAX_SAMPLES = 36000
    #: Curve colours when several layers are plotted together.
    SERIES_COLORS = (
        "#e6194b", "#3cb44b", "#4363d8", "#f58231",
        "#911eb4", "#42d4f4", "#f032e6", "#bfef45",
    )

    def __init__(
        self,
        napariViewer,
        *args,
        modes=None,
        pushTargets: bool = True,
        liveUpdates: bool = False,
        defaultUnit: str = "px",
        toolOwner: str | None = None,
        emptyHint: str | None = None,
        **kwargs,
    ):
        """
        Parameters
        ----------
        modes
            Drawing modes to offer, from ``MODE_BUTTONS``; ``pan`` is always
            included. Defaults to ``MODES``.
        pushTargets
            Offer *Push to table* / *Push to graph*. Only a host that connects
            ``sigResultPushed`` / ``sigPlotPushed`` should.
        liveUpdates
            Re-plot when the measured layer's data is replaced (new frames),
            and offer a *Live* switch to freeze the plot.
        defaultUnit
            Distance unit for a layer with a pixel scale but no
            ``metadata["scale_unit"]``. imcontrol's live layers are scaled by
            the detector pixel size in micrometres and carry no metadata.
        toolOwner, emptyHint
            Override ``TOOL_OWNER`` / ``EMPTY_HINT``.
        """
        super().__init__(*args, **kwargs)
        self._viewer = napariViewer
        modes = tuple(self.MODES if modes is None else modes)
        self._modes = ("pan",) + tuple(
            mode for mode in modes if mode != "pan" and mode in self.MODE_BUTTONS
        )
        self._toolOwner = str(toolOwner or self.TOOL_OWNER)
        self._emptyHint = str(emptyHint or self.EMPTY_HINT)
        self._defaultUnit = str(defaultUnit or "px")
        self._liveUpdates = bool(liveUpdates)
        self._toolService = ViewerToolService.for_viewer(napariViewer)
        # Register only; the tool is acquired when the user picks a mode.
        self._toolToken = self._toolService.register(self._toolOwner)
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
        #: The layer each entry of ``_current_record_inputs`` was measured on.
        self._record_sources: list[str] = []
        #: The pen each curve in ``_last_payload`` was drawn with.
        self._series_pens: list = []
        self._measurementRegion: pg.LinearRegionItem | None = None
        self._measurementValues: tuple[float, float] | None = None
        #: Late-bound by the host; see ImProcessMainView's ROI wiring.
        self._roiManagerWidget = None
        self._rois: list = []
        #: The image layer napari last had selected. Drawing makes the Shapes
        #: layer napari's active one, and falling back to "the first image"
        #: from there is how a two-detector setup ended up profiling the
        #: detector nobody had selected.
        self._rememberedActive = None
        self._lastTargets: list = []
        #: Layers whose data events we listen to, and whether one has fired
        #: since the plot was last drawn (``liveUpdates``).
        self._watched: list = []
        self._dataDirty = False
        # Intensity vs T: sample times (s since start) and one value list per
        # layer name, aligned with the times.
        self._traceBounds = None
        self._traceStart = time.monotonic()
        self._traceTimes: list[float] = []
        self._traceSeries: dict[str, list[float]] = {}

        # -- what is measured ------------------------------------------------
        self.layerCombo = _LayerCombo()
        self.layerCombo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.layerCombo.setMinimumContentsLength(14)
        self.layerCombo.setToolTip(
            "Image layer to measure: the one selected in the viewer, one "
            "chosen by name, or every visible layer at once (one curve each)"
        )

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

        # -- drawing modes ---------------------------------------------------
        self.modeButtons = QtWidgets.QButtonGroup(self)
        self._modeButtons: dict[str, QtWidgets.QPushButton] = {}
        for mode in self._modes:
            text, tip = self.MODE_BUTTONS[mode]
            self._modeButtons[mode] = self._makeModeButton(
                text, mode, checked=(mode == "pan"), tooltip=tip
            )
        self.panButton = self._modeButtons.get("pan")
        self.lineButton = self._modeButtons.get("line")
        self.rectangleButton = self._modeButtons.get("rectangle")
        self.zProfileButton = self._modeButtons.get("zprofile")
        self.timeTraceButton = self._modeButtons.get("timetrace")
        self.clearButton = QtWidgets.QPushButton("Clear")
        self.clearButton.setToolTip("Remove the shape drawn for this panel")

        # -- Intensity vs T --------------------------------------------------
        self.traceIntervalSpin = QtWidgets.QDoubleSpinBox()
        self.traceIntervalSpin.setRange(0.1, 600.0)
        self.traceIntervalSpin.setDecimals(1)
        self.traceIntervalSpin.setSingleStep(0.5)
        self.traceIntervalSpin.setValue(1.0)
        self.traceIntervalSpin.setSuffix(" s")
        self.traceIntervalSpin.setToolTip("Time between two samples of the trace")
        self.tracePauseButton = QtWidgets.QPushButton("Pause")
        self.tracePauseButton.setCheckable(True)
        self.tracePauseButton.setToolTip(
            "Stop sampling; the trace resumes on the same time axis"
        )
        self.traceRestartButton = QtWidgets.QPushButton("Restart")
        self.traceRestartButton.setToolTip("Discard the trace and start again from t = 0")
        self._traceIntervalBox = self._group(
            QtWidgets.QLabel("Every"), self.traceIntervalSpin
        )
        self._traceControls = (
            self._traceIntervalBox,
            self.tracePauseButton,
            self.traceRestartButton,
        )

        # -- profile options and output --------------------------------------
        self.measureButton = QtWidgets.QPushButton("Measure Δx")
        self.measureButton.setCheckable(True)
        self.measureButton.setToolTip(
            "Show two draggable vertical markers and measure their horizontal distance"
        )
        self.liveCheck = QtWidgets.QCheckBox("Live")
        self.liveCheck.setChecked(True)
        self.liveCheck.setToolTip(
            "Re-plot as new frames arrive (at most a few times a second). "
            "Untick to freeze the current profile."
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
        self.saveButton = QtWidgets.QPushButton("Save summary…")
        self.saveButton.setToolTip(
            "Save one row per curve — length, min, max, mean and the fit — as CSV"
        )
        self.saveDataButton = QtWidgets.QPushButton("Save data…")
        self.saveDataButton.setToolTip(
            "Save the plotted curves themselves (an x and a y column each) as CSV"
        )

        self.fitSummary = QtWidgets.QLabel("")
        self.fitSummary.setWordWrap(True)
        self.fitSummary.setStyleSheet("color:#888; font-size:8pt;")
        self.measurementSummary = QtWidgets.QLabel("")
        self.measurementSummary.setWordWrap(True)
        self.measurementSummary.setStyleSheet("color:#b8860b; font-size:8pt;")

        self.plot = _PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.addLegend()

        # -- layout ----------------------------------------------------------
        self._sourceBox = self._group(
            QtWidgets.QLabel("Source"), self.sourceCombo, self.roiPlotLabel, self.roiPlotCombo
        )
        # Offered once an ROI manager is bound; with nothing but "Drawn" in it
        # the chooser is clutter.
        self._sourceBox.setVisible(False)
        items = [
            self._group(QtWidgets.QLabel("Layer"), self.layerCombo),
            self._sourceBox,
            *self._modeButtons.values(),
            self.clearButton,
            *self._traceControls,
            self._group(QtWidgets.QLabel("Width"), self.widthSpinBox),
            self._group(QtWidgets.QLabel("Fit"), self.fitCombo),
            self.measureButton,
            self.liveCheck,
            self.pushButton,
            self.pushGraphButton,
            self.saveButton,
            self.saveDataButton,
        ]
        toolbar = QtWidgets.QWidget()
        flow = _FlowLayout(toolbar)
        for item in items:
            flow.addWidget(item)
        for control in self._traceControls:
            control.setVisible(False)
        self.liveCheck.setVisible(self._liveUpdates)
        self.pushButton.setVisible(bool(pushTargets))
        self.pushGraphButton.setVisible(bool(pushTargets))

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(toolbar)
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.measurementSummary)
        layout.addWidget(self.fitSummary)
        self.setLayout(layout)

        # -- timers ----------------------------------------------------------
        self._traceTimer = QtCore.QTimer(self)
        self._traceTimer.timeout.connect(self._traceTick)
        self._followTimer = QtCore.QTimer(self)
        self._followTimer.setInterval(self.LIVE_REFRESH_MS)
        self._followTimer.timeout.connect(self._followTick)
        if self._liveUpdates:
            self._followTimer.start()

        # -- signals ---------------------------------------------------------
        self.layerCombo.currentIndexChanged.connect(self._layerChoiceChanged)
        self.layerCombo.sigAboutToShowPopup.connect(self.refreshLayerChoices)
        self.sourceCombo.currentIndexChanged.connect(self._profileSourceChanged)
        self.roiPlotCombo.currentIndexChanged.connect(self._profileSourceChanged)
        self.modeButtons.buttonClicked.connect(self._modeChanged)
        self.clearButton.clicked.connect(self._clearShapes)
        self.measureButton.toggled.connect(self._measurementToggled)
        self.liveCheck.toggled.connect(self._liveToggled)
        self.widthSpinBox.valueChanged.connect(self._refresh)
        self.fitCombo.currentIndexChanged.connect(self._refresh)
        self.traceIntervalSpin.valueChanged.connect(self._updateTraceTimer)
        self.tracePauseButton.toggled.connect(self._tracePauseToggled)
        self.traceRestartButton.clicked.connect(self._restartTrace)
        self.pushButton.clicked.connect(self._onPushToTable)
        self.pushGraphButton.clicked.connect(self._onPushToGraph)
        self.saveButton.clicked.connect(self._onSaveCSV)
        self.saveDataButton.clicked.connect(self._onSaveData)
        self._toolService.on_shapes_changed(self._toolToken, self._shapesChanged)
        # Through the broker so release() tears these down too; connecting
        # straight to the viewer left callbacks firing after close. Each is
        # optional: test doubles and headless viewers lack some of them.
        for signal, handler in (
            (lambda: self._viewer.dims.events.current_step, self._onDimsStep),
            (lambda: self._viewer.layers.events.inserted, self._onLayersChanged),
            (lambda: self._viewer.layers.events.removed, self._onLayersChanged),
            (lambda: self._viewer.layers.selection.events.active, self._onActiveLayerChanged),
        ):
            try:
                self._toolService.on_viewer_event(self._toolToken, signal(), handler)
            except Exception:
                pass

        self.refreshLayerChoices()
        self._drawEmpty()

    # -- host API ---------------------------------------------------------

    def modes(self) -> tuple[str, ...]:
        """The drawing modes this panel offers, ``pan`` first."""
        return self._modes

    def mode(self) -> str:
        return self._mode

    def selectMode(self, mode: str) -> None:
        """Switch mode exactly as clicking its button does.

        For a host with tool buttons of its own — imcontrol's Viewer Tools —
        so both sets of buttons drive one drawing tool instead of two.
        """
        button = self._modeButtons.get(mode)
        if button is None:
            raise ValueError(f"{mode!r} is not one of this panel's modes {self._modes}")
        button.setChecked(True)
        self._modeChanged(button)

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
        self.refreshLayerChoices()
        super().showEvent(event)

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        """Release the drawing tool, our viewer callbacks and the timers."""
        self._traceTimer.stop()
        self._followTimer.stop()
        self._watchLayers([])
        try:
            self._toolService.release(self._toolToken)
        except Exception:
            pass
        super().closeEvent(event)

    # -- small helpers ----------------------------------------------------

    @staticmethod
    def _group(*widgets) -> QtWidgets.QWidget:
        """Widgets that belong together, kept on one row by the flow layout."""
        box = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        for widget in widgets:
            layout.addWidget(widget)
        return box

    def _setPlotLabels(self, title: str, x_label: str, y_label: str) -> None:
        """Title and label the plot, remembering both for pushed payloads."""
        self._last_plot_title = title
        self._last_plot_labels = (x_label, y_label)
        self.plot.setTitle(title[:1].upper() + title[1:])
        self.plot.setLabel("bottom", x_label)
        self.plot.setLabel("left", y_label)

    def _makeModeButton(self, text: str, mode: str, checked: bool = False, tooltip: str = ""):
        button = QtWidgets.QPushButton(text)
        button.setCheckable(True)
        button.setProperty("profileMode", mode)
        button.setChecked(checked)
        if tooltip:
            button.setToolTip(tooltip)
        self.modeButtons.addButton(button)
        return button

    def _pen(self, index: int, multi: bool, single, *, dashed: bool = False):
        color = self.SERIES_COLORS[index % len(self.SERIES_COLORS)] if multi else single
        style = QtCore.Qt.DashLine if dashed else QtCore.Qt.SolidLine
        return pg.mkPen(color, width=2, style=style)

    # -- drawing modes ----------------------------------------------------

    def _modeChanged(self, button):
        mode = button.property("profileMode")
        self._mode = mode
        # Picking a drawing mode means drawing is what is wanted, so the
        # source returns to Drawn. Leaving an ROI named in the chooser while
        # the plot came from a freshly drawn shape would label the plot with
        # a region it was not measured on.
        self._returnToDrawn()
        # Re-acquire so shapes drawn from here on are attributed to this panel,
        # and clear only ours — this used to wipe the shared layer, taking the
        # ROI statistics panel's rectangle with it.
        self._toolToken = self._toolService.acquire(self._toolOwner)
        self._toolService.clear(self._toolToken)
        # A Z profile and a time trace are measured over a rectangle, so they
        # draw with the same tool; only what gets plotted differs.
        self._toolService.set_mode(
            self._toolToken, "rectangle" if mode in _RECTANGLE_MODES else mode
        )
        self._traceTimer.stop()
        for control in self._traceControls:
            control.setVisible(mode == "timetrace")
        # A trace samples on its own clock; Pause is its freeze, not Live.
        self.liveCheck.setVisible(self._liveUpdates and mode != "timetrace")
        self._drawEmpty()
        if mode == "zprofile":
            # Unlike the in-plane profiles this one is meaningful with no ROI
            # at all (the whole frame), as it is in ImageJ.
            self._plotZProfile(None)
        elif mode == "timetrace":
            self._startTrace(None)
        self.sigModeChanged.emit(mode)

    def _clearShapes(self):
        self._toolService.clear(self._toolToken)
        self._drawEmpty()
        if self._mode == "zprofile":
            self._plotZProfile(None)
        elif self._mode == "timetrace":
            self._startTrace(None)

    def _returnToDrawn(self) -> None:
        if self.sourceCombo.currentIndex() == 0:
            return
        self.sourceCombo.blockSignals(True)
        self.sourceCombo.setCurrentIndex(0)
        self.sourceCombo.blockSignals(False)
        self.roiPlotCombo.setVisible(False)
        self.roiPlotLabel.setVisible(False)

    def _shapesChanged(self):
        if _qt_deleted(self):
            return
        # A shape drawn while an ROI is the source is not what is on the plot;
        # the mode buttons are the way back to drawing, and they say so.
        if self.selectedROI() is not None:
            return
        if self._mode == "zprofile":
            self._plotZProfile(self._findFirstShape("rectangle"))
            return
        if self._mode == "timetrace":
            bounds = self._findFirstShape("rectangle")
            # napari reports one drawn rectangle more than once; only a
            # different region starts a new trace.
            if not self._sameBounds(bounds, self._traceBounds):
                self._startTrace(bounds)
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

    def _refresh(self, *_args):
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
        elif self._last_kind == "timetrace":
            # Redrawn from what was sampled; sampling runs on its own clock.
            self._drawTrace()

    def _onDimsStep(self, _event=None):
        self._refresh()

    def _drawEmpty(self):
        self.measureButton.setChecked(False)
        self.measureButton.setEnabled(False)
        self.pushGraphButton.setEnabled(False)
        self._removeMeasurementRegion(clear_values=True)
        self._last_kind = None
        self._last_payload = []
        self._last_fit_curves = []
        self._current_record_inputs = []
        self._record_sources = []
        self._series_pens = []
        self.fitSummary.setText("")
        self.plot.clear()
        self._setPlotLabels(
            self._emptyHint,
            f"Distance ({self._distanceUnit()})",
            "Intensity",
        )

    # -- which layer is measured ------------------------------------------

    def _layerChoice(self):
        return self.layerCombo.currentData() or _ACTIVE

    @staticmethod
    def _layerName(layer) -> str:
        return str(getattr(layer, "name", "") or "image")

    def _imageLayers(self) -> list:
        """Every layer that can be measured, in viewer order."""
        try:
            return [layer for layer in self._viewer.layers if is_image_layer(layer)]
        except Exception:
            return []

    def _inViewer(self, layer) -> bool:
        try:
            return any(candidate is layer for candidate in self._viewer.layers)
        except Exception:
            return False

    def _resolveActiveLayer(self):
        """napari's active image layer, else the image layer last selected.

        Only then the first image in the list, as ``active_image_layer`` does.
        """
        try:
            active = self._viewer.layers.selection.active
        except Exception:
            active = None
        if is_image_layer(active):
            self._rememberedActive = _weak(active)
            return active
        remembered = self._rememberedActive() if self._rememberedActive else None
        if remembered is not None and is_image_layer(remembered) and self._inViewer(remembered):
            return remembered
        return active_image_layer(self._viewer)

    def _activeImageLayer(self):
        """The layer measured — the first of several with *All visible layers*."""
        choice = self._layerChoice()
        if choice == _ALL:
            layers = self._imageLayers()
            active = self._resolveActiveLayer()
            if active is not None and any(layer is active for layer in layers):
                return active
            return layers[0] if layers else None
        if choice != _ACTIVE:
            return next(
                (layer for layer in self._imageLayers() if self._layerName(layer) == choice),
                None,
            )
        return self._resolveActiveLayer()

    def _targetLayers(self) -> list:
        """Every layer a curve is drawn for."""
        if self._layerChoice() == _ALL:
            return self._imageLayers()
        layer = self._activeImageLayer()
        return [layer] if layer is not None else []

    def _noLayerText(self) -> str:
        choice = self._layerChoice()
        if choice not in (_ACTIVE, _ALL):
            return f"{choice} is not shown in the viewer."
        return "No image layer selected."

    def _activeItemText(self) -> str:
        layer = self._resolveActiveLayer()
        return "Active layer" if layer is None else f"Active: {self._layerName(layer)}"

    def refreshLayerChoices(self) -> None:
        """Re-list the viewer's image layers, keeping the current choice."""
        current = self._layerChoice()
        names = [self._layerName(layer) for layer in self._imageLayers()]
        self.layerCombo.blockSignals(True)
        self.layerCombo.clear()
        self.layerCombo.addItem(self._activeItemText(), _ACTIVE)
        self.layerCombo.addItem("All visible layers", _ALL)
        for name in dict.fromkeys(names):
            self.layerCombo.addItem(name, name)
        if current not in (_ACTIVE, _ALL) and current not in names:
            # A layer chosen by name stays chosen while it is away: imcontrol
            # recreates a live layer under the same name, and a choice that
            # silently fell back to "Active" would measure something else.
            self.layerCombo.addItem(f"{current} (not shown)", current)
        self.layerCombo.setCurrentIndex(max(0, self.layerCombo.findData(current)))
        self.layerCombo.blockSignals(False)

    def _targetsChanged(self) -> bool:
        """Whether the layers measured differ from the last time this was asked."""
        targets = self._targetLayers()
        changed = len(targets) != len(self._lastTargets) or any(
            new is not old for new, old in zip(targets, self._lastTargets)
        )
        self._lastTargets = list(targets)
        return changed

    def _onTargetsChanged(self) -> None:
        self.layerCombo.setItemText(0, self._activeItemText())
        # A trace keeps one curve per layer name, so a new target simply
        # starts a curve of its own; everything else is re-measured.
        if self._last_kind != "timetrace":
            self._refresh()

    def _onActiveLayerChanged(self, _event=None):
        self._resolveActiveLayer()  # remembers an image layer when one is picked
        self.layerCombo.setItemText(0, self._activeItemText())
        if self._targetsChanged():
            self._onTargetsChanged()

    def _onLayersChanged(self, _event=None):
        self.refreshLayerChoices()
        if self._targetsChanged():
            self._onTargetsChanged()

    def _layerChoiceChanged(self, _index=None):
        self._targetsChanged()
        if self._mode == "timetrace":
            self._startTrace(self._traceBounds)
        else:
            self._refresh()

    # -- following a live image -------------------------------------------

    def _watchLayers(self, layers) -> None:
        """Listen for new data on exactly ``layers``."""
        keep = []
        for ref in self._watched:
            layer = ref()
            if layer is None:
                continue
            if any(layer is target for target in layers):
                keep.append(ref)
                continue
            try:
                layer.events.data.disconnect(self._onLayerData)
            except Exception:
                pass
        watched = [ref() for ref in keep]
        for layer in layers:
            if any(layer is known for known in watched):
                continue
            try:
                layer.events.data.connect(self._onLayerData)
            except Exception:
                continue
            keep.append(_weak(layer))
        self._watched = keep

    def _onLayerData(self, _event=None):
        # Called for every frame; only flag it; _followTick does the work at
        # a rate the plot can keep up with.
        self._dataDirty = True

    def _liveToggled(self, live: bool) -> None:
        if live:
            self._dataDirty = True

    def _isDraggingMeasurement(self) -> bool:
        """True while the Δx markers are being dragged.

        Redrawing removes and re-adds them, which ends the drag — at a few
        redraws a second the markers could not be moved at all.
        """
        region = self._measurementRegion
        if region is None:
            return False
        if getattr(region, "moving", False):
            return True
        return any(getattr(line, "moving", False) for line in getattr(region, "lines", ()))

    def _followTick(self) -> None:
        targets = self._targetLayers()
        self._watchLayers(targets if self.liveCheck.isChecked() else [])
        if self._targetsChanged():
            self.layerCombo.setItemText(0, self._activeItemText())
            self._dataDirty = True
        if not (self._dataDirty and self.liveCheck.isChecked()):
            return
        if not self.isVisible() or self._isDraggingMeasurement():
            return  # still dirty: drawn once it can be
        self._dataDirty = False
        if self._last_kind in (None, "timetrace"):
            return
        self._refresh()

    # -- Intensity vs T ---------------------------------------------------

    @staticmethod
    def _sameBounds(a, b) -> bool:
        if a is None or b is None:
            return a is None and b is None
        return bool(np.allclose(np.asarray(a, dtype=float), np.asarray(b, dtype=float)))

    def _startTrace(self, bounds) -> None:
        """Begin a new trace over ``bounds`` — the whole frame when None."""
        self._traceBounds = None if bounds is None else tuple(float(v) for v in bounds)
        self._traceTimes = []
        self._traceSeries = {}
        self._traceStart = time.monotonic()
        self._sampleTrace()
        self._drawTrace()
        self._updateTraceTimer()

    def _restartTrace(self) -> None:
        self._startTrace(self._traceBounds)

    def _tracePauseToggled(self, paused: bool) -> None:
        self.tracePauseButton.setText("Resume" if paused else "Pause")
        self._updateTraceTimer()

    def _updateTraceTimer(self, *_args) -> None:
        if self._mode == "timetrace" and not self.tracePauseButton.isChecked():
            self._traceTimer.start(int(round(self.traceIntervalSpin.value() * 1000)))
        else:
            self._traceTimer.stop()

    def _traceTick(self) -> None:
        if self._mode != "timetrace":
            self._traceTimer.stop()
            return
        self._sampleTrace()
        if not self._isDraggingMeasurement():
            self._drawTrace()

    def _sampleTrace(self) -> None:
        """Append the current mean of the region, one value per measured layer."""
        values: dict[str, float] = {}
        for layer in self._targetLayers():
            image = self._currentImage2D(layer)
            if image is None:
                continue
            rows, cols = self._roiSliceForBounds(self._traceBounds, image.shape, layer)
            if rows is None:
                continue
            region = np.asarray(image[rows, cols], dtype=float)
            if region.size == 0:
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                values[self._layerName(layer)] = float(np.nanmean(region))

        count = len(self._traceTimes)
        self._traceTimes.append(time.monotonic() - self._traceStart)
        for name in values:
            # A layer that starts being measured mid-trace gets a curve of its
            # own, empty before it appeared — not one spliced onto another's.
            self._traceSeries.setdefault(name, [np.nan] * count)
        for name, series in self._traceSeries.items():
            series.append(values.get(name, np.nan))

        excess = len(self._traceTimes) - self.TRACE_MAX_SAMPLES
        if excess > 0:
            del self._traceTimes[:excess]
            for series in self._traceSeries.values():
                del series[:excess]

    def _drawTrace(self) -> None:
        self._beginPlot("timetrace")
        title = "frame mean vs time" if self._traceBounds is None else "ROI mean vs time"
        self._setPlotLabels(title, "Time (s)", "Mean intensity")
        times = np.asarray(self._traceTimes, dtype=float)
        multi = len(self._traceSeries) > 1
        for index, (name, values) in enumerate(self._traceSeries.items()):
            y = np.asarray(values, dtype=float)
            finite = np.isfinite(y)
            if not finite.any():
                continue
            duration = float(times[finite][-1] - times[finite][0])
            pen = self._pen(index, multi, "#1f77b4")
            # Markers while there are few samples: a single point has no line.
            marks = (
                {"symbol": "o", "symbolSize": 5, "symbolBrush": pen.color(), "symbolPen": None}
                if times.size <= 120
                else {}
            )
            self._addCurve(
                name, times, y, pen,
                ("time-trace", times, y, float(finite.sum()), duration, "s"),
                name,
                **marks,
            )
        if not self._last_payload:
            self._setMeasurementAvailable(False)
            self.fitSummary.setText(
                self._noLayerText() if not self._targetLayers()
                else "The rectangle does not overlap the image."
            )
            return
        self._finishPlot()

    # -- plotting ---------------------------------------------------------

    def _beginPlot(self, kind) -> None:
        self._last_kind = kind
        self._removeMeasurementRegion(clear_values=False)
        self.plot.clear()
        self._last_payload = []
        self._last_fit_curves = []
        self._current_record_inputs = []
        self._record_sources = []
        self._series_pens = []

    def _addCurve(self, name, x, y, pen, record, source, **options) -> None:
        # connect="finite" leaves a gap where a trace has no value, rather
        # than a line drawn straight across it.
        self.plot.plot(x, y, pen=pen, name=name, connect="finite", **options)
        self._last_payload.append((name, x, y))
        self._current_record_inputs.append(record)
        self._record_sources.append(source)
        self._series_pens.append(pen)

    def _finishPlot(self) -> None:
        self._applyFits()
        self._setMeasurementAvailable(True)
        # Say what was measured, in the plot itself: with two detectors the
        # curve alone cannot tell you which one it came from.
        sources = list(dict.fromkeys(self._record_sources))
        if sources:
            base = self._last_plot_title[:1].upper() + self._last_plot_title[1:]
            where = sources[0] if len(sources) == 1 else f"{len(sources)} layers"
            self.plot.setTitle(f"{base} — {where}")

    def _plotLineProfile(self, endpoints):
        self._beginPlot("line")
        self._setPlotLabels(
            "line profile", f"Distance ({self._distanceUnit()})", "Intensity"
        )
        if endpoints is None:
            self._setMeasurementAvailable(False)
            self.fitSummary.setText("")
            return
        layers = self._targetLayers()
        if not layers:
            self._setMeasurementAvailable(False)
            self.fitSummary.setText(self._noLayerText())
            return

        multi = len(layers) > 1
        width = self.widthSpinBox.value()
        for index, layer in enumerate(layers):
            image = self._currentImage2D(layer)
            if image is None:
                continue
            # ROI vertices come from the Shapes layer in world coordinates, while
            # the image layer carries the physical scale (e.g. nm/px) and offset.
            # Undo both to get pixel indices for sampling — otherwise, once the
            # scale stops being ~1, the endpoints land outside the array and the
            # profile reads all zeros ("no signal"). Per layer, because two
            # detectors rarely share a pixel size.
            row_scale, col_scale = self._visiblePixelScales(layer)
            (r0, c0), (r1, c1) = self._worldToPixel(layer, endpoints)
            profile = self._computeLineProfile(image, r0, c0, r1, c1, width)
            if profile is None:
                continue
            length_px = float(np.hypot(r1 - r0, c1 - c0))
            length_scaled = float(np.hypot((r1 - r0) * row_scale, (c1 - c0) * col_scale))
            x = np.linspace(0.0, length_scaled, profile.size)
            label = self._layerName(layer)
            self._addCurve(
                label if multi else "line", x, profile, self._pen(index, multi, "r"),
                ("line", x, profile, length_px, length_scaled, self._distanceUnit(layer)),
                label,
            )
        if not self._last_payload:
            self._setMeasurementAvailable(False)
            self.fitSummary.setText("")
            return
        self._finishPlot()

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
        self._sourceBox.setVisible(panel is not None)

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

        self._beginPlot(None)

        layer = self._activeImageLayer()
        image = self._currentImage2D(layer)
        if image is None:
            self._setMeasurementAvailable(False)
            self.fitSummary.setText(self._noLayerText())
            return
        source = self._layerName(layer)

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

        row_scale, col_scale = self._visiblePixelScales(layer)
        unit = self._distanceUnit(layer)
        # One scale for a path that runs in both directions at once; the mean
        # of the two is the honest single number when they differ.
        path_scale = (float(row_scale) + float(col_scale)) / 2.0
        plot_kind = self.roiPlotCombo.currentData() if _roi_is_area(roi) else "line"
        pen = pg.mkPen("r", width=2)

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
                self._addCurve(
                    label, x, profile, pen,
                    (label, x, profile, float(profile.size),
                     float(x[-1] if x.size else 0.0), unit),
                    source,
                )
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
                self._addCurve(
                    name, x, profile, pen,
                    (name, x, profile, float(profile.size),
                     float(x[-1] if x.size else 0.0), unit),
                    source,
                )
        except Exception as exc:
            self._setMeasurementAvailable(False)
            self.fitSummary.setText(f"Could not profile this ROI: {exc}")
            return

        self._finishPlot()

    def _plotRectangleProfiles(self, bounds):
        self._beginPlot("rectangle")
        self._setPlotLabels(
            "rectangle profile", f"Distance ({self._distanceUnit()})", "Mean intensity"
        )
        if bounds is None:
            self._setMeasurementAvailable(False)
            self.fitSummary.setText("")
            return
        layers = self._targetLayers()
        if not layers:
            self._setMeasurementAvailable(False)
            self.fitSummary.setText(self._noLayerText())
            return

        multi = len(layers) > 1
        for index, layer in enumerate(layers):
            image = self._currentImage2D(layer)
            if image is None:
                continue
            # Bounds are world coordinates from the Shapes layer; converted to
            # pixel indices of this layer before cropping (see _plotLineProfile).
            rows, cols = self._roiSliceForBounds(bounds, image.shape, layer)
            if rows is None:
                continue
            roi = np.asarray(image[rows, cols], dtype=float)
            row_scale, col_scale = self._visiblePixelScales(layer)
            x_profile = roi.mean(axis=0)
            y_profile = roi.mean(axis=1)
            x = np.arange(x_profile.size) * col_scale
            y = np.arange(y_profile.size) * row_scale
            unit = self._distanceUnit(layer)
            label = self._layerName(layer)
            x_name, y_name = (f"{label} x", f"{label} y") if multi else ("x", "y")
            self._addCurve(
                x_name, x, x_profile, self._pen(index, multi, "r"),
                ("rectangle-x", x, x_profile, float(x_profile.size),
                 float(x_profile.size * col_scale), unit),
                label,
            )
            self._addCurve(
                y_name, y, y_profile, self._pen(index, multi, "#00cc44", dashed=multi),
                ("rectangle-y", y, y_profile, float(y_profile.size),
                 float(y_profile.size * row_scale), unit),
                label,
            )
        if not self._last_payload:
            self._setMeasurementAvailable(False)
            self.fitSummary.setText("")
            return
        self._finishPlot()

    def _plotZProfile(self, bounds):
        """Mean intensity through the stack axis over a rectangle.

        ImageJ's *Plot Z-axis Profile*: the in-plane profiles answer "how does
        intensity vary across the field", this one answers "how does it vary
        through the stack" — bleaching over time, an axial PSF, a z-extent.
        With no rectangle drawn it measures the whole frame, as ImageJ does.
        """
        self._beginPlot("zprofile")

        stacks = [
            layer for layer in self._targetLayers()
            if np.ndim(getattr(layer, "data", None)) >= 3
        ]
        if not stacks:
            self._setMeasurementAvailable(False)
            self.plot.setTitle("Z profile")
            self.fitSummary.setText("Select a stack (3D or more) to profile.")
            return

        multi = len(stacks) > 1
        for index, layer in enumerate(stacks):
            data = layer.data
            axis, axis_label = self._stackAxis(data, layer)
            rows, cols = self._roiSliceForBounds(bounds, data.shape[-2:], layer)
            if rows is None:
                continue

            # Every other non-spatial axis stays at what the viewer is showing,
            # so profiling Z on a TZYX stack profiles the timepoint on screen.
            selection = [slice(None)] * data.ndim
            step = self._stepFor(data.ndim)
            for other in range(data.ndim - 2):
                if other != axis:
                    selection[other] = min(max(step[other], 0), data.shape[other] - 1)
            selection[-2], selection[-1] = rows, cols
            volume = np.asarray(data[tuple(selection)], dtype=float)
            volume = volume.reshape(volume.shape[0], -1)

            means = np.nanmean(volume, axis=1)
            scale = self._axisScale(layer, axis)
            z = np.arange(means.size, dtype=float) * scale
            unit = self._distanceUnit(layer) if scale != 1.0 else "slice"
            if not self._last_payload:
                self._setPlotLabels(
                    f"{axis_label} profile", f"{axis_label} ({unit})", "Mean intensity"
                )
            label = self._layerName(layer)
            self._addCurve(
                label if multi else "mean", z, means, self._pen(index, multi, "#1f77b4"),
                (
                    f"{axis_label.lower()}-profile",
                    z,
                    means,
                    float(means.size),
                    float(means.size * scale),
                    unit,
                ),
                label,
            )
        if not self._last_payload:
            self._setMeasurementAvailable(False)
            self.fitSummary.setText("")
            return
        self._finishPlot()

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

    def _roiSliceForBounds(self, bounds, shape, layer=None) -> tuple[slice | None, slice | None]:
        """Row/column slices for a rectangle, or the whole frame when none."""
        height, width = int(shape[0]), int(shape[1])
        if bounds is None:
            return slice(0, height), slice(0, width)
        r0, c0, r1, c1 = bounds
        (pr0, pc0), (pr1, pc1) = self._worldToPixel(layer, ((r0, c0), (r1, c1)))
        rlo, rhi = sorted((int(round(pr0)), int(round(pr1))))
        clo, chi = sorted((int(round(pc0)), int(round(pc1))))
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

    def _stepFor(self, ndim: int) -> tuple[int, ...]:
        """The viewer's position along each axis of an ``ndim`` layer.

        napari aligns layers on their *trailing* axes, so a 3D layer in a
        viewer that also holds a 4D one reads the last three entries of the
        step, not the first three.
        """
        step = self._currentStep(ndim)
        if len(step) >= ndim:
            return tuple(step[len(step) - ndim:])
        return tuple(step) + tuple(0 for _ in range(ndim - len(step)))

    @staticmethod
    def _axisScale(layer, axis: int) -> float:
        try:
            scale = tuple(float(value) for value in layer.scale)
        except Exception:
            return 1.0
        return scale[axis] if 0 <= axis < len(scale) else 1.0

    # -- the Δx measurement -----------------------------------------------

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

    # -- fits -------------------------------------------------------------

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
            pen = pg.mkPen(self._fitColor(index), width=2, style=QtCore.Qt.DashLine)
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

    def _fitColor(self, index: int):
        """A fit's colour: its curve's when several are plotted.

        With one curve a contrasting colour sets the fit off from the data;
        with several, a contrasting colour per fit made it impossible to tell
        which curve a fit belonged to.
        """
        if len(self._last_payload) > 1 and index < len(self._series_pens):
            return self._series_pens[index].color()
        return pg.intColor(index + 3)

    # -- reading the image ------------------------------------------------

    def _currentImage2D(self, layer=None):
        """The plane of ``layer`` (the measured one by default) on screen."""
        if layer is None:
            layer = self._activeImageLayer()
        if layer is None:
            return None
        data = getattr(layer, "data", None)
        if data is None:
            return None
        if not hasattr(data, "ndim"):
            data = np.asarray(data)
        if data.ndim < 2:
            return None
        if data.ndim == 2:
            return np.asarray(data)
        # Index the plane first: converting a lazy stack to an array before
        # slicing would read all of it.
        step = self._stepFor(data.ndim)
        leading = tuple(
            min(max(step[axis], 0), data.shape[axis] - 1)
            for axis in range(data.ndim - 2)
        )
        return np.asarray(data[leading])

    def _visiblePixelScales(self, layer=None) -> tuple[float, float]:
        if layer is None:
            layer = self._activeImageLayer()
        if layer is None:
            return 1.0, 1.0
        try:
            scale = tuple(float(v) for v in layer.scale)
        except Exception:
            return 1.0, 1.0
        if len(scale) < 2:
            return 1.0, 1.0
        return (scale[-2] or 1.0), (scale[-1] or 1.0)

    @staticmethod
    def _pixelOffsets(layer) -> tuple[float, float]:
        try:
            translate = tuple(float(v) for v in layer.translate)
        except Exception:
            return 0.0, 0.0
        if len(translate) < 2:
            return 0.0, 0.0
        return translate[-2], translate[-1]

    def _worldToPixel(self, layer, points):
        """World ``(row, col)`` points as pixel indices of ``layer``.

        Scale and translation of the last two axes: what napari applies to an
        image layer, and what makes two detectors with different pixel sizes
        or offsets line up under one drawn shape.
        """
        if layer is None:
            layer = self._activeImageLayer()
        row_scale, col_scale = self._visiblePixelScales(layer)
        row_offset, col_offset = self._pixelOffsets(layer) if layer is not None else (0.0, 0.0)
        return tuple(
            ((float(row) - row_offset) / row_scale, (float(col) - col_offset) / col_scale)
            for row, col in points
        )

    def _distanceUnit(self, layer=None) -> str:
        if layer is None:
            layer = self._activeImageLayer()
        try:
            unit = (layer.metadata or {}).get("scale_unit")
        except Exception:
            unit = None
        if not unit:
            unit = "px" if self._visiblePixelScales(layer) == (1.0, 1.0) else self._defaultUnit
        if unit == "um":
            return "µm"
        return str(unit or "px")

    # -- output -----------------------------------------------------------

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
        source = self._sourceName()
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
        records = self._buildOutputRecords()

        columns = []
        for record in records:
            columns = merge_columns(columns, record.keys())

        self.sigResultPushed.emit(columns, records)

    def _onSaveCSV(self):
        if not self._current_record_inputs:
            return
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

    def _onSaveData(self):
        """Write the curves on the plot — profiles and fits — as CSV."""
        if not self._last_payload:
            return
        filepath, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save profile data", "", "CSV (*.csv)"
        )
        if not filepath:
            return
        csv_text = series_to_csv(list(self._last_payload) + list(self._last_fit_curves))
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(csv_text)
        except Exception as e:
            self.fitSummary.setText(f"Error saving CSV: {e}")

    def _sourceName(self) -> str:
        """Name of the layer(s) this profile was measured on."""
        sources = list(dict.fromkeys(self._record_sources))
        if sources:
            return " + ".join(sources)
        return self._layerName(self._activeImageLayer())

    def _buildOutputRecords(self) -> list[dict]:
        """Build profile/fit rows plus the optional manual Δx measurement."""
        fallback = self._sourceName()
        aligned = len(self._record_sources) == len(self._current_record_inputs)
        records = []
        for index, rec_input in enumerate(self._current_record_inputs):
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
            # Which layer the profile was measured on. The Results table
            # accumulates, so rows pushed from two reconstructions — or two
            # detectors — are otherwise indistinguishable apart from the values.
            source = self._record_sources[index] if aligned else fallback
            records.append({"source": source, **record})

        if self._measurementValues is not None and self.measureButton.isChecked():
            unit = (
                str(self._current_record_inputs[0][5])
                if self._current_record_inputs else "px"
            )
            titles = {"line": "Line Profile", "rectangle": "Rectangle Projections"}
            title = titles.get(self._last_kind) or (
                self._last_plot_title[:1].upper() + self._last_plot_title[1:]
            )
            # A stack or time axis is not a distance; say what the plot says.
            x_axis = (
                self._last_plot_labels[0]
                if self._last_kind in ("zprofile", "timetrace")
                else f"Distance ({unit})"
            )
            records.append({
                "source": fallback,
                **build_delta_x_record(
                    title,
                    x_axis,
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


__all__ = ["ProfileWidget"]
