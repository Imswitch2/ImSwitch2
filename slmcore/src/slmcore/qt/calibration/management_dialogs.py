"""Management dialogs for persistent mapping and observation calibration state."""

from __future__ import annotations

from typing import Any,Mapping,Sequence

from qtpy import QtCore,QtWidgets

from ...workspace import ObservationPlaneDefinition


class ObservationPlaneEditorDialog(QtWidgets.QDialog):
    """Editor shared by the workspace manager and measurement selectors."""

    def __init__(
        self,
        definition: ObservationPlaneDefinition | None=None,
        *,
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)
        self._existing = definition
        self.setWindowTitle("Observation plane")
        form = QtWidgets.QFormLayout(self)
        self.name_edit = QtWidgets.QLineEdit()
        self.detector_edit = QtWidgets.QLineEdit()
        self.pixel_size_edit = QtWidgets.QDoubleSpinBox()
        self.pixel_size_edit.setRange(0.0,1e9)
        self.pixel_size_edit.setDecimals(9)
        self.pixel_size_edit.setSpecialValueText("Not set")
        self.description_edit = QtWidgets.QPlainTextEdit()
        self.description_edit.setMaximumHeight(80)
        form.addRow("Name",self.name_edit)
        form.addRow("Preferred detector/source",self.detector_edit)
        form.addRow("Default pixel size (µm)",self.pixel_size_edit)
        form.addRow("Description",self.description_edit)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok|QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        if definition is not None:
            self.name_edit.setText(definition.name)
            self.name_edit.setReadOnly(True)
            self.detector_edit.setText(definition.preferred_detector or "")
            self.pixel_size_edit.setValue(
                0.0 if definition.default_pixel_size_um is None
                else float(definition.default_pixel_size_um)
            )
            self.description_edit.setPlainText(definition.description)

    def definition(self) -> ObservationPlaneDefinition:
        name = self.name_edit.text().strip()
        pixel = float(self.pixel_size_edit.value())
        created_at = "" if self._existing is None else self._existing.created_at
        return ObservationPlaneDefinition(
            name=name,
            preferred_detector=self.detector_edit.text().strip() or None,
            default_pixel_size_um=None if pixel <= 0 else pixel,
            description=self.description_edit.toPlainText().strip(),
            created_at=created_at,
        )


class ObservationPlanesManagerDialog(QtWidgets.QDialog):
    sigChanged = QtCore.Signal()

    def __init__(self,service,*,parent: QtWidgets.QWidget | None=None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Observation Planes")
        self.resize(720,360)
        layout = QtWidgets.QVBoxLayout(self)
        self.table = QtWidgets.QTableWidget(0,4,self)
        self.table.setHorizontalHeaderLabels(
            ["Name","Preferred detector/source","Default pixel size (µm)","Description"]
        )
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table,1)
        row = QtWidgets.QHBoxLayout()
        self.add_button = QtWidgets.QPushButton("Add…")
        self.edit_button = QtWidgets.QPushButton("Edit…")
        self.delete_button = QtWidgets.QPushButton("Delete")
        row.addWidget(self.add_button); row.addWidget(self.edit_button); row.addWidget(self.delete_button)
        row.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.close)
        row.addWidget(close)
        layout.addLayout(row)
        self.add_button.clicked.connect(self.add_plane)
        self.edit_button.clicked.connect(self.edit_plane)
        self.delete_button.clicked.connect(self.delete_plane)
        self.table.itemDoubleClicked.connect(lambda _item:self.edit_plane())
        self.refresh()

    def selected_name(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row,0)
        return None if item is None else item.text().strip() or None

    def refresh(self) -> None:
        values = self.service.list()
        self.table.setRowCount(len(values))
        for row,plane in enumerate(values):
            cells = (
                plane.name,
                plane.preferred_detector or "—",
                "—" if plane.default_pixel_size_um is None else "%.9g" % plane.default_pixel_size_um,
                plane.description or "",
            )
            for col,text in enumerate(cells):
                self.table.setItem(row,col,QtWidgets.QTableWidgetItem(str(text)))
        self.table.resizeColumnsToContents()

    def add_plane(self) -> str | None:
        dialog = ObservationPlaneEditorDialog(parent=self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return None
        value = self.service.add(dialog.definition())
        self.refresh(); self.sigChanged.emit()
        return value.name

    def edit_plane(self) -> None:
        name = self.selected_name()
        if not name:
            return
        current = self.service.get(name)
        dialog = ObservationPlaneEditorDialog(current,parent=self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        value = dialog.definition()
        self.service.update(
            name,
            preferred_detector=value.preferred_detector,
            default_pixel_size_um=value.default_pixel_size_um,
            description=value.description,
        )
        self.refresh(); self.sigChanged.emit()

    def delete_plane(self) -> None:
        name = self.selected_name()
        if not name:
            return
        answer = QtWidgets.QMessageBox.question(
            self,"Delete observation plane",
            'Delete observation plane "%s"?\n\nGeometry calibrations are separate resources and are not deleted.' % name,
            QtWidgets.QMessageBox.Yes|QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        self.service.delete(name)
        self.refresh(); self.sigChanged.emit()


class CalibrationManagementDialog(QtWidgets.QDialog):
    """Authoritative management home for mapping calibration and observation geometry."""

    sigContextChanged = QtCore.Signal(str,str)
    sigMappingDeleteRequested = QtCore.Signal(str,str)
    sigBaseCalibrationRequested = QtCore.Signal(str,str)
    sigBaseCalibrationDeleteRequested = QtCore.Signal(str,str)
    sigFovCalibrationRequested = QtCore.Signal(str,str)
    sigFovSetDefaultRequested = QtCore.Signal(str,str,str)
    sigFovInspectRequested = QtCore.Signal(str,str,str)
    sigFovDeleteRequested = QtCore.Signal(str,str,str)
    sigGeometryCalibrationRequested = QtCore.Signal(str,str)
    sigObservationPlanesRequested = QtCore.Signal()

    def __init__(
        self,
        *,
        sections: Sequence[str],
        mappings: Sequence[str],
        current_section: str | None=None,
        current_mapping: str | None=None,
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Calibration Manager")
        self.resize(900,620)
        outer = QtWidgets.QVBoxLayout(self)
        context = QtWidgets.QHBoxLayout()
        context.addWidget(QtWidgets.QLabel("Section:"))
        self.section_combo = QtWidgets.QComboBox(); self.section_combo.addItems([str(v) for v in sections])
        context.addWidget(self.section_combo)
        context.addWidget(QtWidgets.QLabel("Mapping:"))
        self.mapping_combo = QtWidgets.QComboBox(); self.mapping_combo.addItems([str(v) for v in mappings])
        context.addWidget(self.mapping_combo,1)
        self.delete_mapping_button = QtWidgets.QPushButton("Delete mapping…")
        context.addWidget(self.delete_mapping_button)
        self.observation_planes_button = QtWidgets.QPushButton("Observation Planes…")
        context.addWidget(self.observation_planes_button)
        outer.addLayout(context)
        self.tabs = QtWidgets.QTabWidget(self)
        self.tabs.addTab(self._build_mapping_tab(),"Mapping Calibration")
        self.tabs.addTab(self._build_geometry_tab(),"Observation Geometry")
        outer.addWidget(self.tabs,1)
        close = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        close.rejected.connect(self.close); close.clicked.connect(self.close)
        outer.addWidget(close)
        if current_section:
            i=self.section_combo.findText(current_section); self.section_combo.setCurrentIndex(max(0,i))
        if current_mapping:
            i=self.mapping_combo.findText(current_mapping); self.mapping_combo.setCurrentIndex(max(0,i))
        self.section_combo.currentTextChanged.connect(self._context_changed)
        self.mapping_combo.currentTextChanged.connect(self._context_changed)
        self.delete_mapping_button.clicked.connect(
            lambda:self.sigMappingDeleteRequested.emit(self.section_key,self.mapping_name)
        )
        self.observation_planes_button.clicked.connect(self.sigObservationPlanesRequested.emit)

    @property
    def section_key(self) -> str:
        return self.section_combo.currentText().strip()

    @property
    def mapping_name(self) -> str:
        return self.mapping_combo.currentText().strip()

    def set_catalog(self,sections: Sequence[str],mappings: Sequence[str]) -> None:
        section=self.section_key; mapping=self.mapping_name
        for combo,values,current in ((self.section_combo,sections,section),(self.mapping_combo,mappings,mapping)):
            blocker=QtCore.QSignalBlocker(combo); combo.clear(); combo.addItems([str(v) for v in values])
            idx=combo.findText(current); combo.setCurrentIndex(idx if idx>=0 else (0 if combo.count() else -1)); del blocker
        self._context_changed()

    def _build_mapping_tab(self) -> QtWidgets.QWidget:
        widget=QtWidgets.QWidget(); layout=QtWidgets.QVBoxLayout(widget)
        base=QtWidgets.QGroupBox("Base metric calibration"); form=QtWidgets.QFormLayout(base)
        self.base_status=QtWidgets.QLabel("—"); self.base_metrics=QtWidgets.QLabel("—")
        self.base_matrix=QtWidgets.QLabel("—"); self.base_provenance=QtWidgets.QLabel("—"); self.base_provenance.setWordWrap(True)
        form.addRow("Status",self.base_status); form.addRow("kx/µm, ky/µm",self.base_metrics)
        form.addRow("Matrix",self.base_matrix); form.addRow("Provenance",self.base_provenance)
        buttons=QtWidgets.QHBoxLayout(); self.base_calibrate=QtWidgets.QPushButton("Calibrate/Recalibrate…"); self.base_delete=QtWidgets.QPushButton("Delete")
        buttons.addWidget(self.base_calibrate); buttons.addWidget(self.base_delete); buttons.addStretch(1); form.addRow(buttons)
        self.base_calibrate.clicked.connect(lambda:self.sigBaseCalibrationRequested.emit(self.section_key,self.mapping_name))
        self.base_delete.clicked.connect(lambda:self.sigBaseCalibrationDeleteRequested.emit(self.section_key,self.mapping_name))
        layout.addWidget(base)
        fov=QtWidgets.QGroupBox("FOV corrections"); fv=QtWidgets.QVBoxLayout(fov)
        self.fov_default=QtWidgets.QLabel("Default: —"); fv.addWidget(self.fov_default)
        self.fov_table=QtWidgets.QTableWidget(0,4); self.fov_table.setHorizontalHeaderLabels(["Name","Compatibility","Default","Provenance"])
        self.fov_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows); self.fov_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection); self.fov_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers); self.fov_table.horizontalHeader().setStretchLastSection(True)
        fv.addWidget(self.fov_table,1)
        row=QtWidgets.QHBoxLayout(); self.fov_calibrate=QtWidgets.QPushButton("Calibrate new…"); self.fov_default_button=QtWidgets.QPushButton("Set default"); self.fov_inspect=QtWidgets.QPushButton("Inspect"); self.fov_delete=QtWidgets.QPushButton("Delete")
        for b in (self.fov_calibrate,self.fov_default_button,self.fov_inspect,self.fov_delete): row.addWidget(b)
        row.addStretch(1); fv.addLayout(row)
        self.fov_calibrate.clicked.connect(lambda:self.sigFovCalibrationRequested.emit(self.section_key,self.mapping_name))
        self.fov_default_button.clicked.connect(lambda:self._emit_fov(self.sigFovSetDefaultRequested))
        self.fov_inspect.clicked.connect(lambda:self._emit_fov(self.sigFovInspectRequested))
        self.fov_delete.clicked.connect(lambda:self._emit_fov(self.sigFovDeleteRequested))
        layout.addWidget(fov,1)
        return widget

    def _build_geometry_tab(self) -> QtWidgets.QWidget:
        widget=QtWidgets.QWidget(); layout=QtWidgets.QVBoxLayout(widget)
        info=QtWidgets.QLabel("Geometry is scoped by section × observation plane and is independent of mapping calibration."); info.setWordWrap(True); layout.addWidget(info)
        self.geometry_table=QtWidgets.QTableWidget(0,3); self.geometry_table.setHorizontalHeaderLabels(["Observation plane","Status","Calibration"]); self.geometry_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows); self.geometry_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection); self.geometry_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers); self.geometry_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.geometry_table,1)
        row=QtWidgets.QHBoxLayout(); self.geometry_calibrate=QtWidgets.QPushButton("Calibrate…"); row.addWidget(self.geometry_calibrate); row.addStretch(1); layout.addLayout(row)
        self.geometry_calibrate.clicked.connect(self._emit_geometry)
        return widget

    def _context_changed(self,*_args: Any) -> None:
        if self.section_key and self.mapping_name:
            self.sigContextChanged.emit(self.section_key,self.mapping_name)

    def _selected_fov(self) -> str | None:
        row=self.fov_table.currentRow()
        item=None if row<0 else self.fov_table.item(row,0)
        return None if item is None else item.text().strip() or None

    def _emit_fov(self,signal) -> None:
        name=self._selected_fov()
        if name: signal.emit(self.section_key,self.mapping_name,name)

    def _emit_geometry(self) -> None:
        row=self.geometry_table.currentRow(); item=None if row<0 else self.geometry_table.item(row,0)
        plane=None if item is None else item.text().strip() or None
        if plane: self.sigGeometryCalibrationRequested.emit(self.section_key,plane)

    def set_mapping_summary(self,summary: Mapping[str,Any]) -> None:
        calibrated=bool(summary.get("calibrated",False))
        load_error=str(summary.get("load_error") or "").strip()
        if load_error:
            self.base_status.setText("Error")
        else:
            self.base_status.setText("Calibrated" if calibrated else "Not calibrated")
        self.base_metrics.setText(str(summary.get("metrics") or "—"))
        self.base_matrix.setText(str(summary.get("matrix") or "—"))
        self.base_provenance.setText(
            ("Load error: %s" % load_error)
            if load_error else str(summary.get("provenance") or "—")
        )
        self.base_delete.setEnabled(calibrated or bool(load_error))
        default=str(summary.get("default_fov") or "").strip() or None
        self.fov_default.setText("Default: %s" % (default or "—"))
        rows=list(summary.get("fov_rows") or ())
        self.fov_table.setRowCount(len(rows))
        for r,row in enumerate(rows):
            values=(row.get("name",""),row.get("compatibility",""),"Yes" if row.get("default") else "",row.get("provenance",""))
            for c,value in enumerate(values): self.fov_table.setItem(r,c,QtWidgets.QTableWidgetItem(str(value)))
        self.fov_table.resizeColumnsToContents()

    def set_geometry_rows(self,rows: Sequence[Mapping[str,Any]]) -> None:
        self.geometry_table.setRowCount(len(rows))
        for r,row in enumerate(rows):
            values=(row.get("plane",""),row.get("status",""),row.get("details",""))
            for c,value in enumerate(values): self.geometry_table.setItem(r,c,QtWidgets.QTableWidgetItem(str(value)))
        self.geometry_table.resizeColumnsToContents()


__all__ = [
    "CalibrationManagementDialog",
    "ObservationPlaneEditorDialog",
    "ObservationPlanesManagerDialog",
]
