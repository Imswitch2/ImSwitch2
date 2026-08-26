"""Reusable modeless workspace for measurement and CGH correction workflows."""

from __future__ import annotations

from enum import Enum
from typing import Any,Mapping,Sequence

from qtpy import QtCore,QtWidgets

from ...cgh.execution.session_model import (
    CGHSessionInspection,CGHWorkingRoundState,
)
from ...cgh.execution.status import CGHResultState,CGHStatus
from ...cgh.feedback.model import (
    FeedbackCapability,FeedbackChangeKind,FeedbackInspection,FeedbackStatus,
)
from ...cgh.feedback.parameters import INTENSITY_ANALYSIS_PARAMS
from ...cgh.measurement_metrics import IntensityAnalysis
from ...engine.parameters.spec import make_display_name
from ..widgets.fields import ParamForm
from ..measurement.localization_view import MeasurementLocalizationView
from .session_views import (
    FeedbackMetricsHistoryView,
    FeedbackTargetView,
    InspectView,
    PositionCorrectionView,
    format_target_summary,
)


_MUTED_COLOR = "#888"
_OK_COLOR = "#286b2d"
_WARNING_COLOR = "#a66a00"
_POSITION_NOT_CORRECTED = "not_corrected"
_POSITION_CORRECTED = "corrected"


class MeasurementsAction(str,Enum):
    """Host operations requested by :class:`MeasurementsCorrectionsWindow`."""

    ACQUIRE = "acquire"
    LOAD = "load"
    LOCALIZATION_RUN = "localization_run"
    LOCALIZATION_ACCEPT = "localization_accept"
    LOCALIZATION_REUSE = "localization_reuse"
    FEEDBACK_PARAMETERS = "feedback_parameters"
    INTENSITY_APPLY = "intensity_apply"
    INTENSITY_RESET = "intensity_reset"
    POSITION_APPLY = "position_apply"
    POSITION_SET_ACTIVE = "position_set_active"
    POSITION_CLEAR = "position_clear"
    COMPUTE_ADAPTED = "compute_adapted_hologram"
    RESET_TO_ROUND = "reset_to_round"
    PROPAGATE_SELECTED = "propagate_selected"
    INSPECT = "inspect"  # compatibility with the previous details dialog
    AUTOMATIC_START = "automatic_start"
    AUTOMATIC_STOP = "automatic_stop"


class CGHSessionWindow(QtWidgets.QDialog):
    """Modeless CGH session workspace with measurement, feedback and inspection."""

    sigActionRequested = QtCore.Signal(str,object)

    def __init__(
        self,
        *,
        status: FeedbackStatus,
        inspection: FeedbackInspection,
        session_inspection: CGHSessionInspection,
        cgh_status: CGHStatus,
        localization_context: Mapping[str, Any] | None=None,
        detectors: Sequence[str]=(),
        current_detector: str | None=None,
        cgh_summary: Mapping[str, Any] | None=None,
        title: str="CGH Session",
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowModality(QtCore.Qt.NonModal)
        self.setWindowFlag(QtCore.Qt.Window,True)
        self.setWindowTitle(str(title))
        self.setWindowFlags(
            self.windowFlags()
            | QtCore.Qt.WindowMinimizeButtonHint
            | QtCore.Qt.WindowMaximizeButtonHint
        )
        self.setMinimumSize(1120,720)
        self.resize(1500,920)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose,True)

        self._status = status
        self._inspection = inspection  # compatibility view used by legacy callers
        self._session_inspection = session_inspection
        self._cgh_status = cgh_status
        self._cgh_summary = dict(cgh_summary or {})
        self._cgh_computing = False
        self._candidate_current = False
        self._candidate_metrics: IntensityAnalysis | None = None
        self._selected_position_context = _POSITION_CORRECTED
        self._selected_round_key: str | None = None
        self._propagation_round_index: int | None = None
        self._propagation_cache = {}
        self._pending_propagation_key = None
        self._compute_adapted_buttons = []
        self._adaptation_status_labels = []
        self._automatic_feedback_available = False
        self._automatic_feedback_reason = (
            "Automatic feedback requires a measurement provider and automatic frame upload."
        )
        self._automatic_operation_active = False
        self._automatic_operation_owner = False
        self._automatic_operation_stopping = False
        self._automatic_progress_text = ""

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8,8,8,8)
        outer.setSpacing(8)

        outer.addWidget(self._build_session_header())

        self.workspace_tabs = QtWidgets.QTabWidget(self)
        measure_tab = QtWidgets.QWidget(self.workspace_tabs)
        measure_layout = QtWidgets.QVBoxLayout(measure_tab)
        measure_layout.setContentsMargins(0,0,0,0)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal,measure_tab)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_measurement_panel(
            status,localization_context,detectors,current_detector,
        ))
        splitter.addWidget(self._build_feedback_panel(status))
        splitter.setStretchFactor(0,1)
        splitter.setStretchFactor(1,1)
        splitter.setSizes([700,700])
        measure_layout.addWidget(splitter,1)
        self.workspace_tabs.addTab(measure_tab,"Measure & Correct")

        self.inspect_view = InspectView(self.workspace_tabs)
        self.inspect_view.sigPropagationRequested.connect(
            self._request_selected_propagation,
        )
        self.inspect_view.propagation_view.sigPadSizeChanged.connect(
            lambda _value:self._refresh_selected_propagation()
        )
        self.workspace_tabs.addTab(self.inspect_view,"Inspect")
        outer.addWidget(self.workspace_tabs,1)

        footer = QtWidgets.QHBoxLayout()
        footer.addStretch(1)
        close_button = QtWidgets.QPushButton("Close")
        close_button.setFixedHeight(24)
        close_button.clicked.connect(self.close)
        footer.addWidget(close_button)
        outer.addLayout(footer)

        # QSplitter.setSizes() before show is only a sizing hint. Once Qt has
        # performed the real layout, copy the actual Metrics History height
        # to the localization lower pane so both sides open symmetrically.
        self._initial_lower_panes_aligned = False

        self.set_session_state(
            status,inspection,session_inspection,cgh_status,
            localization_context,cgh_summary,
        )

    def showEvent(self,event) -> None:
        super().showEvent(event)
        if self._initial_lower_panes_aligned:
            return
        self._initial_lower_panes_aligned = True
        QtCore.QTimer.singleShot(0,self._align_initial_lower_panes)

    def _align_initial_lower_panes(self) -> None:
        sizes = self.intensity_splitter.sizes()
        if len(sizes) < 2 or int(sizes[1]) <= 0:
            return
        self.measurement_view.set_lower_pane_height(int(sizes[1]))

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_session_header(self) -> QtWidgets.QWidget:
        header = QtWidgets.QWidget(self)
        layout = QtWidgets.QVBoxLayout(header)
        layout.setContentsMargins(2,0,2,0)
        layout.setSpacing(2)

        first = QtWidgets.QHBoxLayout()
        first.setSpacing(6)

        self.position_selector_label = QtWidgets.QLabel("Position:")
        self.position_selector = QtWidgets.QComboBox()
        self.position_selector.setMinimumWidth(120)
        self.position_selector.addItem("Not Corrected",_POSITION_NOT_CORRECTED)
        self.position_selector.addItem("Corrected",_POSITION_CORRECTED)
        self.position_selector.currentIndexChanged.connect(
            self._on_position_selection_changed,
        )
        self.position_selector_label.setVisible(False)
        self.position_selector.setVisible(False)
        first.addWidget(self.position_selector_label)
        first.addWidget(self.position_selector)

        self.round_selector_label = QtWidgets.QLabel("Round:")
        first.addWidget(self.round_selector_label)
        self.round_selector = QtWidgets.QComboBox()
        self.round_selector.setMinimumWidth(120)
        self.round_selector.currentIndexChanged.connect(
            self._on_round_selection_changed,
        )
        first.addWidget(self.round_selector)

        self.reset_round_button = QtWidgets.QPushButton("Reset to this round")
        self.reset_round_button.setFixedHeight(22)
        self.reset_round_button.clicked.connect(self._reset_selected_round)
        first.addWidget(self.reset_round_button)
        first.addStretch(1)
        layout.addLayout(first)

        second = QtWidgets.QHBoxLayout()
        second.setSpacing(6)
        second.addWidget(QtWidgets.QLabel("Target:"))
        self.target_summary_label = QtWidgets.QLabel("—")
        self.target_summary_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse,
        )
        self.target_summary_label.setSizePolicy(
            QtWidgets.QSizePolicy.Maximum,QtWidgets.QSizePolicy.Preferred,
        )
        second.addWidget(self.target_summary_label)
        second.addWidget(QtWidgets.QLabel("Feedback:"))
        self.feedback_summary_label = QtWidgets.QLabel("—")
        self.feedback_summary_label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self.feedback_summary_label.setSizePolicy(
            QtWidgets.QSizePolicy.Maximum,QtWidgets.QSizePolicy.Preferred,
        )
        second.addWidget(self.feedback_summary_label)

        second.addStretch(1)
        layout.addLayout(second)
        return header

    def _build_measurement_panel(
        self,
        status: FeedbackStatus,
        localization_context: Mapping[str, Any] | None,
        detectors: Sequence[str],
        current_detector: str | None,
    ) -> QtWidgets.QWidget:
        panel = QtWidgets.QGroupBox("Measurement")
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(8,10,8,8)
        layout.setSpacing(7)

        self.measurement_view = MeasurementLocalizationView(
            parameters=status.localization_params,
            context=localization_context,
            detectors=detectors,
            current_detector=current_detector,
            show_metrics=False,
            show_accept=True,
            parent=panel,
        )
        self.measurement_view.sigAcquireRequested.connect(
            lambda detector:self._emit(
                MeasurementsAction.ACQUIRE,{
                    "detector":str(detector),
                    "reuse_previous_localization":self._reuse_previous_localization(),
                },
            )
        )
        self.measurement_view.sigLoadRequested.connect(
            lambda:self._emit(
                MeasurementsAction.LOAD,{
                    "reuse_previous_localization":self._reuse_previous_localization(),
                },
            )
        )
        self.measurement_view.sigRunRequested.connect(
            lambda parameters:self._emit(
                MeasurementsAction.LOCALIZATION_RUN,
                {"parameters":dict(parameters)},
            )
        )
        self.measurement_view.sigCandidateStateChanged.connect(
            self._on_candidate_state_changed,
        )
        self.measurement_view.sigAcceptRequested.connect(
            self._accept_localization_candidate,
        )

        layout.addWidget(self.measurement_view,1)

        # Host-specific intensity analysis belongs beside the result image: the
        # integration parameter is then immediately adjacent to the Integration
        # visualization. Calibration/standalone localization simply do not add
        # this optional widget.
        analysis_widget = QtWidgets.QWidget()
        analysis_layout = QtWidgets.QVBoxLayout(analysis_widget)
        analysis_layout.setContentsMargins(0,0,0,0)
        analysis_layout.setSpacing(4)

        analysis_heading = QtWidgets.QLabel("Intensity analysis")
        analysis_font = analysis_heading.font()
        analysis_font.setBold(True)
        analysis_heading.setFont(analysis_font)
        analysis_layout.addWidget(analysis_heading)

        form = ParamForm(
            name="intensity_analysis",
            definitions=INTENSITY_ANALYSIS_PARAMS,
            use_subsection=False,
            per_row=1,
            editor_width=64,
            show_complementary=False,
        )
        form.set_values(status.intensity_params,emit=False)
        form.sigValueChanged.connect(self._on_intensity_parameter_changed)
        form_widget = QtWidgets.QWidget(analysis_widget)
        form_layout = QtWidgets.QGridLayout(form_widget)
        form_layout.setContentsMargins(0,0,0,0)
        form_layout.setHorizontalSpacing(5)
        form_layout.setVerticalSpacing(3)
        form.add_to_grid(form_layout,0)
        analysis_layout.addWidget(form_widget)
        self.intensity_form = form

        metrics_layout = QtWidgets.QGridLayout()
        metrics_layout.setContentsMargins(0,0,0,0)
        metrics_layout.setHorizontalSpacing(5)
        metrics_layout.setVerticalSpacing(2)
        metrics_layout.addWidget(QtWidgets.QLabel("Uniformity"),0,0)
        self.measurement_uniformity_label = QtWidgets.QLabel("—")
        metrics_layout.addWidget(self.measurement_uniformity_label,0,1)
        metrics_layout.addWidget(QtWidgets.QLabel("Efficiency"),1,0)
        self.measurement_efficiency_label = QtWidgets.QLabel("—")
        metrics_layout.addWidget(self.measurement_efficiency_label,1,1)
        metrics_layout.setColumnStretch(2,1)
        for value_label in (
            self.measurement_uniformity_label,self.measurement_efficiency_label,
        ):
            font = value_label.font()
            font.setBold(True)
            value_label.setFont(font)
        analysis_layout.addLayout(metrics_layout)
        self.measurement_view.add_result_widget(analysis_widget)
        return panel

    def _build_feedback_panel(self,status: FeedbackStatus) -> QtWidgets.QWidget:
        panel = QtWidgets.QGroupBox("Feedback")
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(8,10,8,8)
        layout.setSpacing(7)

        self.feedback_tabs = QtWidgets.QTabWidget()
        self.feedback_tabs.addTab(self._build_intensity_tab(status),"Intensity")
        self.feedback_tabs.addTab(self._build_position_tab(),"Position")
        layout.addWidget(self.feedback_tabs,1)
        return panel

    def _build_intensity_tab(self,status: FeedbackStatus) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(5,7,5,5)
        layout.setSpacing(7)

        top = QtWidgets.QWidget(tab)
        top_layout = QtWidgets.QHBoxLayout(top)
        top_layout.setContentsMargins(0,0,0,0)
        top_layout.setSpacing(8)

        target_box = QtWidgets.QGroupBox("Adapted target")
        target_layout = QtWidgets.QVBoxLayout(target_box)
        self.feedback_target_view = FeedbackTargetView(target_box)
        target_layout.addWidget(self.feedback_target_view,1)
        top_layout.addWidget(target_box,1)

        feedback_controls = QtWidgets.QGroupBox("Feedback controls")
        feedback_controls.setMinimumWidth(175)
        feedback_controls.setMaximumWidth(220)
        controls_layout = QtWidgets.QVBoxLayout(feedback_controls)
        controls_layout.setContentsMargins(8,7,8,7)
        controls_layout.setSpacing(5)

        self.reuse_localization_checkbox = QtWidgets.QCheckBox(
            "Reuse previous localization"
        )
        self.reuse_localization_checkbox.setChecked(True)
        controls_layout.addWidget(self.reuse_localization_checkbox)
        controls_layout.addSpacing(3)

        manual_label = QtWidgets.QLabel("Manual")
        manual_font = manual_label.font()
        manual_font.setBold(True)
        manual_label.setFont(manual_font)
        controls_layout.addWidget(manual_label)

        self.intensity_apply_button = QtWidgets.QPushButton("Apply adaptation")
        self.intensity_apply_button.setFixedHeight(24)
        self.intensity_apply_button.clicked.connect(
            lambda _checked=False:self._emit(MeasurementsAction.INTENSITY_APPLY)
        )
        controls_layout.addWidget(self.intensity_apply_button)
        compute_button = self._make_compute_adapted_button()
        compute_button.setFixedHeight(24)
        controls_layout.addWidget(compute_button)

        controls_layout.addSpacing(5)
        automatic_label = QtWidgets.QLabel("Automatic")
        automatic_font = automatic_label.font()
        automatic_font.setBold(True)
        automatic_label.setFont(automatic_font)
        controls_layout.addWidget(automatic_label)

        rounds_row = QtWidgets.QHBoxLayout()
        rounds_row.setContentsMargins(0,0,0,0)
        rounds_row.setSpacing(5)
        rounds_row.addWidget(QtWidgets.QLabel("Rounds"))
        self.loop_rounds_spin = QtWidgets.QSpinBox()
        self.loop_rounds_spin.setRange(1,100)
        self.loop_rounds_spin.setValue(5)
        self.loop_rounds_spin.setFixedWidth(64)
        rounds_row.addWidget(self.loop_rounds_spin)
        rounds_row.addStretch(1)
        controls_layout.addLayout(rounds_row)

        self.loop_run_button = QtWidgets.QPushButton("Start")
        self.loop_stop_button = QtWidgets.QPushButton("Stop")
        self.loop_run_button.setFixedHeight(24)
        self.loop_stop_button.setFixedHeight(24)
        self.loop_run_button.clicked.connect(
            lambda _checked=False:self._emit(
                MeasurementsAction.AUTOMATIC_START,{
                    "rounds":int(self.loop_rounds_spin.value()),
                    "detector":self.measurement_view.current_detector or "",
                    "reuse_previous_localization":self._reuse_previous_localization(),
                },
            )
        )
        self.loop_stop_button.clicked.connect(
            lambda _checked=False:self._emit(MeasurementsAction.AUTOMATIC_STOP)
        )
        controls_layout.addWidget(self.loop_run_button)
        controls_layout.addWidget(self.loop_stop_button)
        self._refresh_automatic_controls()
        controls_layout.addStretch(1)
        top_layout.addWidget(feedback_controls,0)

        history = QtWidgets.QGroupBox("Metrics history")
        history.setMinimumHeight(0)
        history_layout = QtWidgets.QVBoxLayout(history)
        self.metrics_history_view = FeedbackMetricsHistoryView(history)
        history_layout.addWidget(self.metrics_history_view,1)

        self.intensity_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical,tab)
        self.intensity_splitter.setChildrenCollapsible(True)
        self.intensity_splitter.addWidget(top)
        self.intensity_splitter.addWidget(history)
        self.intensity_splitter.setCollapsible(1,True)
        self.intensity_splitter.setStretchFactor(0,1)
        self.intensity_splitter.setStretchFactor(1,0)
        # Match the localization side: visualization dominant by default, with
        # a compact non-zero lower pane that can still be fully collapsed.
        self.intensity_splitter.setSizes([700,150])
        layout.addWidget(self.intensity_splitter,1)
        return tab

    def _build_position_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(tab)
        layout.setContentsMargins(5,7,5,5)
        layout.setSpacing(8)

        # Main visualization.
        viewer = QtWidgets.QGroupBox("Position correction")
        viewer_layout = QtWidgets.QVBoxLayout(viewer)
        viewer_layout.setContentsMargins(8,7,8,7)

        self.position_correction_view = PositionCorrectionView(viewer)
        viewer_layout.addWidget(self.position_correction_view,1)
        layout.addWidget(viewer,1)

        # Match the intensity tab: workflow controls live in a narrow vertical
        # panel on the right while the visualization keeps the dominant area.
        controls = QtWidgets.QGroupBox("Position controls")
        controls.setMinimumWidth(175)
        controls.setMaximumWidth(220)
        controls_layout = QtWidgets.QVBoxLayout(controls)
        controls_layout.setContentsMargins(8,7,8,7)
        controls_layout.setSpacing(5)

        self.position_status_label = QtWidgets.QLabel("No correction")
        self.position_status_label.setWordWrap(True)
        controls_layout.addWidget(self.position_status_label)
        controls_layout.addSpacing(3)

        self.position_apply_button = QtWidgets.QPushButton(
            "Apply adaptation"
        )
        self.position_apply_button.setFixedHeight(24)
        self.position_apply_button.clicked.connect(
            lambda _checked=False:self._emit(
                MeasurementsAction.POSITION_APPLY
            )
        )
        controls_layout.addWidget(self.position_apply_button)

        compute_button = self._make_compute_adapted_button()
        compute_button.setFixedHeight(24)
        controls_layout.addWidget(compute_button)

        controls_layout.addSpacing(5)

        self.position_toggle_button = QtWidgets.QPushButton("Enable")
        self.position_clear_button = QtWidgets.QPushButton("Clear")
        for button in (
            self.position_toggle_button,
            self.position_clear_button,
        ):
            button.setFixedHeight(24)
            controls_layout.addWidget(button)

        self.position_toggle_button.clicked.connect(self._toggle_position)
        self.position_clear_button.clicked.connect(
            lambda _checked=False:self._emit(
                MeasurementsAction.POSITION_CLEAR
            )
        )

        controls_layout.addStretch(1)
        layout.addWidget(controls,0)
        return tab

    def _make_compute_adapted_button(self) -> QtWidgets.QPushButton:
        button = QtWidgets.QPushButton("Compute adapted hologram")
        button.setMinimumHeight(28)
        button.clicked.connect(
            lambda _checked=False:self._emit(MeasurementsAction.COMPUTE_ADAPTED)
        )
        self._compute_adapted_buttons.append(button)
        return button

    def _make_adaptation_status_label(self) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel("No adaptation pending")
        label.setWordWrap(True)
        label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        self._adaptation_status_labels.append(label)
        return label

    @staticmethod
    def _placeholder_view(
        title: str,text: str,*,minimum_height: int=150,
    ) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox(str(title))
        layout = QtWidgets.QVBoxLayout(box)
        label = QtWidgets.QLabel(str(text))
        label.setAlignment(QtCore.Qt.AlignCenter)
        label.setStyleSheet("color: %s;" % _MUTED_COLOR)
        label.setMinimumHeight(int(minimum_height))
        layout.addWidget(label,1)
        return box

    def reject(self) -> None:
        if self._automatic_operation_active:
            return
        super().reject()

    def closeEvent(self,event) -> None:
        if self._automatic_operation_active:
            event.ignore()
            return
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Host-facing state updates
    # ------------------------------------------------------------------

    def configure_detectors(
        self,detectors: Sequence[str],current_detector: str | None=None,
    ) -> None:
        self.measurement_view.configure_detectors(detectors,current_detector)

    def set_session_state(
        self,
        status: FeedbackStatus,
        inspection: FeedbackInspection,
        session_inspection: CGHSessionInspection,
        cgh_status: CGHStatus,
        localization_context: Mapping[str, Any] | None=None,
        cgh_summary: Mapping[str, Any] | None=None,
    ) -> None:
        """Refresh from authoritative backend state while preserving round view."""
        follow_latest = (
            self._selected_position_context == _POSITION_CORRECTED
            and (
                self._selected_round_key is None
                or self._selected_round_key == self._current_round_key()
            )
        )
        self._status = status
        self._inspection = inspection
        self._session_inspection = session_inspection
        self._cgh_status = cgh_status
        if cgh_summary is not None:
            self._cgh_summary = dict(cgh_summary)
        self.measurement_view.set_context(localization_context)
        self.intensity_form.set_values(status.intensity_params,emit=False)
        self._populate_position_selector()
        self._populate_round_selector(follow_latest=follow_latest)
        self._refresh_header()
        self._sync_selected_round()
        self._refresh_status_controls()
        self._refresh_feedback_visualizations()
        if self._automatic_operation_active:
            self._apply_automatic_interaction_lock()

    def set_measurement_busy(self,busy: bool,text: str="") -> None:
        if self._selected_is_current_context():
            self.measurement_view.set_measurement_busy(busy,text)

    def set_measurement_error(self,error: Any) -> None:
        if self._selected_is_current_context():
            self.measurement_view.set_measurement_error(error)

    def set_localization_result(
        self,
        result: Any,
        parameters: Mapping[str,Any],
        *,
        metrics: IntensityAnalysis | None=None,
    ) -> None:
        if not self._selected_is_interactive():
            return
        self.measurement_view.set_result(result,parameters)
        self._candidate_metrics = metrics
        self._refresh_metrics_display()

    def set_localization_error(self,error: Any) -> None:
        if not self._selected_is_current_context():
            return
        self._candidate_metrics = None
        self.measurement_view.set_error(error)
        self._refresh_metrics_display()

    def show_inspection(self,inspection: FeedbackInspection) -> None:
        """Compatibility action: the dedicated Inspect tab replaces the old dialog."""
        self._inspection = inspection
        self.workspace_tabs.setCurrentWidget(self.inspect_view)

    def set_cgh_computing(self,computing: bool) -> None:
        self._cgh_computing = bool(computing)
        text = (
            "Computing..." if self._cgh_computing
            else "Compute adapted hologram"
        )
        for button in self._compute_adapted_buttons:
            button.setText(text)
        self._refresh_header()
        self._sync_selected_round()
        self._refresh_status_controls()
        if self._automatic_operation_active:
            self._apply_automatic_interaction_lock()

    def set_propagation_result(self,round_index: int,image: Any) -> None:
        key = self._pending_propagation_key
        self._pending_propagation_key = None
        if key is None or int(key[1]) != int(round_index):
            return
        self._propagation_cache[key] = image
        self.inspect_view.propagation_view.set_busy(False)
        self._refresh_selected_propagation()

    def set_propagation_error(self,error: Any) -> None:
        key = self._pending_propagation_key
        self._pending_propagation_key = None
        if key is not None and key == self._selected_propagation_key():
            self.inspect_view.propagation_view.set_error(error)
        else:
            self._refresh_selected_propagation()

    def set_automatic_feedback_available(
        self,available: bool,reason: str="",
    ) -> None:
        self._automatic_feedback_available = bool(available)
        if reason:
            self._automatic_feedback_reason = str(reason)
        self._refresh_automatic_controls()

    def set_automatic_operation_state(
        self,
        active: bool,
        *,
        owner: bool=False,
        stopping: bool=False,
        progress: str="",
    ) -> None:
        """Lock the workspace while an SLM-wide automatic operation runs.

        Every interactive control is disabled while active. Only the Stop
        button in the owning feedback window remains available.
        """
        self._automatic_operation_active = bool(active)
        self._automatic_operation_owner = bool(owner) and bool(active)
        self._automatic_operation_stopping = bool(stopping) and bool(active)
        self._automatic_progress_text = str(progress or "")
        self._apply_automatic_interaction_lock()

    def _interactive_widgets_for_automatic_lock(self):
        types = (
            QtWidgets.QAbstractButton,
            QtWidgets.QComboBox,
            QtWidgets.QSpinBox,
            QtWidgets.QDoubleSpinBox,
            QtWidgets.QLineEdit,
            QtWidgets.QTabBar,
            QtWidgets.QAbstractSlider,
        )
        return [widget for widget in self.findChildren(QtWidgets.QWidget)
                if isinstance(widget,types)]

    def _apply_automatic_interaction_lock(self) -> None:
        active = self._automatic_operation_active
        for widget in self._interactive_widgets_for_automatic_lock():
            if widget is self.loop_stop_button:
                continue
            if active:
                widget.setEnabled(False)
            else:
                widget.setEnabled(True)

        if active:
            self.loop_stop_button.setEnabled(
                self._automatic_operation_owner
                and not self._automatic_operation_stopping
            )
            self.loop_run_button.setEnabled(False)
        else:
            self._sync_selected_round()
            self._refresh_status_controls()
            self._refresh_localization_commit_controls()
            self._refresh_automatic_controls()

    def _refresh_automatic_controls(self) -> None:
        if not hasattr(self,"loop_run_button"):
            return
        if self._automatic_operation_active:
            self.loop_run_button.setEnabled(False)
            self.loop_stop_button.setEnabled(
                self._automatic_operation_owner
                and not self._automatic_operation_stopping
            )
            text = self._automatic_progress_text or (
                "Stopping..." if self._automatic_operation_stopping
                else "Automatic feedback running..."
            )
            self.loop_run_button.setToolTip(text)
            self.loop_stop_button.setToolTip(text)
            return

        self.loop_run_button.setEnabled(self._automatic_feedback_available)
        self.loop_stop_button.setEnabled(False)
        reason = (
            "Run automatic intensity feedback."
            if self._automatic_feedback_available
            else self._automatic_feedback_reason
        )
        self.loop_run_button.setToolTip(reason)
        self.loop_stop_button.setToolTip(reason)

    # ------------------------------------------------------------------
    # Global round selection / presentation
    # ------------------------------------------------------------------

    def _position_selector_available(self) -> bool:
        return bool(
            FeedbackCapability.POSITION_CORRECTION in set(self._status.capabilities)
            and self._session_inspection.position_reference_round is not None
        )

    def _populate_position_selector(self) -> None:
        available = self._position_selector_available()
        self.position_selector_label.setVisible(available)
        self.position_selector.setVisible(available)
        if not available:
            self._selected_position_context = _POSITION_CORRECTED
            blocker = QtCore.QSignalBlocker(self.position_selector)
            try:
                index = self.position_selector.findData(_POSITION_CORRECTED)
                self.position_selector.setCurrentIndex(index)
            finally:
                del blocker
            return

        requested = self._selected_position_context
        if requested not in (_POSITION_NOT_CORRECTED,_POSITION_CORRECTED):
            requested = _POSITION_CORRECTED
        blocker = QtCore.QSignalBlocker(self.position_selector)
        try:
            index = self.position_selector.findData(requested)
            if index < 0:
                index = self.position_selector.findData(_POSITION_CORRECTED)
            self.position_selector.setCurrentIndex(index)
            value = self.position_selector.currentData()
            self._selected_position_context = (
                _POSITION_CORRECTED if value is None else str(value)
            )
        finally:
            del blocker

    def _viewing_position_reference(self) -> bool:
        return bool(
            self._position_selector_available()
            and self._selected_position_context == _POSITION_NOT_CORRECTED
        )

    def _on_position_selection_changed(self,_index: int) -> None:
        value = self.position_selector.currentData()
        self._selected_position_context = (
            _POSITION_CORRECTED if value is None else str(value)
        )
        self._selected_round_key = None
        self._candidate_current = False
        self._candidate_metrics = None
        self._populate_round_selector(
            follow_latest=self._selected_position_context == _POSITION_CORRECTED,
        )
        self._refresh_header()
        self._sync_selected_round(force=True)
        self._refresh_status_controls()
        self._refresh_feedback_visualizations()

    def _populate_round_selector(self,*,follow_latest: bool=False) -> None:
        if self._viewing_position_reference():
            blocker = QtCore.QSignalBlocker(self.round_selector)
            try:
                self.round_selector.clear()
                self.round_selector.addItem("—",None)
                self.round_selector.setCurrentIndex(0)
                self._selected_round_key = None
            finally:
                del blocker
            self.round_selector.setEnabled(False)
            self.round_selector.setToolTip(
                "Intensity rounds are not applicable to the uncorrected "
                "position reference."
            )
            return

        previous = self._selected_round_key
        rounds = self._session_inspection.rounds
        entries = [
            ("Round %d" % item.index,"round:%d" % item.index)
            for item in rounds
        ]

        # A pending feedback adaptation is a candidate for the next round, not
        # an official round yet. Keep the user on the latest completed source
        # round until that candidate is successfully computed. If no completed
        # round exists at all, retain the working round as a useful fallback
        # for initial/reset computation states.
        working = self._session_inspection.working_round
        if not rounds and working is not None:
            state = str(getattr(working,"state","") or "")
            state_text = {
                CGHWorkingRoundState.NOT_COMPUTED.value:"Not computed",
                CGHWorkingRoundState.COMPUTING.value:"Computing...",
                CGHWorkingRoundState.FAILED.value:"Failed",
            }.get(state,make_display_name(state) if state else "Not computed")
            entries.append((
                "Round %d · %s" % (working.index,state_text),
                "working:%d" % working.index,
            ))

        current_key = self._current_round_key()
        available_keys = [key for _label,key in entries]
        if follow_latest:
            requested = current_key
        else:
            requested = previous if previous in available_keys else current_key
        if requested not in available_keys:
            requested = available_keys[-1] if available_keys else None

        blocker = QtCore.QSignalBlocker(self.round_selector)
        try:
            self.round_selector.clear()
            for label,key in entries:
                self.round_selector.addItem(label,key)
            index = self.round_selector.findData(requested)
            self.round_selector.setCurrentIndex(index if index >= 0 else -1)
            value = self.round_selector.currentData()
            self._selected_round_key = None if value is None else str(value)
        finally:
            del blocker
        self.round_selector.setEnabled(bool(entries))

    def _current_round_key(self) -> str | None:
        rounds = self._session_inspection.rounds
        if rounds:
            return "round:%d" % rounds[-1].index
        working = self._session_inspection.working_round
        if working is not None:
            return "working:%d" % working.index
        return None

    def _selected_round(self):
        if self._viewing_position_reference():
            return self._session_inspection.position_reference_round
        key = self._selected_round_key
        if key is None:
            return None
        prefix,_,value = str(key).partition(":")
        try:
            index = int(value)
        except Exception:
            return None
        if prefix == "working":
            working = self._session_inspection.working_round
            return working if working is not None and working.index == index else None
        if prefix == "round":
            for item in self._session_inspection.rounds:
                if item.index == index:
                    return item
        return None

    def _selected_is_current_context(self) -> bool:
        return (
            not self._viewing_position_reference()
            and self._selected_round_key is not None
            and self._selected_round_key == self._current_round_key()
        )

    def _selected_feedback_source_round(self):
        selected = self._selected_round()
        if selected is None or selected.result is None:
            return None
        return selected

    def _selected_is_interactive(self) -> bool:
        if not self._selected_is_current_context() or self._cgh_computing:
            return False
        selected = self._selected_round()
        if selected is None or selected.result is None:
            return False
        if self._status.adaptation_pending:
            return True
        return self._cgh_status.result_state is CGHResultState.CURRENT

    def _on_round_selection_changed(self,_index: int) -> None:
        value = self.round_selector.currentData()
        self._selected_round_key = None if value is None else str(value)
        self._candidate_current = False
        self._candidate_metrics = None
        self._refresh_header()
        self._sync_selected_round(force=True)
        self._refresh_status_controls()
        self._refresh_feedback_visualizations()

    def _sync_selected_round(self,force: bool=False) -> None:
        selected = self._selected_round()
        self.inspect_view.set_round(selected)
        self._propagation_round_index = None
        self._refresh_selected_propagation()

        source = self._selected_feedback_source_round()
        evaluation = None if source is None else source.evaluation
        measurement = None if evaluation is None else evaluation.measurement
        if (
            measurement is None
            and self._selected_is_current_context()
            and source is not None
            and source.result is not None
        ):
            measurement = self._session_inspection.measurement

        acquisition = None if measurement is None else measurement.acquisition
        current = self.measurement_view.measurement
        current_id = None if current is None else current.measurement_id
        new_id = None if acquisition is None else acquisition.measurement_id
        changed = bool(force or current_id != new_id)

        if acquisition is None:
            if current is not None:
                self.measurement_view.clear_measurement()
            self._candidate_metrics = None
            self._refresh_metrics_display(None)
        else:
            if changed:
                self.measurement_view.set_measurement(acquisition)
                self.measurement_view.set_parameters(
                    self._status.localization_params,
                    invalidate_candidate=False,
                )
                self._candidate_metrics = None
            committed = measurement.localization
            if committed is not None:
                candidate = self.measurement_view.candidate
                if candidate is None or candidate is committed:
                    self.measurement_view.set_committed_result(
                        committed,committed.parameters,
                    )
            elif self.measurement_view.candidate is None:
                self.measurement_view.clear_localization_result()
            self._refresh_metrics_display(
                None if evaluation is None else evaluation.intensity_analysis
            )

        self.measurement_view.set_read_only(not self._selected_is_interactive())

    def _refresh_header(self) -> None:
        self.target_summary_label.setText(self._target_summary_text())
        self.target_summary_label.setToolTip(self.target_summary_label.text())

        selected = self._selected_round()
        reason = None if selected is None else getattr(
            selected,"unavailable_reason",None,
        )
        if self._viewing_position_reference():
            self.round_selector.setToolTip(
                "Intensity rounds are not applicable to the uncorrected "
                "position reference."
            )
        else:
            self.round_selector.setToolTip("" if reason is None else str(reason))

        latest_key = self._current_round_key()
        resettable = bool(
            not self._viewing_position_reference()
            and selected is not None
            and selected.result is not None
            and self._selected_round_key != latest_key
        )
        self.reset_round_button.setVisible(resettable)
        self.reset_round_button.setEnabled(resettable and not self._cgh_computing)

        capabilities = set(self._status.capabilities)
        parts = []
        if FeedbackCapability.INTENSITY in capabilities:
            parts.append("Intensity ×%d" % self._status.intensity_count)
        if FeedbackCapability.POSITION_CORRECTION in capabilities:
            parts.append(
                "Position %s" % ("ON" if self._status.position_active else "OFF")
            )
        self.feedback_summary_label.setText(" · ".join(parts) if parts else "None")

    def _target_summary_text(self) -> str:
        presentation = self._cgh_summary.get("target_presentation")
        specs = self._cgh_summary.get("target_param_specs") or {}
        params = self._cgh_summary.get("target_params") or {}
        if presentation is None:
            return "Target unavailable"
        return format_target_summary(
            presentation,
            params,
            specs,
            unit_mode=str(self._cgh_summary.get("unit_mode") or "slm"),
            conversion_context=self._cgh_summary.get("conversion_context"),
        )

    def _reset_selected_round(self,*_args: Any) -> None:
        selected = self._selected_round()
        if selected is None or selected.result is None:
            return
        self._emit(
            MeasurementsAction.RESET_TO_ROUND,
            {"round_index":int(selected.index)},
        )

    def _selected_propagation_key(self,pad_size: int | None=None):
        selected = self._selected_round()
        if selected is None or selected.result is None:
            return None
        size = (
            int(self.inspect_view.propagation_view.pad_size.value())
            if pad_size is None else int(pad_size)
        )
        position_context = (
            _POSITION_NOT_CORRECTED
            if self._viewing_position_reference()
            else _POSITION_CORRECTED
        )
        return (
            position_context,
            int(selected.index),
            int(selected.result.generation),
            size,
        )

    def _refresh_selected_propagation(self) -> None:
        view = self.inspect_view.propagation_view
        key = self._selected_propagation_key()
        if key is None:
            selected = self._selected_round()
            reason = (
                "CGH has not been computed for this round."
                if selected is None
                else str(
                    getattr(selected,"unavailable_reason",None)
                    or "CGH has not been computed for this round."
                )
            )
            view.set_round_available(False,reason=reason)
            return
        view.set_round_available(True)
        image = self._propagation_cache.get(key)
        if image is None:
            view.set_not_simulated()
        else:
            view.set_image(image)

    def _request_selected_propagation(self,pad_size: int) -> None:
        key = self._selected_propagation_key(pad_size)
        if key is None:
            return
        cached = self._propagation_cache.get(key)
        if cached is not None:
            self.inspect_view.propagation_view.set_image(cached)
            return
        self._pending_propagation_key = key
        self.inspect_view.propagation_view.set_busy(True)
        self._emit(
            MeasurementsAction.PROPAGATE_SELECTED,
            {
                "position_context":key[0],
                "round_index":key[1],
                "pad_size":key[3],
            },
        )

    def _viewing_current_measurement(self) -> bool:
        """Compatibility helper retained for the existing feedback control code."""
        return self._selected_is_interactive()

    # ------------------------------------------------------------------
    # Status and metrics
    # ------------------------------------------------------------------

    def _refresh_metrics_display(
        self,metrics: Any | None=None,
    ) -> None:
        if self._selected_is_interactive():
            if metrics is None and self._candidate_metrics is not None:
                metrics = self._candidate_metrics
            if metrics is None:
                source = self._selected_feedback_source_round()
                evaluation = None if source is None else source.evaluation
                metrics = (
                    None if evaluation is None
                    else evaluation.intensity_analysis
                )
        values = {} if metrics is None else dict(metrics.values)
        uniformity = values.get("uniformity")
        efficiency = values.get("efficiency")
        self.measurement_uniformity_label.setText(
            "—" if uniformity is None else "%.4f" % float(uniformity)
        )
        self.measurement_efficiency_label.setText(
            "—" if efficiency is None else "%.4f" % float(efficiency)
        )
        preview = None if metrics is None else getattr(metrics,"integration_preview",None)
        if preview is None:
            self.measurement_view.clear_auxiliary_image("Integration")
        else:
            self.measurement_view.set_auxiliary_image("Integration",preview)

    def _refresh_feedback_visualizations(self) -> None:
        selected = self._selected_round()
        pending = bool(
            self._status.adaptation_pending
            and self._selected_is_current_context()
            and self._session_inspection.working_round is not None
        )
        target_record = (
            self._session_inspection.working_round if pending else selected
        )
        target_display = (
            None if target_record is None
            else getattr(target_record,"target_display",None)
        )
        if target_display is None:
            self.feedback_target_view.clear()
        else:
            self.feedback_target_view.set_target(
                positions_kxy=target_display.positions_kxy,
                intensities=target_display.intensities,
                pending=pending,
            )
        self.metrics_history_view.set_rounds(
            () if self._viewing_position_reference()
            else self._session_inspection.rounds
        )

        correction = self._session_inspection.position_correction
        if correction is None:
            self.position_correction_view.clear()
        else:
            self.position_correction_view.set_correction(correction)

    def _refresh_status_controls(self) -> None:
        status = self._status
        capabilities = set(status.capabilities)
        intensity_available = FeedbackCapability.INTENSITY in capabilities
        position_available = FeedbackCapability.POSITION_CORRECTION in capabilities
        localized = bool(status.localization_available)

        viewing_current = self._viewing_current_measurement()
        current_context = self._selected_is_current_context()

        self.feedback_tabs.setTabEnabled(0,True)
        self.feedback_tabs.setTabEnabled(1,position_available)
        if self.feedback_tabs.currentIndex() == 1 and not position_available:
            self.feedback_tabs.setCurrentIndex(0)
        # self.feedback_tabs.setTabEnabled(0,intensity_available)
        # self.feedback_tabs.setTabEnabled(1,position_available)
        # if not self.feedback_tabs.tabBar().isTabEnabled(self.feedback_tabs.currentIndex()):
        #     self.feedback_tabs.setCurrentIndex(0 if intensity_available else 1)

        self.intensity_apply_button.setEnabled(
            intensity_available
            and localized
            and viewing_current
            and not self._cgh_computing
        )
        position_pending = (
            status.feedback_compute_pending
            and status.pending_feedback_change is FeedbackChangeKind.POSITION
        )
        if position_pending:
            if status.position_active:
                position_text = "Correction active · hologram pending"
            elif status.position_available:
                position_text = "Correction disabled · hologram pending"
            else:
                position_text = "Correction cleared · hologram pending"
            position_color = _WARNING_COLOR
        elif status.position_active:
            position_text,position_color = "Correction applied",_OK_COLOR
        elif status.position_available:
            position_text,position_color = (
                "Correction available · disabled",_WARNING_COLOR
            )
        else:
            position_text,position_color = "No correction",_MUTED_COLOR
        self.position_status_label.setText(position_text)
        self.position_status_label.setStyleSheet("color: %s;" % position_color)
        self.position_apply_button.setEnabled(
            position_available
            and localized
            and viewing_current
            and not self._cgh_computing
        )
        self.position_toggle_button.setEnabled(
            position_available
            and status.position_available
            and current_context
            and not self._cgh_computing
        )
        self.position_toggle_button.setText(
            "Disable" if status.position_active else "Enable"
        )
        self.position_clear_button.setEnabled(
            position_available
            and status.position_available
            and current_context
            and not self._cgh_computing
        )

        reuse_available = bool(
            intensity_available
            and status.intensity_count > 0
            and viewing_current
            and status.previous_localization_available
            and not self._cgh_computing
        )
        self.reuse_localization_checkbox.setEnabled(reuse_available)
        self.reuse_localization_checkbox.setToolTip(
            "Reuse the previous accepted localization automatically after "
            "Acquire/Load."
            if reuse_available else
            "Available from Round 1 when a previous accepted localization exists."
        )

        analysis_locked = bool(
            status.intensity_count > 0
            or not self._selected_is_current_context()
            or self._cgh_computing
        )
        for field in self.intensity_form.fields.values():
            field.editor.setEnabled(not analysis_locked)
            field.editor.setToolTip(
                "Locked after Round 1 is computed. Reset intensity feedback "
                "to Round 0 to change this analysis setting."
                if status.intensity_count > 0 else ""
            )

        self._refresh_measurement_status()
        self._refresh_localization_commit_controls()
        self._refresh_adaptation_status()
        self._refresh_cgh_summary()

    def _refresh_measurement_status(self) -> None:
        selected = self._selected_round()
        text = ""
        if selected is None:
            text = "Compute CGH first"
        elif self._viewing_position_reference():
            text = "Position reference · read only"
        elif not self._selected_is_current_context():
            text = "Historical round · read only"
        elif selected.result is None:
            if str(getattr(selected,"state","") or "") == CGHWorkingRoundState.COMPUTING.value:
                text = "Computing CGH..."
            elif str(getattr(selected,"state","") or "") == CGHWorkingRoundState.FAILED.value:
                text = "CGH computation failed"
            else:
                text = "Compute CGH first"
        elif self._status.adaptation_pending:
            text = "Adaptation pending · source round remains editable"
        elif self._cgh_status.result_state is not CGHResultState.CURRENT:
            text = "Compute CGH first"

        if text:
            self.measurement_view.set_measurement_status(text,warning=True)
        else:
            self.measurement_view.refresh_measurement_status()

    def _refresh_adaptation_status(self) -> None:
        status = self._status
        pending = bool(status.feedback_compute_pending)
        has_effective = bool(status.intensity_count or status.position_active)
        if pending:
            text = "Feedback change pending computation."
            color = _WARNING_COLOR
        elif has_effective and self._cgh_status.result_state is CGHResultState.CURRENT:
            text = "Latest feedback adaptation is represented in the current CGH."
            color = _OK_COLOR
        else:
            text = "No feedback adaptation is waiting to be computed."
            color = _MUTED_COLOR
        for label in self._adaptation_status_labels:
            label.setText(text)
            label.setStyleSheet("color: %s;" % color)
        enabled = (
            pending
            and self._selected_is_current_context()
            and not self._cgh_computing
        )
        for button in self._compute_adapted_buttons:
            button.setEnabled(enabled)

    def _refresh_cgh_summary(self) -> None:
        if not hasattr(self,"intensity_cgh_algorithm_label"):
            return
        selected = self._selected_round()
        result = None if selected is None else selected.result
        if result is not None:
            algorithm = str(result.spec.algorithm or "")
            parameters = dict(result.spec.compute_params or {})
        else:
            algorithm = str(self._cgh_summary.get("algorithm") or "")
            parameters = dict(self._cgh_summary.get("parameters") or {})
        algorithm_text = "—" if not algorithm else make_display_name(algorithm)

        self.intensity_cgh_algorithm_label.setText(
            "Algorithm: %s" % algorithm_text
        )

        items = [
            "%s: %s" % (
                make_display_name(str(key)),
                self._format_parameter_value(value),
            )
            for key,value in parameters.items()
        ]
        if items:
            split = (len(items) + 1) // 2
            lines = [" · ".join(items[:split])]
            if split < len(items):
                lines.append(" · ".join(items[split:]))
            parameter_text = "\n".join(lines)
        else:
            parameter_text = "—"
        self.intensity_cgh_params_label.setText(parameter_text)

        details = "CGH computation parameters are read-only here and are " \
            "defined in the CGH computation view."
        if items:
            details += "\n\n%s: %s" % (
                algorithm_text," · ".join(items),
            )
        self.intensity_cgh_algorithm_label.setToolTip(details)
        self.intensity_cgh_params_label.setToolTip(details)

    @staticmethod
    def _format_parameter_value(value: Any) -> str:
        if isinstance(value,bool):
            return "On" if value else "Off"
        return str(value)

    def _refresh_localization_commit_controls(self) -> None:
        cgh_state = self._cgh_status.result_state
        viewing_current = self._viewing_current_measurement()
        source_editable = self._selected_is_interactive()
        self.measurement_view.set_accept_enabled(
            source_editable
            and self._candidate_current
            and not self._cgh_computing
        )
        if not viewing_current:
            selected = self._selected_round()
            if (
                self._selected_is_current_context()
                and selected is not None
                and selected.result is None
            ):
                tip = "Compute the CGH before measuring or localizing this round."
            else:
                tip = "Historical measurements are inspection-only."
        elif self._status.adaptation_pending and source_editable:
            if not self._candidate_current:
                tip = "Run localization to update the source round before adapting again."
            else:
                tip = "Accept this localization; the pending adaptation will be discarded."
        elif cgh_state is CGHResultState.MISSING:
            tip = (
                "No CGH has been computed yet. Compute the CGH before "
                "accepting localization."
            )
        elif cgh_state is CGHResultState.STALE:
            tip = (
                "The computed CGH is stale. Compute it before accepting "
                "localization."
            )
        elif not self._candidate_current:
            tip = "Run localization to create a current candidate."
        else:
            tip = "Accept this localization for feedback operations."
        self.measurement_view.set_accept_tooltip(tip)

    # ------------------------------------------------------------------
    # UI actions
    # ------------------------------------------------------------------

    def _on_candidate_state_changed(self,current: bool) -> None:
        self._candidate_current = bool(current)
        if not self._candidate_current and self.measurement_view.candidate is None:
            self._candidate_metrics = None
        self._refresh_localization_commit_controls()

    def _accept_localization_candidate(self,*_args: Any) -> None:
        if (
            not self._selected_is_interactive()
            or not self.measurement_view.candidate_is_current
        ):
            return
        self._emit(
            MeasurementsAction.LOCALIZATION_ACCEPT,
            {
                "localization":self.measurement_view.candidate,
                "parameters":dict(
                    self.measurement_view.candidate_parameters or {}
                ),
            },
        )

    def _on_intensity_parameter_changed(self,key: str,value: Any) -> None:
        options = {
            "group":"intensity_analysis",
            "changes":{str(key):value},
        }
        if self.measurement_view.candidate_is_current:
            options["localization"] = self.measurement_view.candidate
            options["localization_parameters"] = dict(
                self.measurement_view.candidate_parameters or {}
            )
        self._emit(MeasurementsAction.FEEDBACK_PARAMETERS,options)

    def _reuse_previous_localization(self) -> bool:
        checkbox = getattr(self,"reuse_localization_checkbox",None)
        return bool(
            checkbox is not None
            and checkbox.isEnabled()
            and checkbox.isChecked()
        )

    def _toggle_position(self,*_args: Any) -> None:
        if not self._status.position_available:
            return
        self._emit(
            MeasurementsAction.POSITION_SET_ACTIVE,
            {"active":not self._status.position_active},
        )

    def _emit(
        self,action: MeasurementsAction,options: Mapping[str, Any] | None=None,
    ) -> None:
        self.sigActionRequested.emit(action.value,dict(options or {}))


MeasurementsCorrectionsWindow = CGHSessionWindow


__all__ = [
    "CGHSessionWindow",
    "MeasurementsAction",
    "MeasurementsCorrectionsWindow",
]
