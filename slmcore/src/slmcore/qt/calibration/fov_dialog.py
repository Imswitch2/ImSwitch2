"""Dedicated editable-lattice FOV mapping-calibration dialog."""

from __future__ import annotations

from typing import Any,Mapping,Sequence

from qtpy import QtCore,QtWidgets

from ..measurement.localization_view import MeasurementLocalizationView
from ..cgh.session_views import PositionCorrectionView,PositionReferenceView


class FOVCalibrationDialog(QtWidgets.QDialog):
    """Standalone FOV mapping-calibration workflow.

    Persistent ownership stays in ``MappingService``; this widget only edits one
    transient candidate produced from a measurement/localization correspondence.
    """

    sigObservationPlaneChanged=QtCore.Signal(object)
    sigAcquireRequested=QtCore.Signal(str,object)
    sigLoadRequested=QtCore.Signal(object)
    sigLocalizationRequested=QtCore.Signal(object)
    sigGeometryChanged=QtCore.Signal(object,object)
    sigSquareStraightRequested=QtCore.Signal()
    sigFitCenterRequested=QtCore.Signal()
    sigResetFromFitRequested=QtCore.Signal()
    sigFitCandidateRequested=QtCore.Signal(int)
    sigSaveRequested=QtCore.Signal(str,bool)
    sigGeometryCalibrationRequested=QtCore.Signal(str)

    _GEOMETRY_FIELDS=(
        ("period_x_px","Period X (px)"),
        ("period_y_px","Period Y (px)"),
        ("rotation_deg","Rotation (°)"),
        ("lattice_angle_deg","Lattice angle (°)"),
        ("offset_x_px","Offset X (px)"),
        ("offset_y_px","Offset Y (px)"),
    )

    def __init__(
        self,*,section_key: str,mapping_name: str,observation_planes: Sequence[str],
        current_observation_plane: str | None,localization_parameters: Mapping[str,Any],
        detectors: Sequence[str]=(),current_detector: str | None=None,
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)
        self.section_key=str(section_key); self.mapping_name=str(mapping_name)
        self.setWindowTitle("FOV Calibration - %s / %s" % (self.mapping_name,self.section_key))
        self.resize(1180,820)

        root=QtWidgets.QVBoxLayout(self); root.setContentsMargins(8,8,8,8); root.setSpacing(6)
        context=QtWidgets.QHBoxLayout(); context.setSpacing(8)
        context.addWidget(QtWidgets.QLabel("Mapping: <b>%s</b>" % self.mapping_name))
        context.addWidget(QtWidgets.QLabel("Section: <b>%s</b>" % self.section_key))
        context.addSpacing(12); context.addWidget(QtWidgets.QLabel("Observation:"))
        self.observation_combo=QtWidgets.QComboBox(); context.addWidget(self.observation_combo)
        context.addWidget(QtWidgets.QLabel("Pixel size override:"))
        self.pixel_size_spin=QtWidgets.QDoubleSpinBox(); self.pixel_size_spin.setRange(0.0,1e6)
        self.pixel_size_spin.setDecimals(6); self.pixel_size_spin.setSpecialValueText("auto")
        self.pixel_size_spin.setSuffix(" µm/px"); self.pixel_size_spin.setValue(0.0)
        context.addWidget(self.pixel_size_spin); context.addStretch(1)
        root.addLayout(context)

        self.geometry_warning=QtWidgets.QFrame(); warning_layout=QtWidgets.QHBoxLayout(self.geometry_warning)
        warning_layout.setContentsMargins(6,4,6,4)
        self.geometry_warning_label=QtWidgets.QLabel(""); warning_layout.addWidget(self.geometry_warning_label,1)
        self.geometry_calibrate_button=QtWidgets.QPushButton("Calibrate geometry…")
        self.geometry_calibrate_button.clicked.connect(self._request_geometry_calibration)
        warning_layout.addWidget(self.geometry_calibrate_button)
        self.geometry_warning.setVisible(False); root.addWidget(self.geometry_warning)

        splitter=QtWidgets.QSplitter(QtCore.Qt.Horizontal); root.addWidget(splitter,1)
        left=QtWidgets.QWidget(); left_layout=QtWidgets.QVBoxLayout(left); left_layout.setContentsMargins(0,0,4,0)
        self.measurement_view=MeasurementLocalizationView(
            parameters=localization_parameters,detectors=detectors,current_detector=current_detector,
            show_metrics=False,show_infer_missing=False,show_accept=False,parent=left,
        )
        left_layout.addWidget(self.measurement_view,2)
        self.measurement_view.sigAcquireRequested.connect(
            lambda detector:self.sigAcquireRequested.emit(detector,self.pixel_size_um)
        )
        self.measurement_view.sigLoadRequested.connect(
            lambda:self.sigLoadRequested.emit(self.pixel_size_um)
        )
        self.measurement_view.sigRunRequested.connect(self.sigLocalizationRequested.emit)

        geometry_box=QtWidgets.QGroupBox("Editable desired lattice"); form=QtWidgets.QGridLayout(geometry_box)
        self.geometry_spins={}; self._geometry_lock_boxes={}
        for row,(key,label) in enumerate(self._GEOMETRY_FIELDS):
            form.addWidget(QtWidgets.QLabel(label),row,0)
            spin=QtWidgets.QDoubleSpinBox(); spin.setDecimals(5); spin.setRange(-1e6,1e6)
            if key in ("period_x_px","period_y_px"): spin.setRange(1e-6,1e6)
            if key=="lattice_angle_deg": spin.setRange(0.1,179.9)
            spin.valueChanged.connect(self._geometry_edited); form.addWidget(spin,row,1)
            self.geometry_spins[key]=spin
            if key in ("period_x_px","period_y_px","rotation_deg","lattice_angle_deg"):
                lock=QtWidgets.QCheckBox("Lock"); lock.toggled.connect(self._geometry_edited)
                form.addWidget(lock,row,2); self._geometry_lock_boxes[key]=lock
        actions=QtWidgets.QHBoxLayout()
        self.square_button=QtWidgets.QPushButton("Square && straight")
        self.fit_center_button=QtWidgets.QPushButton("Fit center")
        self.reset_fit_button=QtWidgets.QPushButton("Reset from fit")
        self.square_button.clicked.connect(self.sigSquareStraightRequested.emit)
        self.fit_center_button.clicked.connect(self.sigFitCenterRequested.emit)
        self.reset_fit_button.clicked.connect(self.sigResetFromFitRequested.emit)
        for button in (self.square_button,self.fit_center_button,self.reset_fit_button): actions.addWidget(button)
        actions.addStretch(1); form.addLayout(actions,len(self._GEOMETRY_FIELDS),0,1,3)
        left_layout.addWidget(geometry_box)

        fit_row=QtWidgets.QHBoxLayout(); fit_row.addWidget(QtWidgets.QLabel("Polynomial degree:"))
        self.degree_combo=QtWidgets.QComboBox(); self.degree_combo.addItem("Quadratic (2)",2)
        self.degree_combo.addItem("Linear (1)",1); self.degree_combo.addItem("Cubic (3)",3)
        fit_row.addWidget(self.degree_combo); self.fit_button=QtWidgets.QPushButton("Fit FOV candidate")
        self.fit_button.clicked.connect(lambda:self.sigFitCandidateRequested.emit(int(self.degree_combo.currentData())))
        fit_row.addWidget(self.fit_button); fit_row.addStretch(1); left_layout.addLayout(fit_row)
        splitter.addWidget(left)

        right=QtWidgets.QWidget(); right_layout=QtWidgets.QVBoxLayout(right); right_layout.setContentsMargins(4,0,0,0)
        tabs=QtWidgets.QTabWidget(); right_layout.addWidget(tabs,1)
        self.reference_view=PositionReferenceView(); tabs.addTab(self.reference_view,"Measured vs desired")
        self.correction_view=PositionCorrectionView(); tabs.addTab(self.correction_view,"FOV fit")
        self.candidate_label=QtWidgets.QLabel("No FOV candidate fitted."); self.candidate_label.setWordWrap(True)
        right_layout.addWidget(self.candidate_label)
        save_row=QtWidgets.QHBoxLayout(); save_row.addWidget(QtWidgets.QLabel("Save as:"))
        self.name_edit=QtWidgets.QLineEdit(); self.name_edit.setPlaceholderText("FOV correction name")
        save_row.addWidget(self.name_edit,1); self.default_check=QtWidgets.QCheckBox("Set as default")
        save_row.addWidget(self.default_check); self.save_button=QtWidgets.QPushButton("Save correction")
        self.save_button.clicked.connect(self._save); save_row.addWidget(self.save_button)
        right_layout.addLayout(save_row); splitter.addWidget(right); splitter.setSizes([570,570])

        buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject); root.addWidget(buttons)
        self.observation_combo.currentIndexChanged.connect(self._observation_changed)
        self.configure_observation_planes(observation_planes,current_observation_plane)
        self.set_editable_enabled(False); self.save_button.setEnabled(False)

    @property
    def pixel_size_um(self) -> float | None:
        value=float(self.pixel_size_spin.value()); return None if value<=0 else value

    @property
    def geometry_values(self) -> dict[str,float]:
        return {key:float(spin.value()) for key,spin in self.geometry_spins.items()}

    @property
    def geometry_locks(self) -> dict[str,bool]:
        return {key:bool(box.isChecked()) for key,box in self._geometry_lock_boxes.items()}

    def configure_observation_planes(self,names: Sequence[str],current: str | None) -> None:
        self.observation_combo.blockSignals(True); self.observation_combo.clear()
        self.observation_combo.addItem("Select observation plane…",None)
        for name in names: self.observation_combo.addItem(str(name),str(name))
        index=self.observation_combo.findData(current) if current else 0
        self.observation_combo.setCurrentIndex(max(0,index)); self.observation_combo.blockSignals(False)

    def configure_detectors(self,names: Sequence[str],current: str | None) -> None:
        self.measurement_view.configure_detectors(names,current)

    def set_measurement(self,measurement) -> None:
        self.measurement_view.set_measurement(measurement)

    def clear_measurement(self) -> None:
        self.measurement_view.clear_measurement(); self.measurement_view.clear_localization_result()
        self.reference_view.clear(); self.clear_candidate(); self.set_editable_enabled(False)

    def set_localization(self,result,parameters: Mapping[str,Any]) -> None:
        self.measurement_view.set_result(result,parameters)

    def set_geometry(self,geometry: Mapping[str,Any] | None,locks: Mapping[str,Any] | None=None) -> None:
        if geometry is None:
            self.set_editable_enabled(False); return
        for key,spin in self.geometry_spins.items():
            spin.blockSignals(True); spin.setValue(float(geometry[key])); spin.blockSignals(False)
        for key,box in self._geometry_lock_boxes.items():
            box.blockSignals(True); box.setChecked(bool((locks or {}).get(key,False))); box.blockSignals(False)
        self.set_editable_enabled(True)

    def set_reference_preview(self,preview: Mapping[str,Any] | None) -> None:
        self.reference_view.set_preview(preview,center_editable=False)

    def set_candidate(self,correction) -> None:
        if correction is None: self.clear_candidate(); return
        self.correction_view.set_data(correction.sample_positions_kxy,correction.sample_displacements_kxy)
        self.candidate_label.setText(
            "%d samples · RMS %.6g kxy · max %.6g kxy · coverage %.6g"
            % (correction.sample_count,correction.rms_residual_kxy,
               correction.max_residual_kxy,correction.coverage_area)
        )
        self.save_button.setEnabled(True)

    def clear_candidate(self) -> None:
        self.correction_view.clear(); self.candidate_label.setText("No FOV candidate fitted.")
        self.save_button.setEnabled(False)

    def set_geometry_missing(self,plane_name: str | None,missing: bool) -> None:
        if missing:
            self.geometry_warning_label.setText(
                "Geometry not calibrated for Section %s × Plane %s" % (self.section_key,plane_name or "—")
            )
        self.geometry_warning.setVisible(bool(missing))

    def set_editable_enabled(self,enabled: bool) -> None:
        for spin in self.geometry_spins.values(): spin.setEnabled(bool(enabled))
        for box in self._geometry_lock_boxes.values(): box.setEnabled(bool(enabled))
        for button in (self.square_button,self.fit_center_button,self.reset_fit_button,self.fit_button):
            button.setEnabled(bool(enabled))

    def set_error(self,error: Any) -> None:
        self.measurement_view.set_error(error)

    def _geometry_edited(self,*_args) -> None:
        if not self.geometry_spins["period_x_px"].isEnabled(): return
        self.sigGeometryChanged.emit(self.geometry_values,self.geometry_locks)

    def _observation_changed(self,_index: int) -> None:
        self.sigObservationPlaneChanged.emit(self.observation_combo.currentData())

    def _request_geometry_calibration(self) -> None:
        plane=self.observation_combo.currentData()
        if plane: self.sigGeometryCalibrationRequested.emit(str(plane))

    def _save(self) -> None:
        name=self.name_edit.text().strip()
        if not name:
            QtWidgets.QMessageBox.warning(self,"FOV calibration","Enter a name for the correction.")
            return
        self.sigSaveRequested.emit(name,bool(self.default_check.isChecked()))


__all__=["FOVCalibrationDialog"]
