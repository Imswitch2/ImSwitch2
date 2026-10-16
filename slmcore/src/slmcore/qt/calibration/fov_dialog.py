"""Dedicated editable-lattice FOV mapping-calibration dialog."""

from __future__ import annotations

from typing import Any,Mapping,Sequence

import numpy as np
from qtpy import QtCore,QtWidgets

from ..measurement.localization_view import MeasurementLocalizationView
from ..cgh.session_views import PositionCorrectionView,PositionReferenceView


def _transform_label(correction) -> str:
    model=str(getattr(correction,"model","") or "").strip().lower()
    if model=="polynomial":
        degree=getattr(correction,"degree",None)
        if degree==1:
            return "Polynomial 1 (legacy)"
        return "Polynomial %s" % (degree if degree is not None else "—")
    return model.title() or "Transform"


def _populate_transform_models(combo: QtWidgets.QComboBox) -> None:
    combo.addItem("Translation","translation")
    combo.addItem("Rigid","rigid")
    combo.addItem("Affine","affine")
    combo.addItem("Polynomial","polynomial")


def _populate_polynomial_degrees(combo: QtWidgets.QComboBox) -> None:
    combo.addItem("2",2); combo.addItem("3",3); combo.addItem("4",4)


class SavedFOVCorrectionView(QtWidgets.QWidget):
    """Read/modify view for one persisted continuous FOV correction.

    The persisted source samples are retained by ``FOVMappingCorrection`` so a
    saved correction can be inspected and refit without returning to image
    acquisition/localization.  This widget intentionally edits only the fit
    derived from those saved samples.
    """

    sigRefitRequested=QtCore.Signal(str,object)
    sigSaveUpdateRequested=QtCore.Signal()
    sigRenameRequested=QtCore.Signal(str)

    def __init__(self,parent: QtWidgets.QWidget | None=None) -> None:
        super().__init__(parent)
        self._correction=None
        self._preview=None
        self._mutations_enabled=True
        self._mutation_disabled_reason=""
        root=QtWidgets.QVBoxLayout(self); root.setContentsMargins(4,4,4,4); root.setSpacing(6)

        self.summary=QtWidgets.QLabel(); self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        root.addWidget(self.summary)

        controls=QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("Transform:"))
        self.model_combo=QtWidgets.QComboBox(); _populate_transform_models(self.model_combo)
        controls.addWidget(self.model_combo)
        self.degree_label=QtWidgets.QLabel("Order:")
        controls.addWidget(self.degree_label)
        self.degree_combo=QtWidgets.QComboBox(); _populate_polynomial_degrees(self.degree_combo)
        controls.addWidget(self.degree_combo)
        self.model_combo.setToolTip(
            "Refit the retained FOV samples as Translation, Rigid, Affine, or Polynomial."
        )
        self.model_combo.currentIndexChanged.connect(self._refit_options_changed)
        self.degree_combo.currentIndexChanged.connect(self._refit_options_changed)
        self.refit_button=QtWidgets.QPushButton("Refit saved samples")
        self.refit_button.clicked.connect(self._request_refit)
        controls.addWidget(self.refit_button)
        self.save_update_button=QtWidgets.QPushButton("Save update")
        self.save_update_button.clicked.connect(self.sigSaveUpdateRequested.emit)
        self.save_update_button.setEnabled(False)
        controls.addWidget(self.save_update_button)
        self.rename_button=QtWidgets.QPushButton("Rename…")
        self.rename_button.clicked.connect(self._rename)
        controls.addWidget(self.rename_button)
        controls.addStretch(1); root.addLayout(controls)

        self.preview_label=QtWidgets.QLabel("Saved fit")
        self.preview_label.setWordWrap(True); root.addWidget(self.preview_label)

        tabs=QtWidgets.QTabWidget(); root.addWidget(tabs,1)
        self.field_view=PositionCorrectionView(); tabs.addTab(self.field_view,"Transform field")
        self.samples_view=PositionCorrectionView(); tabs.addTab(self.samples_view,"Saved samples")

    @property
    def correction(self):
        return self._correction

    @property
    def preview(self):
        return self._preview

    def set_mutations_enabled(self,enabled: bool,reason: str="") -> None:
        self._mutations_enabled=bool(enabled)
        self._mutation_disabled_reason=str(reason or "")
        for widget in (
            self.model_combo,self.degree_combo,self.refit_button,self.rename_button,
        ):
            widget.setEnabled(self._mutations_enabled)
            widget.setToolTip("" if self._mutations_enabled else self._mutation_disabled_reason)
        self.save_update_button.setEnabled(
            self._mutations_enabled and self._preview is not None
        )
        self.save_update_button.setToolTip(
            "" if self._mutations_enabled else self._mutation_disabled_reason
        )

    def set_correction(self,correction,*,compatibility: str="",is_default: bool=False) -> None:
        self._correction=correction; self._preview=None
        if correction is None:
            self.summary.setText("No saved FOV correction selected.")
            self.field_view.clear(); self.samples_view.clear(); self.save_update_button.setEnabled(False)
            return
        model_index=self.model_combo.findData(str(correction.model))
        if model_index>=0:self.model_combo.setCurrentIndex(model_index)
        if correction.degree is not None:
            degree_index=self.degree_combo.findData(int(correction.degree))
            if degree_index>=0:self.degree_combo.setCurrentIndex(degree_index)
        self._model_changed()
        self.summary.setText(
            "%s · %s · %s%s\n%d samples · RMS %.6g kxy · max %.6g kxy · coverage %.6g\nCreated: %s" % (
                correction.name,_transform_label(correction),compatibility or "Saved",
                " · Default" if is_default else "",
                correction.sample_count,correction.rms_residual_kxy,
                correction.max_residual_kxy,correction.coverage_area,
                correction.created_at or "—",
            )
        )
        self.samples_view.set_data(
            correction.sample_positions_kxy,correction.sample_displacements_kxy
        )
        self._show_field(correction)
        self.preview_label.setText("Saved %s fit." % _transform_label(correction))
        self.save_update_button.setEnabled(False)
        self.set_mutations_enabled(self._mutations_enabled,self._mutation_disabled_reason)

    def set_preview(self,correction) -> None:
        self._preview=correction
        if correction is None:
            if self._correction is not None:self._show_field(self._correction)
            self.preview_label.setText("Saved fit")
            self.save_update_button.setEnabled(False); return
        self._show_field(correction)
        self.preview_label.setText(
            "Preview refit: %s · RMS %.6g kxy · max %.6g kxy" % (
                _transform_label(correction),correction.rms_residual_kxy,
                correction.max_residual_kxy,
            )
        )
        self.save_update_button.setEnabled(self._mutations_enabled)

    def _model_changed(self) -> None:
        polynomial=str(self.model_combo.currentData() or "")=="polynomial"
        self.degree_label.setVisible(polynomial); self.degree_combo.setVisible(polynomial)

    def _refit_options_changed(self,*_args) -> None:
        self._model_changed()
        if self._preview is not None:
            self.set_preview(None)

    def _request_refit(self) -> None:
        model=str(self.model_combo.currentData() or "polynomial")
        degree=int(self.degree_combo.currentData()) if model=="polynomial" else None
        self.sigRefitRequested.emit(model,degree)

    def _show_field(self,correction) -> None:
        samples=np.asarray(correction.sample_positions_kxy,dtype=np.float64)
        if samples.shape[1] < 1:
            self.field_view.clear(); return
        xmin,xmax=float(np.min(samples[0])),float(np.max(samples[0]))
        ymin,ymax=float(np.min(samples[1])),float(np.max(samples[1]))
        xs=np.linspace(xmin,xmax,19) if xmax>xmin else np.array([xmin])
        ys=np.linspace(ymin,ymax,19) if ymax>ymin else np.array([ymin])
        xx,yy=np.meshgrid(xs,ys,indexing="xy")
        positions=np.vstack((xx.reshape(-1),yy.reshape(-1)))
        self.field_view.set_data(positions,correction.evaluate(positions))

    def _rename(self) -> None:
        correction=self._correction
        if correction is None:return
        name,ok=QtWidgets.QInputDialog.getText(
            self,"Rename FOV correction","New name:",text=str(correction.name)
        )
        name=str(name or "").strip()
        if ok and name and name!=correction.name:self.sigRenameRequested.emit(name)


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
    sigFitCandidateRequested=QtCore.Signal(str,object)
    sigSaveRequested=QtCore.Signal(str,bool)
    sigGeometryCalibrationRequested=QtCore.Signal(str)

    _GEOMETRY_FIELDS=(
        ("period_x_px","Period X"),
        ("period_y_px","Period Y"),
        ("rotation_deg","Rotation"),
        ("lattice_angle_deg","Lattice angle"),
        ("offset_x_px","Offset X"),
        ("offset_y_px","Offset Y"),
    )

    def __init__(
        self,*,section_key: str,mapping_name: str,observation_planes: Sequence[str],
        current_observation_plane: str | None,localization_parameters: Mapping[str,Any],
        detectors: Sequence[str]=(),current_detector: str | None=None,
        section_title: str | None=None,
        parent: QtWidgets.QWidget | None=None,
        embedded: bool=False,
    ) -> None:
        super().__init__(parent)
        self._embedded=bool(embedded)
        if self._embedded:
            self.setWindowFlags(QtCore.Qt.Widget)
        self.section_key=str(section_key); self.mapping_name=str(mapping_name)
        self.section_title=str(section_title or self.section_key)
        self.setWindowTitle("FOV Calibration - %s / %s" % (self.mapping_name,self.section_title))
        self.resize(1180,820)

        root=QtWidgets.QVBoxLayout(self); root.setContentsMargins(8,8,8,8); root.setSpacing(6)

        method_row=QtWidgets.QHBoxLayout(); method_row.setSpacing(8)
        method_row.addWidget(QtWidgets.QLabel("Method:"))
        self.method_combo=QtWidgets.QComboBox()
        self.method_combo.addItem("CGH target","cgh_target")
        self.method_combo.addItem("Load calibration","load_calibration")
        method_row.addWidget(self.method_combo)
        method_row.addSpacing(12); method_row.addWidget(QtWidgets.QLabel("Observation:"))
        self.observation_combo=QtWidgets.QComboBox(); method_row.addWidget(self.observation_combo)
        method_row.addWidget(QtWidgets.QLabel("Pixel size override:"))
        self.pixel_size_spin=QtWidgets.QDoubleSpinBox(); self.pixel_size_spin.setRange(0.0,1e6)
        self.pixel_size_spin.setDecimals(6); self.pixel_size_spin.setSpecialValueText("auto")
        self.pixel_size_spin.setSuffix(" µm/px"); self.pixel_size_spin.setValue(0.0)
        method_row.addWidget(self.pixel_size_spin); method_row.addStretch(1)
        root.addLayout(method_row)

        self.geometry_warning=QtWidgets.QFrame(); warning_layout=QtWidgets.QHBoxLayout(self.geometry_warning)
        warning_layout.setContentsMargins(6,4,6,4)
        self.geometry_warning_label=QtWidgets.QLabel(""); self.geometry_warning_label.setWordWrap(True)
        warning_layout.addWidget(self.geometry_warning_label,1)
        self.geometry_calibrate_button=QtWidgets.QPushButton("Calibrate geometry…")
        self.geometry_calibrate_button.clicked.connect(self._request_geometry_calibration)
        warning_layout.addWidget(self.geometry_calibrate_button)
        self.geometry_warning.setVisible(False); root.addWidget(self.geometry_warning)

        self.method_stack=QtWidgets.QStackedWidget(); root.addWidget(self.method_stack,1)

        # CGH-target method: localization stays a cohesive workbench on the left.
        # The equally weighted right side keeps FOV visualization dominant, with
        # the compact editable lattice beside it and fit/save actions below.
        cgh_page=QtWidgets.QWidget(); cgh_layout=QtWidgets.QVBoxLayout(cgh_page)
        cgh_layout.setContentsMargins(0,0,0,0)
        self.workflow_splitter=QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.workflow_splitter.setChildrenCollapsible(False)
        cgh_layout.addWidget(self.workflow_splitter,1)

        self.measurement_view=MeasurementLocalizationView(
            parameters=localization_parameters,detectors=detectors,current_detector=current_detector,
            show_metrics=False,show_infer_missing=False,show_accept=False,parent=cgh_page,
        )
        self.measurement_view.sigAcquireRequested.connect(
            lambda detector:self.sigAcquireRequested.emit(detector,self.pixel_size_um)
        )
        self.measurement_view.sigLoadRequested.connect(
            lambda:self.sigLoadRequested.emit(self.pixel_size_um)
        )
        self.measurement_view.sigRunRequested.connect(self.sigLocalizationRequested.emit)
        self.workflow_splitter.addWidget(self.measurement_view)

        right=QtWidgets.QWidget(); right_layout=QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(4,0,4,0); right_layout.setSpacing(8)

        top_splitter=QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        top_splitter.setChildrenCollapsible(False)
        tabs=QtWidgets.QTabWidget(); tabs.setMinimumSize(300,320)
        self.reference_view=PositionReferenceView(); tabs.addTab(self.reference_view,"Measured vs desired")
        self.correction_view=PositionCorrectionView(); tabs.addTab(self.correction_view,"FOV fit")
        top_splitter.addWidget(tabs)

        geometry_box=QtWidgets.QGroupBox("Desired lattice"); form=QtWidgets.QGridLayout(geometry_box)
        form.setContentsMargins(8,8,8,8); form.setHorizontalSpacing(6); form.setVerticalSpacing(4)
        form.setColumnStretch(1,1)
        self.geometry_spins={}; self._geometry_lock_boxes={}
        for row,(key,label) in enumerate(self._GEOMETRY_FIELDS):
            form.addWidget(QtWidgets.QLabel(label),row,0)
            spin=QtWidgets.QDoubleSpinBox(); spin.setDecimals(5); spin.setRange(-1e6,1e6)
            if key in ("period_x_px","period_y_px"): spin.setRange(1e-6,1e6)
            if key=="lattice_angle_deg": spin.setRange(0.1,179.9)
            spin.setSuffix(" px" if key.endswith("_px") else " °")
            spin.setMinimumWidth(76)
            spin.valueChanged.connect(self._geometry_edited); form.addWidget(spin,row,1)
            self.geometry_spins[key]=spin
            if key in ("period_x_px","period_y_px","rotation_deg","lattice_angle_deg"):
                lock=QtWidgets.QToolButton(); lock.setCheckable(True); lock.setAutoRaise(True)
                lock.setFixedSize(22,22); lock.setText("🔓")
                lock.setToolTip("Lock %s while fitting." % label.lower())
                lock.toggled.connect(lambda checked,k=key:self._geometry_lock_toggled(k,bool(checked)))
                form.addWidget(lock,row,2); self._geometry_lock_boxes[key]=lock
        self.square_button=QtWidgets.QPushButton("Square && straight")
        self.fit_center_button=QtWidgets.QPushButton("Fit center")
        self.reset_fit_button=QtWidgets.QPushButton("Reset from fit")
        self.square_button.clicked.connect(self.sigSquareStraightRequested.emit)
        self.fit_center_button.clicked.connect(self.sigFitCenterRequested.emit)
        self.reset_fit_button.clicked.connect(self.sigResetFromFitRequested.emit)
        action_row=len(self._GEOMETRY_FIELDS)
        form.addWidget(self.square_button,action_row,0,1,3)
        form.addWidget(self.fit_center_button,action_row+1,0,1,3)
        form.addWidget(self.reset_fit_button,action_row+2,0,1,3)
        geometry_box.setMinimumWidth(190); geometry_box.setMaximumWidth(240)
        top_splitter.addWidget(geometry_box)
        top_splitter.setStretchFactor(0,1); top_splitter.setStretchFactor(1,0)
        top_splitter.setSizes([430,210])
        right_layout.addWidget(top_splitter,1)

        fit_box=QtWidgets.QGroupBox("FOV correction"); fit_layout=QtWidgets.QVBoxLayout(fit_box)
        fit_layout.setContentsMargins(8,8,8,8); fit_layout.setSpacing(5)
        fit_row=QtWidgets.QHBoxLayout(); fit_row.setSpacing(6)
        fit_row.addWidget(QtWidgets.QLabel("Transform:"))
        self.model_combo=QtWidgets.QComboBox(); _populate_transform_models(self.model_combo)
        self.model_combo.setCurrentIndex(self.model_combo.findData("polynomial"))
        fit_row.addWidget(self.model_combo)
        self.degree_label=QtWidgets.QLabel("Order:"); fit_row.addWidget(self.degree_label)
        self.degree_combo=QtWidgets.QComboBox(); _populate_polynomial_degrees(self.degree_combo)
        fit_row.addWidget(self.degree_combo); self.fit_button=QtWidgets.QPushButton("Fit candidate")
        self.model_combo.setToolTip(
            "Translation: common shift; Rigid: shift + rotation; Affine: adds scale/shear; "
            "Polynomial: nonlinear field."
        )
        self.degree_combo.setToolTip("Polynomial total order (2, 3, or 4).")
        self.model_combo.currentIndexChanged.connect(self._fit_options_changed)
        self.degree_combo.currentIndexChanged.connect(self._fit_options_changed)
        self.fit_button.clicked.connect(self._emit_fit_candidate)
        fit_row.addWidget(self.fit_button); fit_row.addStretch(1); fit_layout.addLayout(fit_row)
        self.candidate_label=QtWidgets.QLabel("No FOV candidate fitted."); self.candidate_label.setWordWrap(True)
        fit_layout.addWidget(self.candidate_label)
        save_row=QtWidgets.QHBoxLayout(); save_row.setSpacing(6); save_row.addWidget(QtWidgets.QLabel("Save as:"))
        self.name_edit=QtWidgets.QLineEdit(); self.name_edit.setPlaceholderText("FOV correction name")
        save_row.addWidget(self.name_edit,1)
        self.default_check=QtWidgets.QCheckBox("Set as default"); save_row.addWidget(self.default_check)
        self.save_button=QtWidgets.QPushButton("Save correction"); self.save_button.clicked.connect(self._save)
        save_row.addWidget(self.save_button); fit_layout.addLayout(save_row)
        right_layout.addWidget(fit_box,0)
        self.workflow_splitter.addWidget(right)
        self.workflow_splitter.setStretchFactor(0,1); self.workflow_splitter.setStretchFactor(1,1)
        self.workflow_splitter.setSizes([560,560])
        self.method_stack.addWidget(cgh_page)

        load_page=QtWidgets.QWidget(); load_layout=QtWidgets.QVBoxLayout(load_page)
        load_layout.setContentsMargins(18,18,18,18)
        load_title=QtWidgets.QLabel("Load calibration")
        font=load_title.font(); font.setBold(True); load_title.setFont(font)
        load_layout.addWidget(load_title)
        load_help=QtWidgets.QLabel(
            "Importing an existing FOV calibration is not implemented yet. "
            "This is a separate calibration method and will not depend on the CGH-target workflow."
        )
        load_help.setWordWrap(True); load_layout.addWidget(load_help)
        self.load_calibration_button=QtWidgets.QPushButton("Load calibration…")
        self.load_calibration_button.setEnabled(False)
        self.load_calibration_button.setToolTip("FOV calibration import is not implemented yet.")
        load_layout.addWidget(self.load_calibration_button); load_layout.addStretch(1)
        self.method_stack.addWidget(load_page)
        if not self._embedded:
            buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
            buttons.rejected.connect(self.reject); root.addWidget(buttons)
        self.observation_combo.currentIndexChanged.connect(self._observation_changed)
        self.method_combo.currentIndexChanged.connect(self._method_changed)
        self.configure_observation_planes(observation_planes,current_observation_plane)
        self._method_changed(); self._fit_model_changed()
        self.set_editable_enabled(False); self.save_button.setEnabled(False)

    @property
    def calibration_method(self) -> str:
        return str(self.method_combo.currentData() or "cgh_target")

    def _method_changed(self,*_args) -> None:
        self.method_stack.setCurrentIndex(0 if self.calibration_method=="cgh_target" else 1)
        self.geometry_warning.setVisible(
            self.calibration_method=="cgh_target" and bool(self.geometry_warning_label.text())
        )

    @property
    def pixel_size_um(self) -> float | None:
        value=float(self.pixel_size_spin.value()); return None if value<=0 else value

    @property
    def geometry_values(self) -> dict[str,float]:
        return {key:float(spin.value()) for key,spin in self.geometry_spins.items()}

    @property
    def geometry_locks(self) -> dict[str,bool]:
        return {key:bool(box.isChecked()) for key,box in self._geometry_lock_boxes.items()}

    def _fit_model_changed(self) -> None:
        polynomial=str(self.model_combo.currentData() or "")=="polynomial"
        self.degree_label.setVisible(polynomial); self.degree_combo.setVisible(polynomial)

    def _fit_options_changed(self,*_args) -> None:
        self._fit_model_changed()
        # Prevent saving a candidate fitted with options that no longer match
        # the selector.  The service candidate remains harmlessly cached until
        # the next fit; the UI requires an explicit refit before Save.
        if hasattr(self,"candidate_label") and self.save_button.isEnabled():
            self.clear_candidate()

    def _emit_fit_candidate(self) -> None:
        model=str(self.model_combo.currentData() or "polynomial")
        degree=int(self.degree_combo.currentData()) if model=="polynomial" else None
        self.sigFitCandidateRequested.emit(model,degree)

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
            self._set_geometry_lock_icon(key,box.isChecked())
        self.set_editable_enabled(True)

    def set_reference_preview(self,preview: Mapping[str,Any] | None) -> None:
        self.reference_view.set_preview(preview,center_editable=False)

    def set_candidate(self,correction) -> None:
        if correction is None: self.clear_candidate(); return
        self.correction_view.set_data(correction.sample_positions_kxy,correction.sample_displacements_kxy)
        self.candidate_label.setText(
            "%s · %d samples · RMS %.6g kxy · max %.6g kxy · coverage %.6g"
            % (_transform_label(correction),correction.sample_count,
               correction.rms_residual_kxy,correction.max_residual_kxy,
               correction.coverage_area)
        )
        self.save_button.setEnabled(True)

    def clear_candidate(self) -> None:
        self.correction_view.clear(); self.candidate_label.setText("No FOV candidate fitted.")
        self.save_button.setEnabled(False)

    def set_geometry_missing(self,plane_name: str | None,missing: bool) -> None:
        if missing:
            self.geometry_warning_label.setText(
                "Observation geometry is required before FOV localization. "
                "Calibrate this section × observation-plane pair manually or with a CGH target, then retry."
            )
        self.geometry_warning.setVisible(bool(missing) and self.calibration_method=="cgh_target")
        if not missing:
            self.geometry_warning_label.setText("")

    def set_editable_enabled(self,enabled: bool) -> None:
        for spin in self.geometry_spins.values(): spin.setEnabled(bool(enabled))
        for box in self._geometry_lock_boxes.values(): box.setEnabled(bool(enabled))
        for button in (self.square_button,self.fit_center_button,self.reset_fit_button,self.fit_button):
            button.setEnabled(bool(enabled))

    def set_error(self,error: Any) -> None:
        self.measurement_view.set_error(error)

    def _set_geometry_lock_icon(self,key: str,checked: bool) -> None:
        button=self._geometry_lock_boxes.get(key)
        if button is not None:
            button.setText("🔒" if checked else "🔓")

    def _geometry_lock_toggled(self,key: str,checked: bool) -> None:
        self._set_geometry_lock_icon(key,checked)
        self._geometry_edited()

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


__all__=["FOVCalibrationDialog","SavedFOVCorrectionView"]
