from __future__ import annotations

from typing import Any,Mapping,Sequence

from qtpy import QtCore,QtWidgets

from ..measurement.localization_view import MeasurementLocalizationView


class GeometryCalibrationDialog(QtWidgets.QDialog):
    """Reusable section × observation-plane geometry calibration UI."""

    sigObservationPlaneChanged=QtCore.Signal(object)
    sigComputeRequested=QtCore.Signal(object)
    sigAcquireRequested=QtCore.Signal(str)
    sigLoadRequested=QtCore.Signal()
    sigLocalizationRequested=QtCore.Signal(object)
    sigAnalyzeRequested=QtCore.Signal()
    sigSaveRequested=QtCore.Signal()
    sigManualSaveRequested=QtCore.Signal(str)
    sigFinishRequested=QtCore.Signal()

    def __init__(
        self,*,section_key: str,observation_planes: Sequence[str],
        current_observation_plane: str | None,detectors: Sequence[str],
        current_detector: str | None,localization_parameters: Mapping[str,Any] | None=None,
        section_title: str | None=None,
        parent=None,
        embedded: bool=False,
    ) -> None:
        super().__init__(parent)
        self._embedded=bool(embedded)
        if self._embedded:
            self.setWindowFlags(QtCore.Qt.Widget)
        self.section_key=str(section_key)
        self.section_title=str(section_title or self.section_key)
        self.setWindowTitle("Geometry calibration — %s" % self.section_title)
        self.resize(1040,760)
        root=QtWidgets.QVBoxLayout(self)

        context=QtWidgets.QHBoxLayout()
        context.addWidget(QtWidgets.QLabel("Observation plane:"))
        self.observation_combo=QtWidgets.QComboBox(); context.addWidget(self.observation_combo,1)
        root.addLayout(context)

        target=QtWidgets.QGroupBox("Geometry orientation calibration")
        target_layout=QtWidgets.QVBoxLayout(target)
        self.status_label=QtWidgets.QLabel("Status: Not calibrated")
        self.status_label.setWordWrap(True); target_layout.addWidget(self.status_label)
        form=QtWidgets.QGridLayout()
        form.addWidget(QtWidgets.QLabel("Grid X"),0,0)
        self.grid_x=QtWidgets.QSpinBox(); self.grid_x.setRange(3,31); self.grid_x.setValue(5); form.addWidget(self.grid_x,0,1)
        form.addWidget(QtWidgets.QLabel("Grid Y"),0,2)
        self.grid_y=QtWidgets.QSpinBox(); self.grid_y.setRange(3,31); self.grid_y.setValue(4); form.addWidget(self.grid_y,0,3)
        self.period_x_label=QtWidgets.QLabel("Period X (µm)"); form.addWidget(self.period_x_label,1,0)
        self.period_x=QtWidgets.QDoubleSpinBox(); self.period_x.setRange(0.0001,1e6); self.period_x.setDecimals(4); form.addWidget(self.period_x,1,1)
        self.period_y_label=QtWidgets.QLabel("Period Y (µm)"); form.addWidget(self.period_y_label,1,2)
        self.period_y=QtWidgets.QDoubleSpinBox(); self.period_y.setRange(0.0001,1e6); self.period_y.setDecimals(4); form.addWidget(self.period_y,1,3)
        target_layout.addLayout(form)
        buttons=QtWidgets.QHBoxLayout()
        self.compute_button=QtWidgets.QPushButton("Compute calibrated target")
        self.compute_button.clicked.connect(self._compute); buttons.addWidget(self.compute_button)
        self.finish_button=QtWidgets.QPushButton("Finish / restore CGH")
        self.finish_button.clicked.connect(self.sigFinishRequested.emit); buttons.addWidget(self.finish_button)
        buttons.addStretch(1); target_layout.addLayout(buttons)
        self.target_label=QtWidgets.QLabel("Compute a calibration target to begin.")
        self.target_label.setWordWrap(True); target_layout.addWidget(self.target_label)
        root.addWidget(target)

        self.measurement_view=MeasurementLocalizationView(
            parameters=dict(localization_parameters or {}),detectors=detectors,
            current_detector=current_detector,show_metrics=False,
            show_infer_missing=False,show_accept=False,parent=self,
        )
        self.measurement_view.sigAcquireRequested.connect(self.sigAcquireRequested.emit)
        self.measurement_view.sigLoadRequested.connect(self.sigLoadRequested.emit)
        self.measurement_view.sigRunRequested.connect(self.sigLocalizationRequested.emit)
        root.addWidget(self.measurement_view,1)

        analysis=QtWidgets.QGroupBox("Analysis")
        analysis_layout=QtWidgets.QVBoxLayout(analysis)
        self.analysis_label=QtWidgets.QLabel("Acquire/load and localize the calibration target first.")
        self.analysis_label.setWordWrap(True); analysis_layout.addWidget(self.analysis_label)
        row=QtWidgets.QHBoxLayout()
        self.analyze_button=QtWidgets.QPushButton("Analyze")
        self.analyze_button.clicked.connect(self.sigAnalyzeRequested.emit); row.addWidget(self.analyze_button)
        self.save_button=QtWidgets.QPushButton("Validate & Save")
        self.save_button.clicked.connect(self.sigSaveRequested.emit); row.addWidget(self.save_button)
        row.addStretch(1); analysis_layout.addLayout(row); root.addWidget(analysis)

        manual=QtWidgets.QGroupBox("Manual geometry")
        manual_layout=QtWidgets.QHBoxLayout(manual)
        manual_layout.addWidget(QtWidgets.QLabel("Orientation:"))
        self.manual_orientation_combo=QtWidgets.QComboBox()
        for label,value in (
            ("Identity","identity"),
            ("Flip horizontal","flip_horizontal"),
            ("Flip vertical","flip_vertical"),
            ("Rotate 180°","rotate_180"),
            ("Rotate 90° CW","rotate_90_cw"),
            ("Rotate 90° CCW","rotate_90_ccw"),
            ("Transpose","transpose"),
            ("Anti-transpose","anti_transpose"),
        ):
            self.manual_orientation_combo.addItem(label,value)
        manual_layout.addWidget(self.manual_orientation_combo)
        self.manual_save_button=QtWidgets.QPushButton("Save manual geometry")
        manual_layout.addWidget(self.manual_save_button)
        manual_layout.addStretch(1)
        root.addWidget(manual)
        self.manual_save_button.clicked.connect(
            lambda:self.sigManualSaveRequested.emit(
                str(self.manual_orientation_combo.currentData() or "identity")
            )
        )

        if not self._embedded:
            close=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
            close.rejected.connect(self.reject); root.addWidget(close)
        self.observation_combo.currentIndexChanged.connect(self._observation_changed)
        self.configure_observation_planes(observation_planes,current_observation_plane)

    def configure_observation_planes(self,names: Sequence[str],current: str | None) -> None:
        self.observation_combo.blockSignals(True); self.observation_combo.clear()
        self.observation_combo.addItem("Select observation plane…",None)
        for name in names:
            self.observation_combo.addItem(str(name),str(name))
        index=self.observation_combo.findData(current) if current else 0
        self.observation_combo.setCurrentIndex(max(0,index)); self.observation_combo.blockSignals(False)

    def configure_detectors(self,names: Sequence[str],current: str | None) -> None:
        self.measurement_view.configure_detectors(names,current)

    @property
    def observation_plane_name(self) -> str | None:
        value=self.observation_combo.currentData()
        return str(value) if value else None

    def _observation_changed(self,_index: int) -> None:
        self.sigObservationPlaneChanged.emit(self.observation_combo.currentData())

    def _compute(self) -> None:
        metric=bool(self.period_x_label.text().endswith("(µm)"))
        self.sigComputeRequested.emit({
            "grid_x":self.grid_x.value(),"grid_y":self.grid_y.value(),
            "period_x":self.period_x.value(),"period_y":self.period_y.value(),
            "period_unit":"um" if metric else "reference_px",
            "detector":self.measurement_view.current_detector or "",
        })

    def set_context(self,context: Mapping[str,Any]) -> None:
        calibrated=bool(context.get("calibrated")); calibration=context.get("calibration")
        if calibrated and calibration is not None:
            self.status_label.setText("Status: Calibrated (%s)" % calibration.orientation.value)
            index=self.manual_orientation_combo.findData(calibration.orientation.value)
            if index>=0:self.manual_orientation_combo.setCurrentIndex(index)
        elif context.get("plane_name"):
            self.status_label.setText("Status: Not calibrated for %s" % context.get("plane_name"))
        else:
            self.status_label.setText("Status: Select an observation plane")
        self.grid_x.setValue(int(context.get("grid_x",5))); self.grid_y.setValue(int(context.get("grid_y",4)))
        metric=bool(context.get("metric_periods_available",False))
        self.period_x_label.setText("Period X (µm)" if metric else "Period X (reference px)")
        self.period_y_label.setText("Period Y (µm)" if metric else "Period Y (reference px)")
        x=context.get("period_x_um" if metric else "period_x_px")
        y=context.get("period_y_um" if metric else "period_y_px")
        if x is not None:self.period_x.setValue(float(x))
        if y is not None:self.period_y.setValue(float(y))
        computed=bool(context.get("computed")); frame_active=bool(context.get("frame_active"))
        self.finish_button.setEnabled(bool(context.get("any_frame_active")))
        self.target_label.setText(
            "Calibration target is displayed. Acquire/load an image and run localization."
            if frame_active else
            ("Calibration target computed but is not currently displayed; compute it again before acquisition." if computed else "Compute a calibration target to begin.")
        )
        self.measurement_view.set_context(context.get("localization_context") or {})
        params=context.get("localization_parameters") or {}
        if params:self.measurement_view.set_parameters(params,invalidate_candidate=False)
        measurement=context.get("measurement")
        if measurement is None:
            self.measurement_view.clear_measurement(); self.measurement_view.clear_localization_result()
        elif self.measurement_view.measurement is not measurement:
            self.measurement_view.set_measurement(measurement)
        localization=context.get("localization")
        if localization is None:
            self.measurement_view.clear_localization_result()
        else:
            self.measurement_view.set_result(localization,params)
        analysis=context.get("analysis")
        if analysis is None:
            self.analysis_label.setText("Acquire/load and localize the calibration target first." if localization is None else "Localization ready. Click Analyze.")
        else:
            self.analysis_label.setText(
                "Orientation: %s · score %.3g · margin %.3g · matched %d%s" % (
                    analysis.orientation.value,analysis.score,analysis.confidence_margin,
                    analysis.matched_count," · accepted" if analysis.accepted else " · ambiguous",
                )
            )
        self.analyze_button.setEnabled(bool(context.get("analyze_enabled")))
        self.save_button.setEnabled(bool(context.get("save_enabled")))
        self.manual_save_button.setEnabled(bool(context.get("plane_name")))

    def set_error(self,error: Any) -> None:
        self.measurement_view.set_error(error)


__all__=["GeometryCalibrationDialog"]
