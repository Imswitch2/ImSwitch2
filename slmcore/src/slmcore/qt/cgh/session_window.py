"""Reusable modeless workspace for measurement and CGH correction workflows."""

from __future__ import annotations

from dataclasses import replace
from enum import Enum
from typing import Any,Mapping,Sequence

import numpy as np
from qtpy import QtCore,QtWidgets

from ...core.cgh.execution.session_model import (
    CGHSessionInspection,CGHWorkingRoundState,
)
from ...core.cgh.execution.status import CGHResultState,CGHStatus
from ...core.cgh.feedback.model import (
    FeedbackCapability,FeedbackChangeKind,FeedbackInspection,FeedbackStatus,
)
from ...core.cgh.localization import spot_ordering_label
from ...core.cgh.feedback.parameters import INTENSITY_ANALYSIS_PARAMS
from ...core.cgh.measurement_metrics import IntensityAnalysis
from ...core.engine.parameters.spec import make_display_name
from ..widgets.fields import ParamForm
from ..measurement.localization_view import MeasurementLocalizationView
from .session_views import (
    FeedbackMetricsHistoryView,
    FeedbackTargetView,
    InspectView,
    PositionCorrectionView,
    PositionReferenceView,
    format_target_summary,
)


_MUTED_COLOR = "#888"
_OK_COLOR = "#286b2d"
_WARNING_COLOR = "#a66a00"
_POSITION_NOT_CORRECTED = "not_corrected"
_POSITION_CORRECTED = "corrected"
_DEFINE_OBSERVATION_PLANE = "__define_observation_plane__"


def _format_float(value: Any) -> str:
    try:
        return "%.3g" % float(value)
    except Exception:
        return "—"



class MeasurementsAction(str,Enum):
    """Host operations requested by :class:`MeasurementsCorrectionsWindow`."""

    ACQUIRE = "acquire"
    VISUAL_BACKGROUND_ACQUIRE = "visual_background_acquire"
    LOAD = "load"
    LOCALIZATION_RUN = "localization_run"
    LOCALIZATION_INFER_MISSING = "localization_infer_missing"
    LOCALIZATION_ACCEPT = "localization_accept"
    LOCALIZATION_REUSE = "localization_reuse"
    FEEDBACK_PARAMETERS = "feedback_parameters"
    INTENSITY_APPLY = "intensity_apply"
    INTENSITY_RESET = "intensity_reset"
    POSITION_APPLY = "position_apply"
    POSITION_REFERENCE_SET = "position_reference_set"
    POSITION_REFERENCE_FIT_CENTER = "position_reference_fit_center"
    POSITION_REFERENCE_ADD = "position_reference_add"
    POSITION_REFERENCE_PERSIST = "position_reference_persist"
    POSITION_REFERENCE_DELETE = "position_reference_delete"
    POSITION_SET_ACTIVE = "position_set_active"
    POSITION_CLEAR = "position_clear"
    COMPUTE_ADAPTED = "compute_adapted_hologram"
    RESET_TO_ROUND = "reset_to_round"
    PROPAGATE_SELECTED = "propagate_selected"
    INSPECT = "inspect"  # compatibility with the previous details dialog
    AUTOMATIC_START = "automatic_start"
    AUTOMATIC_STOP = "automatic_stop"
    OBSERVATION_PLANE_SET = "observation_plane_set"


class CGHSessionWindow(QtWidgets.QDialog):
    """Modeless CGH session workspace with measurement, feedback and inspection."""

    sigActionRequested = QtCore.Signal(str,object)
    sigDefineObservationPlaneRequested = QtCore.Signal()

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
        observation_planes: Sequence[str]=(),
        current_observation_plane: str | None=None,
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
        self._position_reference_context: dict[str,Any] = {
            "mode":"global_fit",
            "reference_name":None,
            "references":(),
            "persistence_available":False,
            "editable_geometry":None,
            "editable_locks":{},
            "correction_mode":"per_spot",
            "polynomial_degree":2,
            "affine_transform":None,
            "manual_transform":None,
            "fit_geometry":None,
            "observation_plane_name":None,
            "compatible":True,
            "compatibility_error":"",
            "change_allowed":True,
            "change_unavailable_reason":"",
            "create_allowed":False,
            "create_unavailable_reason":"",
        }
        self._position_reference_preview = None
        # Optional presentation-only detector image used behind the Position
        # detector overlay. It never replaces the measurement/localization.
        self._position_visual_background_measurement = None
        self._position_background_mode = "localization"
        self._normal_localization_context = dict(localization_context or {})
        self._cgh_computing = False
        self._candidate_current = False
        self._candidate_metrics: IntensityAnalysis | None = None
        self._displayed_integration_preview = None
        self._selected_position_context = _POSITION_CORRECTED
        self._selected_round_key: str | None = None
        self._propagation_round_index: int | None = None
        self._propagation_cache = {}
        self._pending_propagation_key = None
        self._compute_adapted_buttons = []
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
            observation_planes,current_observation_plane,
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
        observation_planes: Sequence[str],
        current_observation_plane: str | None,
    ) -> QtWidgets.QWidget:
        panel = QtWidgets.QGroupBox("Measurement")
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(8,10,8,8)
        layout.setSpacing(7)

        observation_row = QtWidgets.QHBoxLayout()
        observation_row.setSpacing(6)
        observation_row.addWidget(QtWidgets.QLabel("Observation:"))
        self.observation_plane_combo = QtWidgets.QComboBox(panel)
        self.observation_plane_combo.setMinimumWidth(150)
        observation_row.addWidget(self.observation_plane_combo,1)
        layout.addLayout(observation_row)
        self.configure_observation_planes(
            observation_planes,current_observation_plane,
        )
        self.observation_plane_combo.currentIndexChanged.connect(
            self._on_observation_plane_changed,
        )

        self.measurement_view = MeasurementLocalizationView(
            parameters=status.localization_params,
            context=localization_context,
            detectors=detectors,
            current_detector=current_detector,
            show_metrics=False,
            show_infer_missing=True,
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
        self.measurement_view.sigInferMissingRequested.connect(
            self._infer_missing_localization_candidate,
        )
        self.measurement_view.sigAcceptRequested.connect(
            self._accept_localization_candidate,
        )
        self.position_visual_snap_button = QtWidgets.QPushButton(
            "Snap visual image"
        )
        self.position_visual_snap_button.setMinimumHeight(23)
        self.position_visual_snap_button.setToolTip(
            "Acquire a detector image for optional use as the Position overlay "
            "background. This does not replace the localization measurement."
        )
        self.position_visual_snap_button.clicked.connect(
            self._position_visual_snap_requested,
        )
        self.measurement_view.add_action_widget(
            self.position_visual_snap_button
        )

        self.position_reference_add_button = QtWidgets.QPushButton(
            "Add Session Reference…"
        )
        self.position_reference_add_button.setMinimumHeight(23)
        self.position_reference_add_button.setMinimumWidth(160)
        self.position_reference_add_button.clicked.connect(
            self._add_position_reference_requested,
        )
        self.measurement_view.add_action_widget(
            self.position_reference_add_button
        )

        layout.addWidget(self.measurement_view,1)

        # Host-specific intensity analysis belongs beside the result image: the
        # integration parameter is then immediately adjacent to the Integration
        # visualization. Calibration/standalone localization simply do not add
        # this optional widget.
        analysis_widget = QtWidgets.QWidget()
        self.measurement_analysis_widget = analysis_widget
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

    def _make_geometry_warning(self) -> QtWidgets.QLabel:
        label=QtWidgets.QLabel()
        label.setWordWrap(True)
        label.setStyleSheet('QLabel { color: %s; }' % _WARNING_COLOR)
        label.setToolTip(
            "Geometry orientation has not been calibrated for this section and "
            "observation plane. Feedback workflows that rely on logical point "
            "correspondence may be mapped incorrectly. Calibrate it in "
            "Calibration → Geometry."
        )
        label.setVisible(False)
        return label

    def _build_intensity_tab(self,status: FeedbackStatus) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(5,7,5,5)
        layout.setSpacing(7)
        self.intensity_geometry_warning=self._make_geometry_warning()
        layout.addWidget(self.intensity_geometry_warning)

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
        outer_layout = QtWidgets.QVBoxLayout(tab)
        outer_layout.setContentsMargins(5,7,5,5)
        outer_layout.setSpacing(7)
        self.position_geometry_warning=self._make_geometry_warning()
        outer_layout.addWidget(self.position_geometry_warning)
        content=QtWidgets.QWidget(tab)
        layout=QtWidgets.QHBoxLayout(content)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(8)
        outer_layout.addWidget(content,1)

        # Main visualization: detector-space reference workbench by default,
        # with the original k-space diagnostic retained as an alternate view.
        viewer = QtWidgets.QGroupBox("Alignment preview")
        viewer_layout = QtWidgets.QVBoxLayout(viewer)
        viewer_layout.setContentsMargins(8,7,8,7)

        view_row = QtWidgets.QHBoxLayout()
        view_row.addWidget(QtWidgets.QLabel("View:"))
        self.position_view_combo = QtWidgets.QComboBox()
        self.position_view_combo.addItem("Detector overlay","detector")
        self.position_view_combo.addItem("Position correction","kxy")
        self.position_view_combo.currentIndexChanged.connect(
            self._on_position_view_changed,
        )
        view_row.addWidget(self.position_view_combo)
        self.position_background_label = QtWidgets.QLabel("Background:")
        view_row.addWidget(self.position_background_label)
        self.position_background_combo = QtWidgets.QComboBox()
        self.position_background_combo.addItem(
            "Localization image","localization"
        )
        self.position_background_combo.currentIndexChanged.connect(
            self._on_position_background_changed,
        )
        view_row.addWidget(self.position_background_combo)
        view_row.addStretch(1)
        viewer_layout.addLayout(view_row)

        self.position_view_stack = QtWidgets.QStackedWidget(viewer)
        self.position_reference_view = PositionReferenceView(viewer)
        self.position_reference_view.sigCenterOffsetRequested.connect(
            self._on_position_reference_center_dragged,
        )
        self.position_reference_view.sigManualTransformRequested.connect(
            self._on_position_reference_manual_transform_dragged,
        )
        self.position_correction_view = PositionCorrectionView(viewer)
        self.position_view_stack.addWidget(self.position_reference_view)
        self.position_view_stack.addWidget(self.position_correction_view)
        viewer_layout.addWidget(self.position_view_stack,1)
        layout.addWidget(viewer,1)

        # Match the intensity tab: workflow controls live in a narrow vertical
        # panel on the right while the visualization keeps the dominant area.
        controls = QtWidgets.QGroupBox("Position setup")
        controls.setMinimumWidth(220)
        controls.setMaximumWidth(285)
        controls_layout = QtWidgets.QVBoxLayout(controls)
        controls_layout.setContentsMargins(8,7,8,7)
        controls_layout.setSpacing(5)

        self.position_status_label = QtWidgets.QLabel("No correction")
        self.position_status_label.setWordWrap(True)

        reference_label = QtWidgets.QLabel("REFERENCE")
        reference_font = reference_label.font()
        reference_font.setBold(True)
        reference_label.setFont(reference_font)
        controls_layout.addWidget(reference_label)

        # First choose the *kind* of detector-space reference.  Controls that
        # belong to one kind live in their own conditional panel below.
        source_row = QtWidgets.QHBoxLayout()
        source_row.setContentsMargins(0,0,0,0)
        source_row.setSpacing(5)
        source_row.addWidget(QtWidgets.QLabel("Source"))
        self.position_reference_combo = QtWidgets.QComboBox()
        self.position_reference_combo.addItem("Global fit","global_fit")
        self.position_reference_combo.addItem("Editable lattice","editable")
        self.position_reference_combo.addItem("Saved reference","reference")
        self.position_reference_combo.addItem("Current (free edit)","current")
        self.position_reference_combo.currentIndexChanged.connect(
            self._on_position_reference_changed,
        )
        source_row.addWidget(self.position_reference_combo,1)
        controls_layout.addLayout(source_row)

        self.position_reference_editable_widget = QtWidgets.QWidget(controls)
        editable_layout = QtWidgets.QVBoxLayout(
            self.position_reference_editable_widget
        )
        editable_layout.setContentsMargins(0,0,0,0)
        editable_layout.setSpacing(4)
        geometry_form = QtWidgets.QFormLayout()
        geometry_form.setContentsMargins(0,0,0,0)
        geometry_form.setHorizontalSpacing(6)
        geometry_form.setVerticalSpacing(3)

        self.position_reference_period_x = self._make_position_geometry_spinbox(
            0.01,10000.0,3," px",0.1,
        )
        self.position_reference_period_y = self._make_position_geometry_spinbox(
            0.01,10000.0,3," px",0.1,
        )
        self.position_reference_rotation = self._make_position_geometry_spinbox(
            -180.0,180.0,3,"°",0.1,
        )
        self.position_reference_lattice_angle = self._make_position_geometry_spinbox(
            0.1,179.9,3,"°",0.1,
        )
        self.position_reference_offset_x = self._make_position_geometry_spinbox(
            -10000.0,10000.0,2," px",0.1,
        )
        self.position_reference_offset_y = self._make_position_geometry_spinbox(
            -10000.0,10000.0,2," px",0.1,
        )
        lock_rows = (
            ("Period X",self.position_reference_period_x,"position_reference_period_x_lock"),
            ("Period Y",self.position_reference_period_y,"position_reference_period_y_lock"),
            ("Rotation",self.position_reference_rotation,"position_reference_rotation_lock"),
            ("Lattice angle",self.position_reference_lattice_angle,"position_reference_lattice_angle_lock"),
        )
        for label,editor,lock_name in lock_rows:
            lock = QtWidgets.QCheckBox("Lock")
            lock.setToolTip(
                "Keep this value fixed when using Fit center."
            )
            setattr(self,lock_name,lock)
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(0,0,0,0)
            row_layout.setSpacing(5)
            row_layout.addWidget(editor,1)
            row_layout.addWidget(lock)
            geometry_form.addRow(label,row)
            editor.valueChanged.connect(self._on_editable_geometry_changed)
            lock.toggled.connect(self._on_editable_locks_changed)
        for label,editor in (
            ("Offset X",self.position_reference_offset_x),
            ("Offset Y",self.position_reference_offset_y),
        ):
            geometry_form.addRow(label,editor)
            editor.valueChanged.connect(self._on_editable_geometry_changed)
        editable_layout.addLayout(geometry_form)

        self.position_reference_square_straight_button = QtWidgets.QPushButton(
            "Square && straight"
        )
        self.position_reference_square_straight_button.setToolTip(
            "Set rotation to 0°, lattice angle to 90°, and lock both values."
        )
        self.position_reference_square_straight_button.clicked.connect(
            self._square_and_straight_editable_geometry,
        )
        editable_layout.addWidget(self.position_reference_square_straight_button)

        self.position_reference_fit_center_button = QtWidgets.QPushButton(
            "Fit center"
        )
        self.position_reference_fit_center_button.setToolTip(
            "Fit the editable lattice to the central localized spots while "
            "keeping locked geometry values fixed."
        )
        self.position_reference_fit_center_button.clicked.connect(
            self._fit_editable_geometry_center,
        )
        self.position_reference_fit_center_button.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Fixed,
        )
        editable_layout.addWidget(self.position_reference_fit_center_button)

        self.position_reference_reset_fit_button = QtWidgets.QPushButton(
            "Reset from fit"
        )
        self.position_reference_reset_fit_button.clicked.connect(
            self._reset_editable_geometry_from_fit,
        )
        self.position_reference_reset_fit_button.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Fixed,
        )
        editable_layout.addWidget(self.position_reference_reset_fit_button)
        editable_hint = QtWidgets.QLabel(
            "Drag the reference center in the detector view to translate it."
        )
        editable_hint.setWordWrap(True)
        editable_hint.setStyleSheet("color: %s;" % _MUTED_COLOR)
        editable_layout.addWidget(editable_hint)
        controls_layout.addWidget(self.position_reference_editable_widget)

        # Saved references are intentionally a separate panel: choosing the
        # source only reveals this selector. A concrete reference is activated
        # only after the user explicitly chooses one.
        self.position_reference_saved_widget = QtWidgets.QWidget(controls)
        saved_layout = QtWidgets.QVBoxLayout(self.position_reference_saved_widget)
        saved_layout.setContentsMargins(0,0,0,0)
        saved_layout.setSpacing(4)

        saved_label = QtWidgets.QLabel("Reference")
        saved_layout.addWidget(saved_label)
        saved_row = QtWidgets.QHBoxLayout()
        saved_row.setContentsMargins(0,0,0,0)
        saved_row.setSpacing(4)
        self.position_reference_saved_combo = QtWidgets.QComboBox()
        self.position_reference_saved_combo.setPlaceholderText("Select reference…")
        self.position_reference_saved_combo.currentIndexChanged.connect(
            self._on_saved_position_reference_changed
        )
        saved_row.addWidget(self.position_reference_saved_combo,1)
        self.position_reference_manage_button = QtWidgets.QToolButton()
        self.position_reference_manage_button.setText("⋯")
        self.position_reference_manage_button.setPopupMode(
            QtWidgets.QToolButton.InstantPopup
        )
        manage_menu = QtWidgets.QMenu(self.position_reference_manage_button)
        self.position_reference_persist_action = manage_menu.addAction(
            "Save permanently"
        )
        self.position_reference_persist_action.triggered.connect(
            self._persist_position_reference_requested
        )
        self.position_reference_delete_action = manage_menu.addAction("Delete")
        self.position_reference_delete_action.triggered.connect(
            self._delete_position_reference_requested
        )
        self.position_reference_manage_button.setMenu(manage_menu)
        saved_row.addWidget(self.position_reference_manage_button)
        saved_layout.addLayout(saved_row)

        correspondence_label = QtWidgets.QLabel("Correspondence")
        saved_layout.addWidget(correspondence_label)
        self.position_reference_correspondence_combo = QtWidgets.QComboBox()
        self.position_reference_correspondence_combo.setToolTip(
            "Choose how current localized spot identities are paired with the "
            "saved reference after the section feedback orientation is applied."
        )
        self.position_reference_correspondence_combo.currentIndexChanged.connect(
            self._on_position_reference_correspondence_changed
        )
        saved_layout.addWidget(self.position_reference_correspondence_combo)
        self.position_reference_correspondence_info = QtWidgets.QLabel()
        self.position_reference_correspondence_info.setWordWrap(True)
        self.position_reference_correspondence_info.setStyleSheet(
            "color: %s;" % _MUTED_COLOR
        )
        saved_layout.addWidget(self.position_reference_correspondence_info)
        controls_layout.addWidget(self.position_reference_saved_widget)

        self.position_reference_info_label = QtWidgets.QLabel()
        self.position_reference_info_label.setWordWrap(True)
        self.position_reference_info_label.setStyleSheet(
            "color: %s;" % _MUTED_COLOR
        )
        controls_layout.addWidget(self.position_reference_info_label)

        reference_separator = QtWidgets.QFrame(controls)
        reference_separator.setFrameShape(QtWidgets.QFrame.HLine)
        reference_separator.setFrameShadow(QtWidgets.QFrame.Sunken)
        controls_layout.addWidget(reference_separator)

        correction_model_label = QtWidgets.QLabel("CORRECTION")
        correction_model_font = correction_model_label.font()
        correction_model_font.setBold(True)
        correction_model_label.setFont(correction_model_font)
        controls_layout.addWidget(correction_model_label)

        correction_model_row = QtWidgets.QHBoxLayout()
        correction_model_row.setContentsMargins(0,0,0,0)
        correction_model_row.setSpacing(5)
        correction_model_row.addWidget(QtWidgets.QLabel("Model"))
        self.position_reference_correction_model_combo = QtWidgets.QComboBox()
        self.position_reference_correction_model_combo.addItem("Translation","translation")
        self.position_reference_correction_model_combo.addItem("Rigid","rigid")
        self.position_reference_correction_model_combo.addItem("Affine","affine")
        self.position_reference_correction_model_combo.addItem("Polynomial","polynomial")
        self.position_reference_correction_model_combo.addItem("Per-spot","per_spot")
        self.position_reference_correction_model_combo.setToolTip(
            "Translation fits one common shift. Rigid fits translation + rotation. "
            "Affine also allows scale and shear. Polynomial fits a smooth nonlinear "
            "field of order 2, 3, or 4. Per-spot applies each measured reference "
            "error independently."
        )
        self.position_reference_correction_model_combo.currentIndexChanged.connect(
            self._on_position_reference_correction_mode_changed
        )
        correction_model_row.addWidget(self.position_reference_correction_model_combo,1)
        controls_layout.addLayout(correction_model_row)

        polynomial_row = QtWidgets.QHBoxLayout()
        polynomial_row.setContentsMargins(0,0,0,0)
        polynomial_row.setSpacing(5)
        self.position_reference_polynomial_degree_label = QtWidgets.QLabel(
            "Polynomial order"
        )
        polynomial_row.addWidget(self.position_reference_polynomial_degree_label)
        self.position_reference_polynomial_degree_combo = QtWidgets.QComboBox()
        for degree in (2,3,4):
            self.position_reference_polynomial_degree_combo.addItem(
                str(degree),degree
            )
        self.position_reference_polynomial_degree_combo.setToolTip(
            "Total polynomial degree of the continuous detector-space correction "
            "field. Higher orders can model more complex warping but require "
            "more matched spots and can follow localization noise more closely."
        )
        self.position_reference_polynomial_degree_combo.currentIndexChanged.connect(
            self._on_position_reference_polynomial_degree_changed
        )
        polynomial_row.addWidget(self.position_reference_polynomial_degree_combo,1)
        controls_layout.addLayout(polynomial_row)

        edit_row = QtWidgets.QHBoxLayout()
        edit_row.setContentsMargins(0,0,0,0)
        edit_row.setSpacing(5)
        self.position_reference_interactive_edit_checkbox = QtWidgets.QCheckBox(
            "Edit"
        )
        self.position_reference_interactive_edit_checkbox.setToolTip(
            "Edit the fitted correction directly in the detector view."
        )
        self.position_reference_interactive_edit_checkbox.toggled.connect(
            self._on_position_reference_interactive_edit_toggled
        )
        edit_row.addWidget(self.position_reference_interactive_edit_checkbox)

        self.position_reference_reset_button = QtWidgets.QPushButton("Reset")
        self.position_reference_reset_button.setToolTip(
            "Discard interactive edits and return to the automatic Current → Reference fit."
        )
        self.position_reference_reset_button.clicked.connect(
            self._reset_position_reference_manual_edit,
        )
        edit_row.addWidget(self.position_reference_reset_button)
        edit_row.addStretch(1)

        self.position_reference_edit_state_label = QtWidgets.QLabel("Automatic")
        self.position_reference_edit_state_label.setToolTip(
            "Automatic uses the fitted correction. Edited means an interactive "
            "manual transform is currently overriding that fit."
        )
        self.position_reference_edit_state_label.setStyleSheet(
            "color: %s;" % _MUTED_COLOR
        )
        edit_row.addWidget(self.position_reference_edit_state_label)
        controls_layout.addLayout(edit_row)

        self.position_reference_edit_hint = QtWidgets.QLabel(
            "Drag the editor handles in the detector view. Predicted updates live."
        )
        self.position_reference_edit_hint.setWordWrap(True)
        self.position_reference_edit_hint.setStyleSheet(
            "color: %s;" % _MUTED_COLOR
        )
        controls_layout.addWidget(self.position_reference_edit_hint)

        applied_separator = QtWidgets.QFrame(controls)
        applied_separator.setFrameShape(QtWidgets.QFrame.HLine)
        applied_separator.setFrameShadow(QtWidgets.QFrame.Sunken)
        controls_layout.addWidget(applied_separator)

        applied_label = QtWidgets.QLabel("APPLIED CORRECTION")
        applied_font = applied_label.font()
        applied_font.setBold(True)
        applied_label.setFont(applied_font)
        controls_layout.addWidget(applied_label)
        controls_layout.addWidget(self.position_status_label)

        self.position_use_checkbox = QtWidgets.QCheckBox(
            "Use position correction"
        )
        self.position_use_checkbox.toggled.connect(
            lambda checked:self._emit(
                MeasurementsAction.POSITION_SET_ACTIVE,{"active":bool(checked)},
            )
        )
        controls_layout.addWidget(self.position_use_checkbox)

        correction_actions = QtWidgets.QHBoxLayout()
        correction_actions.setContentsMargins(0,0,0,0)
        correction_actions.setSpacing(5)
        self.position_apply_button = QtWidgets.QPushButton("Apply correction")
        self.position_apply_button.setFixedHeight(24)
        self.position_apply_button.clicked.connect(
            lambda _checked=False:self._emit(
                MeasurementsAction.POSITION_APPLY
            )
        )
        correction_actions.addWidget(self.position_apply_button,1)

        compute_button = self._make_compute_adapted_button()
        compute_button.setText("Compute")
        compute_button.setFixedHeight(24)
        correction_actions.addWidget(compute_button,1)
        controls_layout.addLayout(correction_actions)

        self.position_clear_button = QtWidgets.QPushButton("Clear correction")
        self.position_clear_button.setFixedHeight(24)
        self.position_clear_button.clicked.connect(
            lambda _checked=False:self._emit(MeasurementsAction.POSITION_CLEAR)
        )
        controls_layout.addWidget(self.position_clear_button)

        controls_layout.addSpacing(5)
        separator = QtWidgets.QFrame(controls)
        separator.setFrameShape(QtWidgets.QFrame.HLine)
        separator.setFrameShadow(QtWidgets.QFrame.Sunken)
        controls_layout.addWidget(separator)
        controls_layout.addSpacing(2)

        # Persistent FOV fitting lives in the dedicated Calibration workspace.
        # Position Feedback only inspects the currently selected mapping correction
        # alongside transient measurement/reference corrections.

        controls_layout.addStretch(1)
        layout.addWidget(controls,0)
        self._refresh_position_reference_controls()
        return tab

    def _make_compute_adapted_button(self) -> QtWidgets.QPushButton:
        button = QtWidgets.QPushButton("Compute")
        button.setMinimumHeight(28)
        button.clicked.connect(
            lambda _checked=False:self._emit(MeasurementsAction.COMPUTE_ADAPTED)
        )
        self._compute_adapted_buttons.append(button)
        return button

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

    def set_geometry_calibration_status(
        self,*,observation_plane_name: str | None,calibrated: bool,
    ) -> None:
        plane=str(observation_plane_name or "").strip() or None
        missing=bool(plane and not calibrated)
        text=(
            "⚠ Geometry not calibrated for %s\n"
            "Calibrate this section and observation plane in Calibration → Geometry."
            % plane
            if missing else ""
        )
        for warning in (
            getattr(self,"intensity_geometry_warning",None),
            getattr(self,"position_geometry_warning",None),
        ):
            if warning is not None:
                warning.setText(text)
                warning.setVisible(missing)

    def set_position_reference_state(
        self,context: Mapping[str,Any] | None,preview: Any=None,*,
        lightweight: bool=False,
    ) -> None:
        values = dict(context or {})
        self._position_reference_context.update(values)
        self._position_reference_preview = preview
        self._refresh_position_reference_controls()
        self._refresh_position_background_controls()
        if lightweight:
            self._refresh_position_reference_visualization()
        else:
            self._refresh_feedback_visualizations()
        self._refresh_status_controls()

    def _refresh_position_reference_controls(self) -> None:
        combo=getattr(self,"position_reference_combo",None)
        if combo is None:
            return
        context=dict(self._position_reference_context or {})
        mode=str(context.get("mode") or "global_fit")
        reference_name=str(context.get("reference_name") or "").strip()
        references=tuple(dict(item) for item in (context.get("references") or ()))

        blocker=QtCore.QSignalBlocker(combo)
        try:
            saved_index=combo.findData("reference")
            if saved_index>=0:
                item=combo.model().item(saved_index)
                if item is not None:
                    item.setEnabled(bool(references))
            current_index=combo.findData("current")
            if current_index>=0:
                item=combo.model().item(current_index)
                if item is not None:
                    item.setEnabled(True)
                    item.setToolTip(
                        "Use the current localized pattern as the reference baseline, "
                        "then edit a continuous correction freely."
                    )
            index=combo.findData(mode)
            combo.setCurrentIndex(index if index>=0 else 0)
        finally:
            del blocker

        saved_combo=getattr(self,"position_reference_saved_combo",None)
        if saved_combo is not None:
            previous_name=self._selected_managed_position_reference_name()
            preferred_name=reference_name or previous_name
            saved_blocker=QtCore.QSignalBlocker(saved_combo)
            try:
                saved_combo.clear()
                # Keep an explicit unselected entry. In an already-active saved
                # reference context we restore the active name; otherwise the
                # user must choose one deliberately.
                saved_combo.addItem("Select reference…",None)
                saved_combo.setItemData(0,"",QtCore.Qt.UserRole+1)
                for entry in references:
                    name=str(entry.get("name") or "").strip()
                    if not name:
                        continue
                    lifetime=str(entry.get("lifetime") or "session")
                    suffix="Persistent" if lifetime=="persistent" else "Session"
                    saved_combo.addItem("%s · %s" % (name,suffix),name)
                    saved_combo.setItemData(
                        saved_combo.count()-1,lifetime,QtCore.Qt.UserRole+1,
                    )
                selected_index=saved_combo.findData(preferred_name)
                if selected_index<0:
                    selected_index=0
                saved_combo.setCurrentIndex(selected_index)
            finally:
                del saved_blocker

        editable_widget=getattr(self,"position_reference_editable_widget",None)
        saved_widget=getattr(self,"position_reference_saved_widget",None)
        geometry=context.get("editable_geometry")
        if mode=="editable" and geometry is None:
            geometry=context.get("fit_geometry")
        if geometry:
            self._set_editable_geometry_controls(geometry)
        self._set_editable_lock_controls(context.get("editable_locks") or {})
        if editable_widget is not None:
            editable_widget.setVisible(mode=="editable")
        if saved_widget is not None:
            saved_widget.setVisible(mode=="reference")

        correction_mode=str(context.get("correction_mode") or "per_spot")
        correction_model_combo=getattr(
            self,"position_reference_correction_model_combo",None,
        )
        if correction_model_combo is not None:
            correction_blocker=QtCore.QSignalBlocker(correction_model_combo)
            try:
                correction_index=correction_model_combo.findData(correction_mode)
                correction_model_combo.setCurrentIndex(
                    correction_index if correction_index>=0 else 0
                )
                per_spot_index=correction_model_combo.findData("per_spot")
                if per_spot_index>=0:
                    per_spot_item=correction_model_combo.model().item(per_spot_index)
                    if per_spot_item is not None:
                        per_spot_item.setEnabled(mode!="current")
                        per_spot_item.setToolTip(
                            "Free editing requires Translation, Rigid, Affine, or "
                            "Polynomial." if mode=="current" else ""
                        )
            finally:
                del correction_blocker

        polynomial_degree=int(context.get("polynomial_degree") or 2)
        polynomial_degree_combo=getattr(
            self,"position_reference_polynomial_degree_combo",None,
        )
        if polynomial_degree_combo is not None:
            polynomial_blocker=QtCore.QSignalBlocker(polynomial_degree_combo)
            try:
                polynomial_index=polynomial_degree_combo.findData(polynomial_degree)
                polynomial_degree_combo.setCurrentIndex(
                    polynomial_index if polynomial_index>=0 else 0
                )
            finally:
                del polynomial_blocker

        change_allowed=bool(
            context.get("change_allowed",True)
            and not self._cgh_computing
            and self._selected_is_current_context()
        )
        reason=str(context.get("change_unavailable_reason") or "")
        source_valid=bool(mode!="reference" or reference_name)
        combo.setEnabled(change_allowed)
        combo.setToolTip(reason)
        if saved_combo is not None:
            saved_combo.setEnabled(change_allowed and bool(references))
            saved_combo.setToolTip(reason)

        if correction_model_combo is not None:
            correction_model_combo.setEnabled(change_allowed and source_valid)
            correction_model_combo.setToolTip(
                reason or (
                    "Translation fits one common shift; Rigid fits translation + "
                    "rotation; Affine allows scale and shear; Polynomial fits a "
                    "smooth nonlinear field; Per-spot applies each reference error "
                    "independently."
                )
            )

        polynomial_visible=correction_mode=="polynomial"
        polynomial_degree_label=getattr(
            self,"position_reference_polynomial_degree_label",None,
        )
        if polynomial_degree_label is not None:
            polynomial_degree_label.setVisible(polynomial_visible)
        if polynomial_degree_combo is not None:
            polynomial_degree_combo.setVisible(polynomial_visible)
            polynomial_degree_combo.setEnabled(
                change_allowed and source_valid and polynomial_visible
            )
            polynomial_degree_combo.setToolTip(
                reason or (
                    "Polynomial order 2 needs at least 6 fit constraints, order 3 "
                    "needs 10, and order 4 needs 15. Interactive editing uses a "
                    "sparse deformation mesh."
                )
            )

        interactive=getattr(
            self,"position_reference_interactive_edit_checkbox",None,
        )
        interactive_supported=bool(
            source_valid
            and correction_mode in {"translation","rigid","affine","polynomial"}
            and self._position_reference_preview is not None
        )
        if interactive is not None:
            interactive.setVisible(
                correction_mode in {"translation","rigid","affine","polynomial"}
            )
            interactive.setEnabled(change_allowed and interactive_supported)
            interactive.setToolTip(
                reason or (
                    "Translation: drag the center. Rigid: drag the center and "
                    "rotation handle. Affine: drag the cage handles. Polynomial: "
                    "drag deformation-mesh handles. Predicted updates live."
                )
            )
            if not interactive_supported and interactive.isChecked():
                interactive_blocker=QtCore.QSignalBlocker(interactive)
                interactive.setChecked(False)
                del interactive_blocker

        has_manual=context.get("manual_transform") is not None
        continuous_mode=correction_mode in {
            "translation","rigid","affine","polynomial",
        }
        reset_button=getattr(self,"position_reference_reset_button",None)
        if reset_button is not None:
            reset_button.setVisible(continuous_mode)
            reset_button.setEnabled(
                change_allowed and interactive_supported and has_manual
            )
        edit_state=getattr(self,"position_reference_edit_state_label",None)
        if edit_state is not None:
            edit_state.setVisible(continuous_mode)
            edit_state.setText("Edited" if has_manual else "Automatic")
        hint=getattr(self,"position_reference_edit_hint",None)
        if hint is not None:
            if correction_mode=="translation":
                hint.setText("Drag the cage center to translate the correction.")
            elif correction_mode=="rigid":
                hint.setText(
                    "Drag the cage center to translate; drag the rotation handle "
                    "to rotate without changing scale."
                )
            elif correction_mode=="affine":
                hint.setText(
                    "Drag the cage center to translate; drag the two axis handles "
                    "to scale, rotate, or shear."
                )
            elif correction_mode=="polynomial":
                hint.setText(
                    "Drag deformation-mesh handles to reshape the smooth correction "
                    "field locally."
                )
            hint.setVisible(
                interactive_supported and interactive is not None
                and interactive.isChecked()
            )

        for editor in self._position_geometry_editors():
            editor.setEnabled(change_allowed)
            editor.setToolTip(reason)
        for lock in self._position_geometry_lock_checkboxes():
            lock.setEnabled(change_allowed)
            lock.setToolTip(reason or "Keep this value fixed when using Fit center.")
        button_tooltips={
            "position_reference_square_straight_button":(
                "Set rotation to 0°, lattice angle to 90°, and lock both values."
            ),
            "position_reference_fit_center_button":(
                "Fit the editable lattice to the central localized spots while "
                "keeping locked geometry values fixed."
            ),
            "position_reference_reset_fit_button":(
                "Reset editable lattice geometry to the current global fit and "
                "unlock all geometry values."
            ),
        }
        for button_name,default_tooltip in button_tooltips.items():
            button=getattr(self,button_name,None)
            if button is not None:
                button.setEnabled(change_allowed)
                button.setToolTip(reason or default_tooltip)

        self._refresh_position_reference_correspondence_controls(
            mode=mode,change_allowed=change_allowed,
        )

        compatible=bool(context.get("compatible",True))
        compatibility_error=str(context.get("compatibility_error") or "")
        plane=str(context.get("observation_plane_name") or "").strip()
        if mode=="global_fit":
            info="Reference: current localization global fit"
        elif mode=="editable":
            info="Reference: editable lattice"
        elif mode=="current":
            info="Reference: current localization · free-edit baseline"
        elif reference_name:
            info=""
        else:
            info=""
        if plane:
            info=((info+" · ") if info else "")+"Plane: %s" % plane
        if not compatible and compatibility_error:
            info=compatibility_error
        self.position_reference_info_label.setText(info)
        self.position_reference_info_label.setStyleSheet(
            "color: %s;" % (_WARNING_COLOR if not compatible else _MUTED_COLOR)
        )

        detector_view=getattr(self,"position_reference_view",None)
        if detector_view is not None:
            detector_view.set_center_editable(mode=="editable" and change_allowed)
            detector_view.set_interactive_editing(
                bool(
                    interactive_supported and change_allowed
                    and interactive is not None and interactive.isChecked()
                ),
                correction_mode,
            )

        add_button=getattr(self,"position_reference_add_button",None)
        if add_button is not None:
            create_reason=str(context.get("create_unavailable_reason") or "")
            create_allowed=bool(
                context.get("create_allowed",False)
                and self._selected_is_current_context()
                and not self._cgh_computing
            )
            add_button.setEnabled(create_allowed)
            add_button.setToolTip(create_reason)

        self._refresh_position_reference_manager_actions()

    def _refresh_position_reference_correspondence_controls(
        self,*,mode: str,change_allowed: bool,
    ) -> None:
        combo = getattr(self,"position_reference_correspondence_combo",None)
        info = getattr(self,"position_reference_correspondence_info",None)
        if combo is None:
            return
        context = dict(self._position_reference_context or {})
        requested = str(context.get("correspondence") or "auto")
        resolved = str(context.get("correspondence_resolved") or "canonical")
        options = tuple(str(value) for value in (
            context.get("correspondence_options") or ()
        ))
        if "auto" not in options:
            options = ("auto",)+options
        if "canonical" not in options:
            options = options+("canonical",)

        blocker = QtCore.QSignalBlocker(combo)
        try:
            combo.clear()
            for value in options:
                if value == "auto":
                    try:
                        label = "Auto — %s" % spot_ordering_label(resolved)
                    except Exception:
                        label = "Auto"
                else:
                    try:
                        label = spot_ordering_label(value)
                    except Exception:
                        label = value.replace("_"," ").title()
                combo.addItem(label,value)
            index = combo.findData(requested)
            combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            del blocker
        combo.setEnabled(mode == "reference" and change_allowed and combo.count()>0)

        diagnostics = dict(context.get("correspondence_diagnostics") or {})
        if info is not None:
            if mode != "reference" or not diagnostics:
                info.setText("")
            else:
                resolved_value = str(diagnostics.get("resolved") or resolved)
                try:
                    resolved_label = spot_ordering_label(resolved_value)
                except Exception:
                    resolved_label = resolved_value.replace("_"," ").title()
                prefix = (
                    "Auto → %s" % resolved_label
                    if requested == "auto" else resolved_label
                )
                details = [prefix]
                rotation = diagnostics.get("relative_rotation_deg")
                rms = diagnostics.get("rms_px")
                if rotation is not None:
                    details.append("Δrot %+.2f°" % float(rotation))
                if rms is not None:
                    details.append("RMS %.3g px" % float(rms))
                if bool(diagnostics.get("reflection",False)):
                    details.append("reflection")
                info.setText(" · ".join(details))

    def _selected_managed_position_reference_name(self) -> str:
        combo = getattr(self,"position_reference_saved_combo",None)
        if combo is None or combo.currentIndex() < 0:
            return ""
        return str(combo.currentData() or "").strip()

    def _selected_managed_position_reference_lifetime(self) -> str:
        combo = getattr(self,"position_reference_saved_combo",None)
        if combo is None or combo.currentIndex() < 0:
            return ""
        return str(combo.currentData(QtCore.Qt.UserRole+1) or "").strip()

    def _refresh_position_reference_manager_actions(self,*_args: Any) -> None:
        context = dict(self._position_reference_context or {})
        name = self._selected_managed_position_reference_name()
        lifetime = self._selected_managed_position_reference_lifetime()
        current_context = bool(
            self._selected_is_current_context() and not self._cgh_computing
        )
        saved_mode = str(context.get("mode") or "") == "reference"
        button = getattr(self,"position_reference_manage_button",None)
        if button is not None:
            button.setEnabled(bool(name) and current_context and saved_mode)

        delete_action = getattr(self,"position_reference_delete_action",None)
        if delete_action is not None:
            delete_action.setEnabled(bool(name) and current_context and saved_mode)

        persist_action = getattr(self,"position_reference_persist_action",None)
        if persist_action is not None:
            persistence_available = bool(context.get("persistence_available",False))
            persist_action.setEnabled(
                bool(name) and lifetime == "session" and current_context
                and saved_mode and persistence_available
            )
            if lifetime == "persistent":
                persist_action.setToolTip("This reference is already persistent.")
            elif not persistence_available:
                persist_action.setToolTip(
                    "Persistent position-reference storage is unavailable."
                )
            else:
                persist_action.setToolTip(
                    "Keep this reference across application sessions."
                )

    def _make_position_geometry_spinbox(
        self,minimum: float,maximum: float,decimals: int,suffix: str,step: float,
    ) -> QtWidgets.QDoubleSpinBox:
        editor = QtWidgets.QDoubleSpinBox()
        editor.setRange(float(minimum),float(maximum))
        editor.setDecimals(int(decimals))
        editor.setSingleStep(float(step))
        editor.setSuffix(str(suffix))
        editor.setKeyboardTracking(False)
        return editor

    def _position_geometry_editors(self) -> tuple[QtWidgets.QDoubleSpinBox,...]:
        names = (
            "position_reference_period_x",
            "position_reference_period_y",
            "position_reference_rotation",
            "position_reference_lattice_angle",
            "position_reference_offset_x",
            "position_reference_offset_y",
        )
        return tuple(
            editor for editor in (getattr(self,name,None) for name in names)
            if editor is not None
        )

    def _position_geometry_lock_checkboxes(self) -> tuple[QtWidgets.QCheckBox,...]:
        names = (
            "position_reference_period_x_lock",
            "position_reference_period_y_lock",
            "position_reference_rotation_lock",
            "position_reference_lattice_angle_lock",
        )
        return tuple(
            checkbox for checkbox in (getattr(self,name,None) for name in names)
            if checkbox is not None
        )

    def _set_editable_lock_controls(self,locks: Mapping[str,Any]) -> None:
        values = (
            ("position_reference_period_x_lock","period_x_px"),
            ("position_reference_period_y_lock","period_y_px"),
            ("position_reference_rotation_lock","rotation_deg"),
            ("position_reference_lattice_angle_lock","lattice_angle_deg"),
        )
        for widget_name,key in values:
            checkbox = getattr(self,widget_name,None)
            if checkbox is None:
                continue
            blocker = QtCore.QSignalBlocker(checkbox)
            try:
                checkbox.setChecked(bool(locks.get(key,False)))
            finally:
                del blocker

    def _editable_locks_payload(self) -> dict[str,bool]:
        return {
            "period_x_px":bool(self.position_reference_period_x_lock.isChecked()),
            "period_y_px":bool(self.position_reference_period_y_lock.isChecked()),
            "rotation_deg":bool(self.position_reference_rotation_lock.isChecked()),
            "lattice_angle_deg":bool(
                self.position_reference_lattice_angle_lock.isChecked()
            ),
        }

    def _set_editable_geometry_controls(
        self,geometry: Mapping[str,Any],
    ) -> None:
        values = (
            ("position_reference_period_x","period_x_px"),
            ("position_reference_period_y","period_y_px"),
            ("position_reference_rotation","rotation_deg"),
            ("position_reference_lattice_angle","lattice_angle_deg"),
            ("position_reference_offset_x","offset_x_px"),
            ("position_reference_offset_y","offset_y_px"),
        )
        for widget_name,key in values:
            editor = getattr(self,widget_name,None)
            if editor is None or key not in geometry:
                continue
            blocker = QtCore.QSignalBlocker(editor)
            try:
                editor.setValue(float(geometry[key]))
            finally:
                del blocker

    def _editable_geometry_payload(self) -> dict[str,Any]:
        context = dict(self._position_reference_context or {})
        geometry = dict(
            context.get("editable_geometry")
            or context.get("fit_geometry")
            or {}
        )
        geometry.update({
            "period_x_px":float(self.position_reference_period_x.value()),
            "period_y_px":float(self.position_reference_period_y.value()),
            "rotation_deg":float(self.position_reference_rotation.value()),
            "lattice_angle_deg":float(
                self.position_reference_lattice_angle.value()
            ),
            "offset_x_px":float(self.position_reference_offset_x.value()),
            "offset_y_px":float(self.position_reference_offset_y.value()),
        })
        geometry.setdefault("handedness",1)
        return geometry

    def _emit_editable_geometry(self) -> None:
        if str(self.position_reference_combo.currentData()) != "editable":
            return
        self._emit(
            MeasurementsAction.POSITION_REFERENCE_SET,
            {
                "mode":"editable",
                "editable_geometry":self._editable_geometry_payload(),
                "editable_locks":self._editable_locks_payload(),
                "correction_mode":self._selected_position_reference_correction_mode(),
                "polynomial_degree":self._selected_position_reference_polynomial_degree(),
                "manual_transform":None,
            },
        )

    def _position_reference_request_payload(
        self,*,reset_manual: bool=False,correspondence: str | None=None,
    ) -> dict[str,Any] | None:
        mode=str(self.position_reference_combo.currentData() or "global_fit")
        payload: dict[str,Any]={
            "mode":mode,
            "correction_mode":self._selected_position_reference_correction_mode(),
            "polynomial_degree":self._selected_position_reference_polynomial_degree(),
        }
        if mode=="editable":
            payload["editable_geometry"]=self._editable_geometry_payload()
            payload["editable_locks"]=self._editable_locks_payload()
        elif mode=="reference":
            name=self._selected_managed_position_reference_name()
            if not name:
                return None
            payload["reference_name"]=name
            payload["correspondence"]=(
                correspondence
                if correspondence is not None
                else str(
                    getattr(self,"position_reference_correspondence_combo").currentData()
                    or "auto"
                )
            )
        if reset_manual:
            payload["manual_transform"]=None
        return payload

    def _on_position_reference_changed(self,_index: int) -> None:
        mode=str(self.position_reference_combo.currentData() or "global_fit")
        if mode=="reference":
            # Merely choosing the Saved reference source must not silently
            # activate whichever reference happens to be first/previously shown.
            # Reveal the panel in a deliberately unselected state and wait for
            # _on_saved_position_reference_changed().
            saved_combo=getattr(self,"position_reference_saved_combo",None)
            if saved_combo is not None:
                blocker=QtCore.QSignalBlocker(saved_combo)
                try:
                    saved_combo.setCurrentIndex(0 if saved_combo.count() else -1)
                finally:
                    del blocker
            editable_widget=getattr(self,"position_reference_editable_widget",None)
            if editable_widget is not None:
                editable_widget.setVisible(False)
            saved_widget=getattr(self,"position_reference_saved_widget",None)
            if saved_widget is not None:
                saved_widget.setVisible(True)

            # This is a UI-only pending state: the backend keeps the previous
            # source until a concrete name is chosen, so do not allow correction
            # edits against that stale source in the meantime.
            correspondence=getattr(
                self,"position_reference_correspondence_combo",None,
            )
            if correspondence is not None:
                correspondence.setEnabled(False)
            correspondence_info=getattr(
                self,"position_reference_correspondence_info",None,
            )
            if correspondence_info is not None:
                correspondence_info.setText("")
            info=getattr(self,"position_reference_info_label",None)
            if info is not None:
                info.setText("Select a saved reference.")
                info.setStyleSheet("color: %s;" % _MUTED_COLOR)

            correction_combo=getattr(
                self,"position_reference_correction_model_combo",None,
            )
            if correction_combo is not None:
                correction_combo.setEnabled(False)
            polynomial_combo=getattr(
                self,"position_reference_polynomial_degree_combo",None,
            )
            if polynomial_combo is not None:
                polynomial_combo.setEnabled(False)
            interactive=getattr(
                self,"position_reference_interactive_edit_checkbox",None,
            )
            if interactive is not None:
                interactive_blocker=QtCore.QSignalBlocker(interactive)
                try:
                    interactive.setChecked(False)
                finally:
                    del interactive_blocker
                interactive.setEnabled(False)
            reset_button=getattr(
                self,"position_reference_reset_button",None,
            )
            if reset_button is not None:
                reset_button.setEnabled(False)
            edit_state=getattr(self,"position_reference_edit_state_label",None)
            if edit_state is not None:
                edit_state.setVisible(False)
            detector_view=getattr(self,"position_reference_view",None)
            if detector_view is not None:
                detector_view.set_center_editable(False)
                detector_view.set_interactive_editing(False)

            self._refresh_position_reference_manager_actions()
            return

        if (
            mode=="current"
            and self._selected_position_reference_correction_mode()=="per_spot"
        ):
            correction_combo=getattr(
                self,"position_reference_correction_model_combo",None,
            )
            if correction_combo is not None:
                translation_index=correction_combo.findData("translation")
                if translation_index>=0:
                    correction_blocker=QtCore.QSignalBlocker(correction_combo)
                    try:
                        correction_combo.setCurrentIndex(translation_index)
                    finally:
                        del correction_blocker

        payload=self._position_reference_request_payload(reset_manual=True)
        if payload is None:
            return
        self._emit(MeasurementsAction.POSITION_REFERENCE_SET,payload)

    def _on_saved_position_reference_changed(self,_index: int) -> None:
        if str(self.position_reference_combo.currentData())!="reference":
            return
        name=self._selected_managed_position_reference_name()
        if not name:
            return
        active=str(self._position_reference_context.get("reference_name") or "")
        if name==active:
            return
        payload=self._position_reference_request_payload(
            reset_manual=True,correspondence="auto",
        )
        if payload is not None:
            self._emit(MeasurementsAction.POSITION_REFERENCE_SET,payload)

    def _selected_position_reference_correction_mode(self) -> str:
        combo=getattr(self,"position_reference_correction_model_combo",None)
        if combo is None:
            return "per_spot"
        return str(combo.currentData() or "per_spot")

    def _selected_position_reference_polynomial_degree(self) -> int:
        combo=getattr(self,"position_reference_polynomial_degree_combo",None)
        if combo is None:
            return 2
        value=combo.currentData()
        return int(2 if value is None else value)

    def _on_position_reference_interactive_edit_toggled(
        self,checked: bool,
    ) -> None:
        correction_mode=self._selected_position_reference_correction_mode()
        enabled=bool(
            checked
            and correction_mode in {"translation","rigid","affine","polynomial"}
            and self._selected_is_current_context()
            and not self._cgh_computing
            and self._position_reference_preview is not None
        )
        self.position_reference_view.set_interactive_editing(
            enabled,correction_mode,
        )
        hint=getattr(self,"position_reference_edit_hint",None)
        if hint is not None:
            hint.setVisible(enabled)

    def _on_position_reference_polynomial_degree_changed(self,_index: int) -> None:
        if self._selected_position_reference_correction_mode()!="polynomial":
            return
        degree=self._selected_position_reference_polynomial_degree()
        current=int(self._position_reference_context.get("polynomial_degree") or 2)
        if degree==current:
            return
        payload=self._position_reference_request_payload(reset_manual=True)
        if payload is not None:
            self._emit(MeasurementsAction.POSITION_REFERENCE_SET,payload)

    def _on_position_reference_correction_mode_changed(self,_index: int) -> None:
        correction_mode=self._selected_position_reference_correction_mode()
        if (
            str(self.position_reference_combo.currentData())=="current"
            and correction_mode=="per_spot"
        ):
            return
        current=str(self._position_reference_context.get("correction_mode") or "per_spot")
        if correction_mode==current:
            return
        payload=self._position_reference_request_payload(reset_manual=True)
        if payload is not None:
            self._emit(MeasurementsAction.POSITION_REFERENCE_SET,payload)

    def _reset_position_reference_manual_edit(self,*_args: Any) -> None:
        if self._selected_position_reference_correction_mode() not in {
            "translation","rigid","affine","polynomial",
        }:
            return
        payload=self._position_reference_request_payload(reset_manual=True)
        if payload is not None:
            self._emit(MeasurementsAction.POSITION_REFERENCE_SET,payload)

    def _on_position_reference_correspondence_changed(self,_index: int) -> None:
        if str(self.position_reference_combo.currentData())!="reference":
            return
        combo=getattr(self,"position_reference_correspondence_combo",None)
        if combo is None:
            return
        correspondence=str(combo.currentData() or "auto")
        current=str(self._position_reference_context.get("correspondence") or "auto")
        if correspondence==current:
            return
        payload=self._position_reference_request_payload(
            reset_manual=True,correspondence=correspondence,
        )
        if payload is not None:
            self._emit(MeasurementsAction.POSITION_REFERENCE_SET,payload)

    def _square_and_straight_editable_geometry(self,*_args: Any) -> None:
        if str(self.position_reference_combo.currentData()) != "editable":
            return
        widgets = (
            (self.position_reference_rotation,0.0),
            (self.position_reference_lattice_angle,90.0),
        )
        blockers = [
            QtCore.QSignalBlocker(widget) for widget in (
                self.position_reference_rotation,
                self.position_reference_lattice_angle,
                self.position_reference_rotation_lock,
                self.position_reference_lattice_angle_lock,
            )
        ]
        try:
            for widget,value in widgets:
                widget.setValue(float(value))
            self.position_reference_rotation_lock.setChecked(True)
            self.position_reference_lattice_angle_lock.setChecked(True)
        finally:
            del blockers
        self._emit_editable_geometry()

    def _fit_editable_geometry_center(self,*_args: Any) -> None:
        if str(self.position_reference_combo.currentData()) != "editable":
            return
        self._emit(MeasurementsAction.POSITION_REFERENCE_FIT_CENTER,{})

    def _on_editable_geometry_changed(self,*_args: Any) -> None:
        self._emit_editable_geometry()

    def _on_editable_locks_changed(self,*_args: Any) -> None:
        self._emit_editable_geometry()

    def _reset_editable_geometry_from_fit(self,*_args: Any) -> None:
        geometry = dict(
            self._position_reference_context.get("fit_geometry") or {}
        )
        if not geometry:
            return
        geometry["offset_x_px"] = 0.0
        geometry["offset_y_px"] = 0.0
        self._set_editable_geometry_controls(geometry)
        self._set_editable_lock_controls({})
        self._emit_editable_geometry()

    def _on_position_reference_center_dragged(
        self,offset_x: float,offset_y: float,
    ) -> None:
        if str(self.position_reference_combo.currentData()) != "editable":
            return
        for editor,value in (
            (self.position_reference_offset_x,offset_x),
            (self.position_reference_offset_y,offset_y),
        ):
            blocker = QtCore.QSignalBlocker(editor)
            try:
                editor.setValue(float(value))
            finally:
                del blocker
        self._emit_editable_geometry()

    def _on_position_reference_manual_transform_dragged(
        self,transform: Any,
    ) -> None:
        correction_mode=self._selected_position_reference_correction_mode()
        if correction_mode not in {"translation","rigid","affine","polynomial"}:
            return
        if not isinstance(transform,Mapping):
            return
        if str(transform.get("model") or "")!=correction_mode:
            return
        payload=self._position_reference_request_payload(reset_manual=False)
        if payload is None:
            return
        payload["manual_transform"]=dict(transform)
        payload["lightweight"]=True
        self._emit(MeasurementsAction.POSITION_REFERENCE_SET,payload)

    def _on_position_view_changed(self,index: int) -> None:
        stack = getattr(self,"position_view_stack",None)
        if stack is not None:
            stack.setCurrentIndex(max(0,min(int(index),stack.count()-1)))
        self._refresh_position_background_controls()

    def _position_visual_snap_requested(self,*_args: Any) -> None:
        detector = self.measurement_view.current_detector
        if detector:
            self._emit(
                MeasurementsAction.VISUAL_BACKGROUND_ACQUIRE,{
                    "detector":str(detector),
                },
            )

    def set_position_visual_background(self,measurement: Any) -> None:
        """Install a transient detector image without changing localization state."""
        image=np.asarray(getattr(measurement,"image",None),dtype=np.float64)
        if image.ndim!=2:
            raise ValueError("Visual background measurement image must be two-dimensional")
        self._position_visual_background_measurement=measurement
        self._refresh_position_background_controls()
        self._apply_position_background_image()

    def _on_position_background_changed(self,_index: int) -> None:
        mode=str(self.position_background_combo.currentData() or "localization")
        self._position_background_mode=mode
        self._apply_position_background_image()

    def _visual_background_crop(self) -> np.ndarray | None:
        measurement=self._position_visual_background_measurement
        preview=self._position_reference_preview
        if measurement is None or preview is None:
            return None
        image=np.asarray(getattr(measurement,"image",None),dtype=np.float64)
        localization_image=np.asarray(preview.get("cropped_image"),dtype=np.float64)
        offset=np.asarray(
            preview.get("crop_offset_px",(0.0,0.0)),dtype=np.float64,
        ).reshape(2)
        if image.ndim!=2 or localization_image.ndim!=2:
            return None
        x0=int(round(float(offset[0])))
        y0=int(round(float(offset[1])))
        height,width=localization_image.shape
        x1=x0+int(width)
        y1=y0+int(height)
        if x0<0 or y0<0 or x1>image.shape[1] or y1>image.shape[0]:
            return None
        crop=np.asarray(image[y0:y1,x0:x1],dtype=np.float64)
        if crop.shape!=localization_image.shape:
            return None
        return crop

    def _refresh_position_background_controls(self) -> None:
        combo=getattr(self,"position_background_combo",None)
        label=getattr(self,"position_background_label",None)
        if combo is None:
            return
        detector_mode=str(self.position_view_combo.currentData() or "detector")=="detector"
        if label is not None:
            label.setVisible(detector_mode)
        combo.setVisible(detector_mode)

        visual_available=self._visual_background_crop() is not None
        blocker=QtCore.QSignalBlocker(combo)
        try:
            current=str(self._position_background_mode or "localization")
            combo.clear()
            combo.addItem("Localization image","localization")
            if visual_available:
                combo.addItem("Latest visual snap","visual_snap")
            if current=="visual_snap" and visual_available:
                index=combo.findData("visual_snap")
                combo.setCurrentIndex(index if index>=0 else 0)
            else:
                if current=="visual_snap":
                    self._position_background_mode="localization"
                combo.setCurrentIndex(0)
        finally:
            del blocker

        snap_button=getattr(self,"position_visual_snap_button",None)
        if snap_button is not None:
            allowed=bool(
                self._position_reference_preview is not None
                and self._selected_is_interactive()
                and not self._cgh_computing
                and self.measurement_view.current_detector
            )
            snap_button.setEnabled(allowed)
            if allowed:
                snap_button.setToolTip(
                    "Acquire a detector image for optional use as the Position "
                    "overlay background. This does not replace the localization measurement."
                )
            else:
                snap_button.setToolTip(
                    "A current accepted localization and detector are required."
                )

    def _apply_position_background_image(self) -> None:
        view=getattr(self,"position_reference_view",None)
        preview=self._position_reference_preview
        if view is None or preview is None or not self._selected_is_current_context():
            return
        image=np.asarray(preview.get("cropped_image"),dtype=np.float64)
        if self._position_background_mode=="visual_snap":
            visual=self._visual_background_crop()
            if visual is not None:
                image=visual
            else:
                self._position_background_mode="localization"
                self._refresh_position_background_controls()
        view.set_background_image(image)

    def _add_position_reference_requested(self,*_args: Any) -> None:
        name,ok = QtWidgets.QInputDialog.getText(
            self,"Add session reference","Reference name:"
        )
        name = str(name or "").strip()
        if ok and name:
            self._emit(
                MeasurementsAction.POSITION_REFERENCE_ADD,{"name":name},
            )

    def _persist_position_reference_requested(self,*_args: Any) -> None:
        name = self._selected_managed_position_reference_name()
        if name:
            self._emit(
                MeasurementsAction.POSITION_REFERENCE_PERSIST,{"name":name},
            )

    def _delete_position_reference_requested(self,*_args: Any) -> None:
        name = self._selected_managed_position_reference_name()
        if name:
            self._emit(
                MeasurementsAction.POSITION_REFERENCE_DELETE,{"name":name},
            )

    def configure_observation_planes(
        self,planes: Sequence[str],current_plane: str | None=None,
    ) -> None:
        combo = getattr(self,"observation_plane_combo",None)
        if combo is None:
            return
        blocker = QtCore.QSignalBlocker(combo)
        combo.clear()
        names = tuple(str(name) for name in planes if str(name).strip())
        if current_plane is None or str(current_plane) not in names:
            combo.addItem("Select observation…",None)
        for name in names:
            combo.addItem(name,name)
        combo.insertSeparator(combo.count())
        combo.addItem("Define new observation plane…",_DEFINE_OBSERVATION_PLANE)
        if current_plane is not None:
            index = combo.findData(str(current_plane))
            if index >= 0:
                combo.setCurrentIndex(index)
        del blocker

    def _on_observation_plane_changed(self,_index: int) -> None:
        plane = self.observation_plane_combo.currentData()
        if plane == _DEFINE_OBSERVATION_PLANE:
            self.sigDefineObservationPlaneRequested.emit()
            return
        self._emit(MeasurementsAction.OBSERVATION_PLANE_SET,{"plane":plane})

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
        self._normal_localization_context=dict(localization_context or {})
        self.measurement_view.set_context(self._normal_localization_context)
        self.measurement_view.set_parameters(
            status.localization_params,invalidate_candidate=False,
        )
        self.intensity_form.set_values(status.intensity_params,emit=False)
        self._populate_position_selector()
        self._populate_round_selector(follow_latest=follow_latest)
        self._refresh_header()
        self._sync_selected_round()
        self._refresh_status_controls()
        self._refresh_feedback_visualizations()
        if self._automatic_operation_active:
            self._apply_automatic_interaction_lock()

    def set_feedback_state(
        self,
        status: FeedbackStatus,
        inspection: FeedbackInspection,
        localization_context: Mapping[str,Any] | None=None,
    ) -> None:
        """Refresh measurement/localization controls without rebuilding CGH round views."""
        self._status=status
        self._inspection=inspection
        self._session_inspection=replace(
            self._session_inspection,measurement=inspection.measurement,
        )
        self._normal_localization_context=dict(localization_context or {})
        self.measurement_view.set_context(self._normal_localization_context)
        self.intensity_form.set_values(status.intensity_params,emit=False)
        self._refresh_header()
        self._sync_selected_round()
        self._refresh_status_controls()
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
            else "Compute"
        )
        for button in self._compute_adapted_buttons:
            button.setText(text)
        self._refresh_header()
        self._sync_selected_round()
        self._refresh_position_reference_controls()
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
            self._displayed_integration_preview = None
            self._candidate_metrics = None
            self._refresh_metrics_display(None)
        else:
            if changed:
                self.measurement_view.set_measurement(acquisition)
                self._displayed_integration_preview = None
                self.measurement_view.set_parameters(
                    self._status.localization_params,
                    invalidate_candidate=False,
                )
                self._candidate_metrics = None
            committed = measurement.localization
            if committed is not None:
                candidate = self.measurement_view.candidate
                if (
                    (candidate is None or candidate is committed)
                    and self.measurement_view.committed_result is not committed
                ):
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
            if self._displayed_integration_preview is not None:
                self.measurement_view.clear_auxiliary_image("Integration")
                self._displayed_integration_preview = None
        elif preview is not self._displayed_integration_preview:
            self.measurement_view.set_auxiliary_image("Integration",preview)
            self._displayed_integration_preview = preview

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

        self._refresh_position_reference_visualization()

    def _refresh_position_reference_visualization(self) -> None:
        """Refresh only the position-reference views, not the whole session UI."""
        preview = (
            self._position_reference_preview
            if self._selected_is_current_context() else None
        )
        mode = str(
            dict(self._position_reference_context or {}).get("mode")
            or "global_fit"
        )
        change_allowed = bool(
            dict(self._position_reference_context or {}).get(
                "change_allowed",True
            )
            and not self._cgh_computing
            and self._selected_is_current_context()
        )
        if preview is not None:
            self.position_reference_view.set_preview(
                preview,
                center_editable=(mode == "editable" and change_allowed),
            )
            # set_preview() already installs the localization image when the
            # localization changes. Only re-apply an explicit visual snap here;
            # otherwise this would upload/autolevel the same detector image twice.
            if self._position_background_mode == "visual_snap":
                self._apply_position_background_image()
            self.position_correction_view.set_data(
                ideal_positions_kxy=preview.get(
                    "baseline_positions_kxy",preview.get("ideal_positions_kxy")
                ),
                displacement_kxy=preview.get("displacement_kxy"),
            )
        else:
            self.position_reference_view.clear()
            self._refresh_position_background_controls()
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

        self._refresh_position_reference_controls()
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
        position_stale = bool(getattr(status,"position_stale",False))
        position_stale_reason = str(
            getattr(status,"position_stale_reason","") or ""
        )
        if position_pending:
            if status.position_available:
                position_text = (
                    "Applying correction · hologram pending"
                    if status.position_active
                    else "Using baseline · hologram pending"
                )
            else:
                position_text = "Correction cleared · hologram pending"
            position_color = _WARNING_COLOR
        elif position_stale:
            position_text = "Stored correction stale · FOV mapping correction changed"
            position_color = _WARNING_COLOR
        elif status.position_available and status.position_active:
            position_text,position_color = "Correction active",_OK_COLOR
        elif status.position_available:
            position_text,position_color = (
                "Correction stored · using baseline",_MUTED_COLOR
            )
        else:
            position_text,position_color = "No correction",_MUTED_COLOR
        self.position_status_label.setText(position_text)
        self.position_status_label.setStyleSheet("color: %s;" % position_color)
        use_blocker = QtCore.QSignalBlocker(self.position_use_checkbox)
        self.position_use_checkbox.setChecked(bool(status.position_active))
        del use_blocker
        self.position_use_checkbox.setEnabled(
            position_available
            and status.position_available
            and not position_stale
            and not position_pending
            and current_context
            and not self._cgh_computing
        )
        self.position_use_checkbox.setToolTip(
            position_stale_reason
            if position_stale else
            "Temporarily switch between the stored corrected positions and "
            "the FOV-corrected baseline. The hologram is recomputed automatically."
        )
        position_localization_complete = bool(
            localized
            and status.localization_total_count > 0
            and (
                status.localization_matched_count + status.localization_inferred_count
                == status.localization_total_count
            )
        )
        reference_compatible = bool(
            self._position_reference_context.get("compatible",True)
        )
        self.position_apply_button.setText(
            "New correction" if position_stale else
            "Replace correction" if status.position_available else
            "Apply correction"
        )
        self.position_apply_button.setEnabled(
            position_available
            and position_localization_complete
            and reference_compatible
            and viewing_current
            and not self._cgh_computing
            and not position_pending
        )
        if localized and not position_localization_complete:
            position_tip = (
                "Resolve every target spot before position correction; use Infer missing for unmatched sites."
            )
        elif not reference_compatible:
            position_tip = str(
                self._position_reference_context.get("compatibility_error") or
                "The selected position reference is incompatible."
            )
        else:
            position_tip = ""
        self.position_apply_button.setToolTip(position_tip)
        self.position_clear_button.setEnabled(
            position_available
            and status.position_available
            and current_context
            and not self._cgh_computing
            and not position_pending
        )

        reuse_available = bool(
            intensity_available
            and viewing_current
            and status.previous_localization_available
            and not self._cgh_computing
        )
        self.reuse_localization_checkbox.setEnabled(reuse_available)
        self.reuse_localization_checkbox.setToolTip(
            "Reuse the previous accepted localization automatically after "
            "Acquire/Load."
            if reuse_available else
            "Available when a previous accepted localization exists."
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

    def _infer_missing_localization_candidate(self,*_args: Any) -> None:
        if (
            not self._selected_is_interactive()
            or not self.measurement_view.candidate_is_current
        ):
            return
        self._emit(
            MeasurementsAction.LOCALIZATION_INFER_MISSING,
            {
                "localization":self.measurement_view.candidate,
                "parameters":dict(
                    self.measurement_view.candidate_parameters or {}
                ),
            },
        )

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
