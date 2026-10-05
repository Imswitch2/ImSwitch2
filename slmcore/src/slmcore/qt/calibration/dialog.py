"""Reusable SLM calibration dialog UI."""

from __future__ import annotations

from typing import Any,Mapping,Sequence

from qtpy import QtCore,QtGui,QtWidgets

from ...core.cgh.localization.parameters import LOCALIZATION_PARAMS
from ..measurement.localization_view import MeasurementLocalizationView


LINEAR_PHASE_METHOD = "linear_phase"
TARGET_LOCALIZATION_METHOD = "target_localization"
_DEFINE_OBSERVATION_PLANE = "__define_observation_plane__"


def _default_values(specs: Mapping[str,Any]) -> dict[str, Any]:
    return {key:spec.validate(spec.default) for key,spec in specs.items()}


class CalibrationDialog(QtWidgets.QDialog):
    """Complete host-neutral calibration UI.

    The dialog owns calibration presentation only. Hosts provide detectors,
    measurements, localization results and eventual calibration persistence by
    responding to the emitted signals.
    """

    LINEAR_PHASE = LINEAR_PHASE_METHOD
    TARGET_LOCALIZATION = TARGET_LOCALIZATION_METHOD

    sigAcquireRequested = QtCore.Signal(str)
    sigLoadRequested = QtCore.Signal()
    sigObservationPlaneChanged = QtCore.Signal(object)
    sigDefineObservationPlaneRequested = QtCore.Signal()
    sigLocalizationRunRequested = QtCore.Signal(object)
    sigCalibrationRequested = QtCore.Signal(str,object)
    sigMethodChanged = QtCore.Signal(str)

    _LINEAR_FIELDS = (
        ("period_x_px","Tested period X (px)","100"),
        ("measured_dx_um","Measured displacement X (um)","1.0"),
        ("period_y_px","Tested period Y (px)","100"),
        ("measured_dy_um","Measured displacement Y (um)","1.0"),
    )

    def __init__(
        self,
        *,
        mapping_name: str | None=None,
        section_key: str | None=None,
        section_title: str | None=None,
        observation_planes: Sequence[str]=(),
        current_observation_plane: str | None=None,
        observation_default_pixel_size_um: float | None=None,
        localization_parameters: Mapping[str, Any] | None=None,
        target_context: Mapping[str, Any] | None=None,
        detectors: Sequence[str]=(),
        current_detector: str | None=None,
        title: str="Calibration",
        parent: QtWidgets.QWidget | None=None,
        embedded: bool=False,
    ) -> None:
        super().__init__(parent)
        self._embedded=bool(embedded)
        self.setWindowTitle(str(title))
        if self._embedded:
            self.setWindowFlags(QtCore.Qt.Widget)
        else:
            self.setWindowFlags(
                self.windowFlags()
                | QtCore.Qt.WindowMinimizeButtonHint
                | QtCore.Qt.WindowMaximizeButtonHint
            )
            self.setMinimumSize(900,640)
            self.resize(1180,760)
            self.setAttribute(QtCore.Qt.WA_DeleteOnClose,True)

        self._mapping_name = ""
        self._section_key = str(section_key or "").strip()
        self._section_title = str(section_title or self._section_key).strip()
        self._target_available = False
        self._target_calibration_candidate = None
        self._linear_edits: dict[str, QtWidgets.QLineEdit] = {}

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8,8,8,8)
        layout.setSpacing(8)

        self._mapping_label = QtWidgets.QLabel()
        mapping_font = self._mapping_label.font()
        mapping_font.setBold(True)
        self._mapping_label.setFont(mapping_font)
        layout.addWidget(self._mapping_label)
        self.set_mapping_name(mapping_name)

        method_row = QtWidgets.QHBoxLayout()
        method_row.addWidget(QtWidgets.QLabel("Method:"))
        self._method_combo = QtWidgets.QComboBox()
        self._method_combo.addItem("Linear phase",LINEAR_PHASE_METHOD)
        self._method_combo.addItem(
            "Target localization",TARGET_LOCALIZATION_METHOD,
        )
        self._method_combo.currentIndexChanged.connect(
            self._on_method_changed,
        )
        method_row.addWidget(self._method_combo)
        method_row.addStretch(1)
        layout.addLayout(method_row)

        self._stack = QtWidgets.QStackedWidget()
        self._linear_page = self._build_linear_phase_page()
        self._target_page = self._build_target_localization_page(
            localization_parameters or _default_values(LOCALIZATION_PARAMS),
            target_context,
            observation_planes,
            current_observation_plane,
            observation_default_pixel_size_um,
            detectors,
            current_detector,
        )
        self._stack.addWidget(self._linear_page)
        self._stack.addWidget(self._target_page)
        layout.addWidget(self._stack,1)

        self._button_box = QtWidgets.QDialogButtonBox()
        self._calibration_button = QtWidgets.QPushButton("Save")
        self._button_box.addButton(
            self._calibration_button,
            QtWidgets.QDialogButtonBox.AcceptRole,
        )
        if not self._embedded:
            self._button_box.addButton(QtWidgets.QDialogButtonBox.Cancel)
        self._calibration_button.clicked.connect(
            self._request_calibration,
        )
        self._button_box.rejected.connect(self.reject)
        layout.addWidget(self._button_box)

        self.set_target_available(
            False,
            "Select Target localization to initialize this workflow.",
        )
        self._refresh_calibration_button()

    @property
    def current_method(self) -> str:
        data = self._method_combo.currentData()
        return str(data or LINEAR_PHASE_METHOD)

    @property
    def target_measurement(self):
        return self.target_view.measurement

    @property
    def target_candidate(self):
        return self.target_view.candidate

    @property
    def target_calibration_candidate(self):
        return self._target_calibration_candidate

    @property
    def mapping_name(self) -> str:
        return self._mapping_name

    def set_mapping_name(self,mapping_name: str | None) -> None:
        mapping = str(mapping_name or "").strip()
        self._mapping_name = mapping
        section = self._section_title
        suffix = " / Section %s" % section if section else ""
        self._mapping_label.setText(
            "Calibrating mapping: %s%s" % (mapping,suffix)
            if mapping else "Calibrating mapping: none%s" % suffix
        )

    @property
    def current_observation_plane(self) -> str | None:
        value = self._observation_combo.currentData()
        if value == _DEFINE_OBSERVATION_PLANE:
            return None
        return str(value).strip() if value is not None and str(value).strip() else None

    @property
    def target_pixel_size_um(self) -> float | None:
        text = self._pixel_size_edit.text().strip()
        if not text:
            return None
        value = float(text)
        if value <= 0:
            raise ValueError("Pixel size must be > 0 um.")
        return value

    def clear_target_pixel_size_override(self) -> None:
        self._pixel_size_edit.clear()

    def _on_observation_combo_changed(self,_index: int) -> None:
        value=self._observation_combo.currentData()
        if value == _DEFINE_OBSERVATION_PLANE:
            self.sigDefineObservationPlaneRequested.emit()
            return
        self.sigObservationPlaneChanged.emit(self.current_observation_plane)

    def configure_observation_planes(
        self,
        observation_planes: Sequence[str],
        current_observation_plane: str | None=None,
        default_pixel_size_um: float | None=None,
    ) -> None:
        names = tuple(
            str(value).strip() for value in observation_planes if str(value).strip()
        )
        blocker = QtCore.QSignalBlocker(self._observation_combo)
        try:
            self._observation_combo.clear()
            self._observation_combo.addItem("Select observation plane...",None)
            for name in names:
                self._observation_combo.addItem(name,name)
            self._observation_combo.insertSeparator(self._observation_combo.count())
            self._observation_combo.addItem(
                "Define new observation plane…",_DEFINE_OBSERVATION_PLANE,
            )
            index = self._observation_combo.findData(current_observation_plane)
            if index < 0:
                index = 0
            self._observation_combo.setCurrentIndex(index)
        finally:
            del blocker
        if default_pixel_size_um is None:
            self._pixel_size_edit.setPlaceholderText("Auto / required if unavailable")
        else:
            self._pixel_size_edit.setPlaceholderText(
                "Auto (plane default %.6g)" % float(default_pixel_size_um)
            )


    def set_live_acquisition_available(
        self,available: bool,reason: str="",
    ) -> None:
        self.target_view.set_acquire_available(bool(available),str(reason or ""))

    def configure_detectors(
        self,detectors: Sequence[str],current_detector: str | None=None,
    ) -> None:
        self.target_view.configure_detectors(detectors,current_detector)

    def set_target_available(self,available: bool,text: str="") -> None:
        self._target_available = bool(available)
        self.target_view.set_read_only(not bool(available))
        self.set_target_status(text,error=False)
        self._refresh_calibration_button()

    def set_target_reference(
        self,
        *,
        context: Mapping[str, Any] | None=None,
        parameters: Mapping[str, Any] | None=None,
        status: str="Target localization ready.",
        clear_measurement: bool=False,
    ) -> None:
        if clear_measurement:
            self.target_view.clear_measurement()
            self.clear_target_calibration_candidate()
        self.target_view.set_context(context)
        if parameters is not None:
            self.target_view.set_parameters(parameters,invalidate_candidate=False)
        self.set_target_available(True,status)

    def set_target_reference_error(self,error: Any) -> None:
        self.target_view.clear_measurement()
        self.target_view.set_context(None)
        self.clear_target_calibration_candidate()
        self.set_target_available(False,str(error))
        self.set_target_status(str(error),error=True)

    def set_target_measurement_busy(self,busy: bool,text: str="") -> None:
        if busy:
            self.clear_target_calibration_candidate()
        self.target_view.set_measurement_busy(busy,text)
        if text:
            self.set_target_status(text,error=False)

    def set_target_measurement_error(self,error: Any) -> None:
        self.clear_target_calibration_candidate()
        self.target_view.set_measurement_error(error)
        self.set_target_status(str(error),error=True)

    def set_target_measurement(
        self,
        measurement: Any,
        *,
        parameters: Mapping[str, Any] | None=None,
        context: Mapping[str, Any] | None=None,
    ) -> None:
        if context is not None:
            self.target_view.set_context(context)
        if parameters is not None:
            self.target_view.set_parameters(parameters,invalidate_candidate=False)
        self.clear_target_calibration_candidate()
        self.target_view.set_measurement(measurement)
        self.set_target_status("Measurement ready.",error=False)

    def set_target_localization_result(
        self,result: Any,parameters: Mapping[str, Any] | None=None,
    ) -> None:
        self.clear_target_calibration_candidate()
        self.target_view.set_result(result,parameters)
        self.set_target_status("Localization candidate ready.",error=False)

    def set_target_localization_error(self,error: Any) -> None:
        self.clear_target_calibration_candidate()
        self.target_view.set_error(error)
        self.set_target_status(str(error),error=True)

    def set_target_calibration_candidate(self,candidate: Any) -> None:
        self._target_calibration_candidate = candidate
        self._target_result_label.setText(
            self._format_target_calibration_candidate(candidate)
        )
        self.set_target_status("Calibration candidate ready.",error=False)
        self._refresh_calibration_button()

    def set_target_calibration_candidate_error(self,error: Any) -> None:
        self.clear_target_calibration_candidate(
            "Target calibration candidate unavailable."
        )
        self.set_target_status(str(error),error=True)

    def clear_target_calibration_candidate(
        self,
        text: str="Target calibration result will appear here.",
    ) -> None:
        self._target_calibration_candidate = None
        if hasattr(self,"_target_result_label"):
            self._target_result_label.setText(str(text or ""))
        self._refresh_calibration_button()

    def set_target_status(self,text: str,error: bool=False) -> None:
        self._target_status_label.setText(str(text or ""))
        self._target_status_label.setStyleSheet(
            "color: %s;" % ("#a33" if error else "#888")
        )

    def _build_linear_phase_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0,6,0,0)
        layout.setSpacing(8)

        form = QtWidgets.QFormLayout()
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        for key,text,default in self._LINEAR_FIELDS:
            edit = QtWidgets.QLineEdit(default)
            edit.setValidator(self._signed_float_validator(edit))
            self._linear_edits[key] = edit
            form.addRow(text + ":",edit)
        layout.addLayout(form)

        self._linear_status_label = QtWidgets.QLabel("")
        self._linear_status_label.setStyleSheet("color: #a33;")
        self._linear_status_label.setWordWrap(True)
        layout.addWidget(self._linear_status_label)
        layout.addStretch(1)
        return page

    def _build_target_localization_page(
        self,
        localization_parameters: Mapping[str,Any],
        target_context: Mapping[str, Any] | None,
        observation_planes: Sequence[str],
        current_observation_plane: str | None,
        observation_default_pixel_size_um: float | None,
        detectors: Sequence[str],
        current_detector: str | None,
    ) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0,6,0,0)
        layout.setSpacing(8)

        context_row = QtWidgets.QHBoxLayout()
        context_row.addWidget(QtWidgets.QLabel("Observation plane:"))
        self._observation_combo = QtWidgets.QComboBox()
        self._observation_combo.setMinimumWidth(170)
        self._observation_combo.currentIndexChanged.connect(
            self._on_observation_combo_changed
        )
        context_row.addWidget(self._observation_combo)
        context_row.addSpacing(12)
        context_row.addWidget(QtWidgets.QLabel("Pixel size (um):"))
        self._pixel_size_edit = QtWidgets.QLineEdit()
        validator = QtGui.QDoubleValidator(0.0,1e9,9,self._pixel_size_edit)
        validator.setNotation(QtGui.QDoubleValidator.StandardNotation)
        self._pixel_size_edit.setValidator(validator)
        self._pixel_size_edit.setMaximumWidth(170)
        context_row.addWidget(self._pixel_size_edit)
        context_row.addStretch(1)
        layout.addLayout(context_row)
        self.configure_observation_planes(
            observation_planes,
            current_observation_plane,
            observation_default_pixel_size_um,
        )

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal,page)
        splitter.setChildrenCollapsible(False)

        measurement_group = QtWidgets.QGroupBox("Measurement / Localization")
        measurement_layout = QtWidgets.QVBoxLayout(measurement_group)
        measurement_layout.setContentsMargins(8,10,8,8)
        measurement_layout.setSpacing(7)
        self.target_view = MeasurementLocalizationView(
            parameters=localization_parameters,
            context=target_context,
            detectors=detectors,
            current_detector=current_detector,
            show_metrics=False,
            show_accept=False,
            parent=measurement_group,
        )
        self.target_view.set_detector_selection_enabled(True)
        self.target_view.sigAcquireRequested.connect(
            lambda detector:self.sigAcquireRequested.emit(str(detector))
        )
        self.target_view.sigLoadRequested.connect(self.sigLoadRequested.emit)
        self.target_view.sigRunRequested.connect(
            lambda parameters:self.sigLocalizationRunRequested.emit(
                dict(parameters)
            )
        )
        measurement_layout.addWidget(self.target_view,1)
        splitter.addWidget(measurement_group)

        result_group = QtWidgets.QGroupBox("Calibration result")
        result_layout = QtWidgets.QVBoxLayout(result_group)
        result_layout.setContentsMargins(8,10,8,8)
        result_layout.setSpacing(8)
        self._target_result_label = QtWidgets.QLabel(
            "Target calibration result will appear here."
        )
        self._target_result_label.setWordWrap(True)
        self._target_result_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse
        )
        result_layout.addWidget(self._target_result_label)
        self._target_status_label = QtWidgets.QLabel("")
        self._target_status_label.setWordWrap(True)
        self._target_status_label.setStyleSheet("color: #888;")
        result_layout.addWidget(self._target_status_label)
        result_layout.addStretch(1)
        splitter.addWidget(result_group)
        splitter.setStretchFactor(0,3)
        splitter.setStretchFactor(1,1)
        splitter.setSizes([780,300])

        layout.addWidget(splitter,1)
        return page

    def _on_method_changed(self,*_args: Any) -> None:
        method = self.current_method
        self._stack.setCurrentWidget(
            self._target_page
            if method == TARGET_LOCALIZATION_METHOD else self._linear_page
        )
        if method == TARGET_LOCALIZATION_METHOD:
            self.set_target_available(False,"Preparing target localization...")
        self._refresh_calibration_button()
        self.sigMethodChanged.emit(method)

    def _refresh_calibration_button(self) -> None:
        method = self.current_method
        if method == TARGET_LOCALIZATION_METHOD:
            self._calibration_button.setText("Set calibration")
            enabled = (
                self._target_available
                and self._target_calibration_candidate is not None
            )
            self._calibration_button.setEnabled(enabled)
            self._calibration_button.setToolTip(
                ""
                if enabled else
                "Run localization to compute a target calibration candidate."
            )
        else:
            self._calibration_button.setText("Save")
            self._calibration_button.setEnabled(True)
            self._calibration_button.setToolTip("")

    def _request_calibration(self,*_args: Any) -> None:
        method = self.current_method
        if method == TARGET_LOCALIZATION_METHOD:
            if self._target_calibration_candidate is None:
                self.set_target_status(
                    "Run localization before setting calibration.",
                    error=True,
                )
                return
            self.sigCalibrationRequested.emit(
                method,
                {
                    "mapping_name":self.mapping_name,
                    "candidate":self._target_calibration_candidate,
                },
            )
            if not self._embedded:
                self.accept()
            return
        if method != LINEAR_PHASE_METHOD:
            return
        try:
            values = self._linear_phase_values()
        except ValueError as error:
            self._linear_status_label.setText(str(error))
            return
        values["mapping_name"] = self.mapping_name
        self._linear_status_label.setText("")
        self.sigCalibrationRequested.emit(method,values)
        if not self._embedded:
            self.accept()

    def _linear_phase_values(self) -> dict[str, float]:
        result: dict[str, float] = {}
        for key,text,_default in self._LINEAR_FIELDS:
            value = self._read_float_value(self._linear_edits[key],text)
            result[key] = value
        return result

    @staticmethod
    def _format_target_calibration_candidate(candidate: Any) -> str:
        calibration = getattr(candidate,"calibration",None)
        lines = [
            "Target spacing:",
            "  X: %.6g reference px -> kx = %.6g" % (
                float(getattr(candidate,"target_period_x_reference_px",0.0)),
                float(getattr(candidate,"target_kx",0.0)),
            ),
            "  Y: %.6g reference px -> ky = %.6g" % (
                float(getattr(candidate,"target_period_y_reference_px",0.0)),
                float(getattr(candidate,"target_ky",0.0)),
            ),
            "",
            "Detector scale:",
            "  %.6g um / image pixel" % float(
                getattr(candidate,"detector_pixel_size_um",0.0)
            ),
            "",
            "Measured spacing:",
            "  X: %.6g px = %.6g um" % (
                float(getattr(candidate,"fitted_period_x_px",0.0)),
                float(getattr(candidate,"measured_period_x_um",0.0)),
            ),
            "  Y: %.6g px = %.6g um" % (
                float(getattr(candidate,"fitted_period_y_px",0.0)),
                float(getattr(candidate,"measured_period_y_um",0.0)),
            ),
            "",
            "Calibration result:",
            "  kx_per_um = %.6g" % float(
                getattr(calibration,"kx_per_um",0.0)
            ),
            "  ky_per_um = %.6g" % float(
                getattr(calibration,"ky_per_um",0.0)
            ),
            "",
            "Localization quality:",
            "  matched/expected = %d/%d" % (
                int(getattr(candidate,"matched_count",0)),
                int(getattr(candidate,"expected_count",0)),
            ),
        ]
        rms = getattr(candidate,"rms_residual_px",None)
        if rms is not None:
            lines.append("  RMS residual = %.6g px" % float(rms))
        warnings = tuple(getattr(candidate,"warnings",()) or ())
        if warnings:
            lines.append("")
            lines.append("Warnings:")
            lines.extend("  %s" % warning for warning in warnings)
        return "\n".join(lines)

    @staticmethod
    def _read_float_value(edit: QtWidgets.QLineEdit,label: str) -> float:
        text = edit.text().strip()
        if not text:
            raise ValueError("%s is required." % label)
        try:
            return float(text)
        except Exception as error:
            raise ValueError("%s must be a number." % label) from error

    @staticmethod
    def _signed_float_validator(parent) -> QtGui.QDoubleValidator:
        validator = QtGui.QDoubleValidator(parent)
        validator.setNotation(QtGui.QDoubleValidator.StandardNotation)
        validator.setLocale(QtCore.QLocale(QtCore.QLocale.C))
        return validator


__all__ = [
    "CalibrationDialog",
    "LINEAR_PHASE_METHOD",
    "TARGET_LOCALIZATION_METHOD",
]
