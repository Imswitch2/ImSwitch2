"""Management dialogs for persistent mapping and observation calibration state."""

from __future__ import annotations

from typing import Any,Mapping,Sequence

from qtpy import QtCore,QtWidgets

from ...workspace import ObservationPlaneDefinition
from .fov_dialog import SavedFOVCorrectionView
from .geometry_calibration_dialog import GeometryOrientationPreview
from .reference_manager import PositionReferenceManagerView


class ObservationPlaneEditorDialog(QtWidgets.QDialog):
    """Editor shared by the workspace manager and measurement selectors."""

    def __init__(
        self,
        definition: ObservationPlaneDefinition | None=None,
        *,
        detectors: Sequence[str]=(),
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)
        self._existing = definition
        self.setWindowTitle("Observation plane")
        form = QtWidgets.QFormLayout(self)
        self.name_edit = QtWidgets.QLineEdit()
        self.detector_combo = QtWidgets.QComboBox()
        self.detector_combo.addItem("None",None)
        for detector in dict.fromkeys(
            str(value).strip() for value in detectors if str(value).strip()
        ):
            self.detector_combo.addItem(detector,detector)
        self.detector_combo.addItem("Other…","__other__")
        self.detector_other_edit = QtWidgets.QLineEdit()
        self.detector_other_edit.setPlaceholderText("Detector/source name")
        self.detector_other_edit.setVisible(False)
        detector_widget = QtWidgets.QWidget()
        detector_layout = QtWidgets.QHBoxLayout(detector_widget)
        detector_layout.setContentsMargins(0,0,0,0)
        detector_layout.addWidget(self.detector_combo,1)
        detector_layout.addWidget(self.detector_other_edit,1)
        self.pixel_size_edit = QtWidgets.QDoubleSpinBox()
        self.pixel_size_edit.setRange(0.0,1e9)
        self.pixel_size_edit.setDecimals(9)
        self.pixel_size_edit.setSpecialValueText("Not set")
        self.description_edit = QtWidgets.QPlainTextEdit()
        self.description_edit.setMaximumHeight(80)
        form.addRow("Name",self.name_edit)
        form.addRow("Preferred detector/source",detector_widget)
        form.addRow("Default pixel size (µm)",self.pixel_size_edit)
        form.addRow("Description",self.description_edit)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok|QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.detector_combo.currentIndexChanged.connect(
            self._detector_selection_changed
        )
        if definition is not None:
            self.name_edit.setText(definition.name)
            self.name_edit.setReadOnly(True)
            preferred=str(definition.preferred_detector or "").strip()
            index=self.detector_combo.findData(preferred) if preferred else 0
            if preferred and index < 0:
                index=self.detector_combo.findData("__other__")
                self.detector_other_edit.setText(preferred)
            self.detector_combo.setCurrentIndex(max(0,index))
            self.pixel_size_edit.setValue(
                0.0 if definition.default_pixel_size_um is None
                else float(definition.default_pixel_size_um)
            )
            self.description_edit.setPlainText(definition.description)
        self._detector_selection_changed()

    def _detector_selection_changed(self,*_args) -> None:
        other=self.detector_combo.currentData()=="__other__"
        self.detector_other_edit.setVisible(bool(other))

    def preferred_detector(self) -> str | None:
        value=self.detector_combo.currentData()
        if value=="__other__":
            return self.detector_other_edit.text().strip() or None
        return str(value).strip() if value else None

    def definition(self) -> ObservationPlaneDefinition:
        name = self.name_edit.text().strip()
        pixel = float(self.pixel_size_edit.value())
        created_at = "" if self._existing is None else self._existing.created_at
        return ObservationPlaneDefinition(
            name=name,
            preferred_detector=self.preferred_detector(),
            default_pixel_size_um=None if pixel <= 0 else pixel,
            description=self.description_edit.toPlainText().strip(),
            created_at=created_at,
        )


class ObservationPlanesManagerDialog(QtWidgets.QDialog):
    sigChanged = QtCore.Signal()

    def __init__(
        self,service,*,detectors: Sequence[str]=(),
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._detectors=tuple(detectors)
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

    def configure_detectors(self,detectors: Sequence[str]) -> None:
        self._detectors=tuple(
            dict.fromkeys(str(value).strip() for value in detectors if str(value).strip())
        )

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
        dialog = ObservationPlaneEditorDialog(detectors=self._detectors,parent=self)
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
        dialog = ObservationPlaneEditorDialog(
            current,detectors=self._detectors,parent=self,
        )
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
            'Delete observation plane "%s"?\n\nOrientation and observation-registration calibrations are separate resources and are not deleted.' % name,
            QtWidgets.QMessageBox.Yes|QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        self.service.delete(name)
        self.refresh(); self.sigChanged.emit()


class CalibrationManagementDialog(QtWidgets.QDialog):
    """Unified calibration/characterization workspace.

    The left column owns selection, persisted state and actions.  The right
    workspace shows either the selected persisted calibration or the active
    calibration workflow.  Workflow widgets are mounted here by the Qt
    application coordinator; no nested calibration windows are required.
    """

    sigContextChanged = QtCore.Signal(str,str)
    sigMappingAddRequested = QtCore.Signal(str)
    sigMappingDeleteRequested = QtCore.Signal(str,str)
    sigBaseCalibrationRequested = QtCore.Signal(str,str)
    sigBaseCalibrationDeleteRequested = QtCore.Signal(str,str)
    sigFovCalibrationRequested = QtCore.Signal(str,str)
    sigFovSetDefaultRequested = QtCore.Signal(str,str,str)
    sigFovDeleteRequested = QtCore.Signal(str,str,str)
    sigFovRefitRequested = QtCore.Signal(str,str,str,str,object)
    sigFovUpdateRequested = QtCore.Signal(str,str,str,object)
    sigFovRenameRequested = QtCore.Signal(str,str,str,str)
    sigSlmChanged = QtCore.Signal(str)
    sigGeometryCalibrationRequested = QtCore.Signal(str,str)
    sigObservationRegistrationRequested = QtCore.Signal(str,str)
    sigObservationRegistrationDeleteRequested = QtCore.Signal(str,str)
    sigObservationSelectionChanged = QtCore.Signal(str)
    sigObservationCreateRequested = QtCore.Signal(object)
    sigObservationUpdateRequested = QtCore.Signal(str,object)
    sigObservationDeleteRequested = QtCore.Signal(str)
    sigWorkspaceResetRequested = QtCore.Signal()

    def __init__(
        self,
        *,
        sections: Sequence[str],
        mappings: Sequence[str],
        section_titles: Mapping[str,str] | None=None,
        current_section: str | None=None,
        current_mapping: str | None=None,
        slms: Sequence[tuple[str,str]] = (),
        current_slm: str | None=None,
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("SLM Workspace")
        self.setWindowFlags(
            self.windowFlags()
            | QtCore.Qt.WindowMinimizeButtonHint
            | QtCore.Qt.WindowMaximizeButtonHint
        )
        self.setMinimumSize(920,620)
        self.resize(1280,820)
        self._section_titles=dict(section_titles or {})
        self._mapping_descriptions: dict[str,str]={}
        self._mapping_summary: dict[str,Any]={}
        self._observation_rows: dict[str,dict[str,Any]]={}
        self._geometry_rows: list[dict[str,Any]]=[]
        self._registration_rows: list[dict[str,Any]]=[]
        self._detectors: tuple[str,...]=()
        self._observation_edit_mode: str | None=None
        self._editing_observation_name: str | None=None
        self._workspace_widget: QtWidgets.QWidget | None=None
        self._inspection_kind="base"
        self._context_switching_enabled=True
        self._context_switching_reason=""
        self._mutations_enabled=True
        self._mutation_disabled_reason=""

        outer=QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8,8,8,8)
        outer.setSpacing(8)

        header=QtWidgets.QHBoxLayout()
        header.addWidget(QtWidgets.QLabel("SLM:"))
        self.slm_combo=QtWidgets.QComboBox()
        for key,label in slms:
            self.slm_combo.addItem(str(label),str(key))
        if current_slm:
            index=self.slm_combo.findData(str(current_slm))
            if index>=0:self.slm_combo.setCurrentIndex(index)
        self.slm_combo.setMinimumWidth(180)
        self.slm_combo.setEnabled(self.slm_combo.count()>1)
        header.addWidget(self.slm_combo)
        header.addSpacing(12)
        header.addWidget(QtWidgets.QLabel("Section:"))
        self.section_combo=QtWidgets.QComboBox()
        self.section_combo.setMinimumWidth(180)
        self._populate_sections(sections,self._section_titles)
        header.addWidget(self.section_combo)
        header.addStretch(1)
        outer.addLayout(header)

        self.tabs=QtWidgets.QTabWidget(self)
        self.tabs.addTab(self._build_mapping_tab(),"Mapping")
        self.tabs.addTab(self._build_observation_tab(),"Observation & Geometry")
        self.reference_view=PositionReferenceManagerView(self)
        self.tabs.addTab(self.reference_view,"References")
        outer.addWidget(self.tabs,1)

        close=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        close.rejected.connect(self.close)
        outer.addWidget(close)

        if current_section:
            i=self.section_combo.findData(current_section)
            if i>=0:self.section_combo.setCurrentIndex(i)
        self.set_mapping_catalog(mappings,current_mapping=current_mapping)
        self.section_combo.currentIndexChanged.connect(self._section_changed)
        self.slm_combo.currentIndexChanged.connect(self._slm_changed)
        self.tabs.currentChanged.connect(self._tab_changed)
        self._apply_context_switching_state()
        self._apply_mutation_state()

    # ------------------------------------------------------------------
    # Basic selection state
    # ------------------------------------------------------------------

    def select_mapping_tab(self) -> None:
        """Show the Mapping page without changing the selected SLM/section."""
        self.tabs.setCurrentIndex(0)

    @property
    def slm_key(self) -> str:
        value=self.slm_combo.currentData()
        return str(value).strip() if value is not None else ""

    def _slm_changed(self,*_args: Any) -> None:
        if self.slm_key:self.sigSlmChanged.emit(self.slm_key)

    def set_slm_catalog(
        self,slms: Sequence[tuple[str,str]],*,current_slm: str | None=None,
    ) -> None:
        previous=str(current_slm or self.slm_key or "").strip()
        blocker=QtCore.QSignalBlocker(self.slm_combo)
        self.slm_combo.clear()
        for key,label in slms:
            self.slm_combo.addItem(str(label),str(key).strip())
        index=self.slm_combo.findData(previous) if previous else -1
        if index<0 and self.slm_combo.count():
            index=0
        self.slm_combo.setCurrentIndex(index)
        del blocker
        self._apply_context_switching_state()

    def select_slm(self,slm_key: str) -> None:
        index=self.slm_combo.findData(str(slm_key).strip())
        if index>=0:self.slm_combo.setCurrentIndex(index)

    def clear_slm_context(self) -> None:
        """Clear per-SLM presentation while retaining workspace-wide resources."""
        self.finish_observation_edit()
        self.set_slm_catalog(())
        self.set_catalog((),(),{}, {})
        self.set_mapping_summary({})
        self.set_observation_catalog(())
        self.set_geometry_rows(())
        self.set_registration_rows(())
        self.show_inspection(
            "Mapping",
            "No SLM session is currently available.",
        )
        self.show_observation_inspection(
            "Observation & Geometry",
            "No SLM session is currently available.",
        )
        self.set_mutations_enabled(False,"No SLM session is currently available.")
        self._apply_context_switching_state()

    @property
    def context_switching_enabled(self) -> bool:
        return bool(self._context_switching_enabled)

    def set_context_switching_enabled(
        self,enabled: bool,reason: str="",
    ) -> None:
        self._context_switching_enabled=bool(enabled)
        self._context_switching_reason=str(reason or "")
        self._apply_context_switching_state()

    def _apply_context_switching_state(self) -> None:
        enabled=bool(self._context_switching_enabled)
        workspace_scope=(hasattr(self,"tabs") and self.tabs.currentIndex()==2)
        selectors_enabled=enabled and not workspace_scope
        self.slm_combo.setEnabled(selectors_enabled and self.slm_combo.count()>1)
        self.section_combo.setEnabled(selectors_enabled and self.section_combo.count()>0)
        if not enabled:
            tooltip=self._context_switching_reason
        elif workspace_scope:
            tooltip="References are workspace-wide and are not filtered by SLM or section."
        else:
            tooltip=""
        self.slm_combo.setToolTip(tooltip)
        self.section_combo.setToolTip(tooltip)

    @property
    def mutations_enabled(self) -> bool:
        return bool(self._mutations_enabled)

    def set_mutations_enabled(self,enabled: bool,reason: str="") -> None:
        self._mutations_enabled=bool(enabled)
        self._mutation_disabled_reason=str(reason or "")
        self._apply_mutation_state()

    def _set_mutation_widget_enabled(self,widget,enabled: bool=True) -> None:
        widget.setEnabled(bool(enabled) and self._mutations_enabled)
        widget.setToolTip("" if self._mutations_enabled else self._mutation_disabled_reason)

    def _apply_mutation_state(self) -> None:
        for widget in (
            self.mapping_add,self.mapping_delete,self.base_calibrate,self.fov_calibrate,
            self.observation_new,self.observation_edit,self.obs_save,
        ):
            self._set_mutation_widget_enabled(widget,True)
        self._set_mutation_widget_enabled(
            self.base_delete,
            self._mapping_summary.get("calibrated",False)
            or bool(self._mapping_summary.get("load_error")),
        )
        self._refresh_fov_actions()
        self.fov_inspector.set_mutations_enabled(
            self._mutations_enabled,self._mutation_disabled_reason,
        )
        self._set_mutation_widget_enabled(
            self.geometry_calibrate,bool(self.geometry_list.currentItem()),
        )
        self._update_registration_actions()
        self._set_mutation_widget_enabled(
            self.observation_delete,
            self._observation_edit_mode != "new" and bool(self.observation_plane_name),
        )
        editing=self._observation_edit_mode is not None
        self.obs_name.setReadOnly(not editing or self._observation_edit_mode != "new" or not self._mutations_enabled)
        self.obs_detector_combo.setEnabled(editing and self._mutations_enabled)
        self.obs_pixel.setEnabled(editing and self._mutations_enabled)
        self.obs_description.setReadOnly(not editing or not self._mutations_enabled)
        self._obs_detector_changed()

    @property
    def section_key(self) -> str:
        value=self.section_combo.currentData()
        return str(value).strip() if value is not None else ""

    @property
    def mapping_name(self) -> str:
        item=self.mapping_list.currentItem()
        if item is None:return ""
        value=item.data(QtCore.Qt.UserRole)
        return str(value or item.text()).strip()

    @property
    def observation_plane_name(self) -> str:
        item=self.observation_list.currentItem()
        if item is None:return ""
        value=item.data(QtCore.Qt.UserRole)
        return str(value or item.text()).strip()

    def _populate_sections(self,sections: Sequence[str],section_titles: Mapping[str,str]) -> None:
        self.section_combo.clear()
        for index,value in enumerate(sections):
            key=str(value)
            title=str(section_titles.get(key) or "Section %d" % (index+1))
            self.section_combo.addItem(title,key)

    def set_catalog(
        self,sections: Sequence[str],mappings: Sequence[str],
        section_titles: Mapping[str,str] | None=None,
        mapping_descriptions: Mapping[str,str] | None=None,
    ) -> None:
        section=self.section_key; mapping=self.mapping_name
        self._section_titles=dict(section_titles or self._section_titles)
        blocker=QtCore.QSignalBlocker(self.section_combo)
        self._populate_sections(sections,self._section_titles)
        idx=self.section_combo.findData(section)
        self.section_combo.setCurrentIndex(idx if idx>=0 else (0 if self.section_combo.count() else -1))
        del blocker
        self.set_mapping_catalog(
            mappings,current_mapping=mapping,
            descriptions=mapping_descriptions,
        )
        self._apply_context_switching_state()
        self._emit_context()

    def set_mapping_catalog(
        self,mappings: Sequence[str],*,current_mapping: str | None=None,
        descriptions: Mapping[str,str] | None=None,
    ) -> None:
        if descriptions is not None:
            self._mapping_descriptions={str(k):str(v or "") for k,v in descriptions.items()}
        current=str(current_mapping or self.mapping_name or "").strip()
        blocker=QtCore.QSignalBlocker(self.mapping_list)
        self.mapping_list.clear()
        for value in mappings:
            name=str(value)
            item=QtWidgets.QListWidgetItem(name)
            item.setData(QtCore.Qt.UserRole,name)
            description=self._mapping_descriptions.get(name,"")
            if description:item.setToolTip(description)
            self.mapping_list.addItem(item)
        if self.mapping_list.count():
            matches=self.mapping_list.findItems(current,QtCore.Qt.MatchExactly) if current else []
            self.mapping_list.setCurrentItem(matches[0] if matches else self.mapping_list.item(0))
        del blocker
        self._mapping_changed()

    def select_mapping(self,name: str | None) -> None:
        target=str(name or "").strip()
        for index in range(self.mapping_list.count()):
            item=self.mapping_list.item(index)
            if str(item.data(QtCore.Qt.UserRole) or item.text())==target:
                self.mapping_list.setCurrentItem(item); return

    def select_section(self,section_key: str) -> None:
        index=self.section_combo.findData(str(section_key))
        if index>=0:self.section_combo.setCurrentIndex(index)

    def _section_changed(self,*_args: Any) -> None:
        self.sigWorkspaceResetRequested.emit()
        self._emit_context()
        if self.tabs.currentIndex()==1 and self.observation_plane_name:
            self.sigObservationSelectionChanged.emit(self.observation_plane_name)

    def _mapping_changed(self,*_args: Any) -> None:
        mapping=self.mapping_name
        description=self._mapping_descriptions.get(mapping,"")
        self.mapping_description.setText(description or "No description.")
        self.mapping_description.setToolTip(description)
        self.sigWorkspaceResetRequested.emit()
        self._inspection_kind="base"
        self._emit_context()
        self._show_base_inspection()

    def _mapping_clicked(self,_item) -> None:
        self.sigWorkspaceResetRequested.emit()
        self._inspection_kind="base"
        self._show_base_inspection()

    def _emit_context(self) -> None:
        if self.section_key and self.mapping_name:
            self.sigContextChanged.emit(self.section_key,self.mapping_name)

    def _tab_changed(self,_index: int) -> None:
        self.sigWorkspaceResetRequested.emit()
        self._apply_context_switching_state()
        index=self.tabs.currentIndex()
        if index==0:
            self._show_current_mapping_inspection()
        elif index==1:
            plane=self.observation_plane_name
            if plane:self.sigObservationSelectionChanged.emit(plane)
            self._show_observation_inspection()

    def set_reference_records(self,rows: Sequence[Mapping[str,Any]]) -> None:
        self.reference_view.set_records(rows)

    def set_reference_mutations_enabled(self,enabled: bool,reason: str="") -> None:
        self.reference_view.set_mutations_enabled(enabled,reason)

    def closeEvent(self,event) -> None:
        if not self._context_switching_enabled:
            QtWidgets.QMessageBox.information(
                self,
                "SLM Workspace",
                (self._context_switching_reason + "\n\nFinish or cancel it before closing the SLM Workspace.")
                if self._context_switching_reason
                else "Finish or cancel the active calibration workflow before closing the SLM Workspace.",
            )
            event.ignore()
            return
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Mapping tab / persisted-state selection
    # ------------------------------------------------------------------

    def _build_mapping_tab(self) -> QtWidgets.QWidget:
        page=QtWidgets.QWidget()
        root=QtWidgets.QHBoxLayout(page); root.setContentsMargins(0,6,0,0)
        splitter=QtWidgets.QSplitter(QtCore.Qt.Horizontal); splitter.setChildrenCollapsible(False)
        root.addWidget(splitter,1)

        left=QtWidgets.QWidget(); left.setMinimumWidth(250); left.setMaximumWidth(360)
        lv=QtWidgets.QVBoxLayout(left); lv.setContentsMargins(0,0,6,0); lv.setSpacing(7)

        mappings_box=QtWidgets.QGroupBox("Mappings")
        ml=QtWidgets.QVBoxLayout(mappings_box)
        self.mapping_list=QtWidgets.QListWidget(); self.mapping_list.setMaximumHeight(145)
        self.mapping_list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        ml.addWidget(self.mapping_list)
        self.mapping_description=QtWidgets.QLabel("No description."); self.mapping_description.setWordWrap(True)
        self.mapping_description.setStyleSheet("color: #777;")
        self.mapping_description.setMaximumHeight(48); ml.addWidget(self.mapping_description)
        row=QtWidgets.QHBoxLayout()
        self.mapping_add=QtWidgets.QPushButton("+ New")
        self.mapping_delete=QtWidgets.QPushButton("Delete")
        row.addWidget(self.mapping_add); row.addWidget(self.mapping_delete); row.addStretch(1); ml.addLayout(row)
        lv.addWidget(mappings_box)

        base=QtWidgets.QGroupBox("Base calibration")
        bf=QtWidgets.QFormLayout(base)
        self.base_status=QtWidgets.QLabel("—")
        self.base_metrics=QtWidgets.QLabel("—")
        self.base_matrix=QtWidgets.QLabel("—"); self.base_matrix.setWordWrap(True)
        self.base_provenance=QtWidgets.QLabel("—"); self.base_provenance.setWordWrap(True)
        bf.addRow("Status",self.base_status)
        bf.addRow("kx/µm, ky/µm",self.base_metrics)
        bf.addRow("Matrix",self.base_matrix)
        bf.addRow("Provenance",self.base_provenance)
        row=QtWidgets.QHBoxLayout()
        self.base_calibrate=QtWidgets.QPushButton("Calibrate / Recalibrate")
        self.base_delete=QtWidgets.QPushButton("Delete")
        row.addWidget(self.base_calibrate); row.addWidget(self.base_delete); row.addStretch(1)
        bf.addRow(row)
        lv.addWidget(base)

        fov=QtWidgets.QGroupBox("FOV corrections")
        fv=QtWidgets.QVBoxLayout(fov)
        self.fov_default=QtWidgets.QLabel("Default: —"); fv.addWidget(self.fov_default)
        self.fov_list=QtWidgets.QListWidget(); self.fov_list.setMinimumHeight(90)
        self.fov_list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        fv.addWidget(self.fov_list,1)
        row=QtWidgets.QHBoxLayout()
        self.fov_calibrate=QtWidgets.QPushButton("+ Calibrate new")
        self.fov_default_button=QtWidgets.QPushButton("Set default")
        self.fov_delete=QtWidgets.QPushButton("Delete")
        row.addWidget(self.fov_calibrate); row.addWidget(self.fov_default_button); row.addWidget(self.fov_delete)
        fv.addLayout(row)
        lv.addWidget(fov,1)
        splitter.addWidget(left)
        splitter.addWidget(self._build_workspace())
        splitter.setSizes([300,980])
        self.fov_inspector=SavedFOVCorrectionView()
        self.fov_inspector.sigRefitRequested.connect(self._request_fov_refit)
        self.fov_inspector.sigSaveUpdateRequested.connect(self._request_fov_update)
        self.fov_inspector.sigRenameRequested.connect(self._request_fov_rename)

        self.mapping_list.currentItemChanged.connect(lambda *_args:self._mapping_changed())
        self.mapping_list.itemClicked.connect(self._mapping_clicked)
        self.mapping_add.clicked.connect(lambda:self.sigMappingAddRequested.emit(self.section_key))
        self.mapping_delete.clicked.connect(
            lambda:self.sigMappingDeleteRequested.emit(self.section_key,self.mapping_name)
        )
        self.base_calibrate.clicked.connect(
            lambda:self.sigBaseCalibrationRequested.emit(self.section_key,self.mapping_name)
        )
        self.base_delete.clicked.connect(
            lambda:self.sigBaseCalibrationDeleteRequested.emit(self.section_key,self.mapping_name)
        )
        self.fov_calibrate.clicked.connect(
            lambda:self.sigFovCalibrationRequested.emit(self.section_key,self.mapping_name)
        )
        self.fov_default_button.clicked.connect(lambda:self._emit_fov(self.sigFovSetDefaultRequested))
        self.fov_delete.clicked.connect(lambda:self._emit_fov(self.sigFovDeleteRequested))
        self.fov_list.currentItemChanged.connect(lambda *_args:self._fov_selected())
        return page

    def set_mapping_summary(self,summary: Mapping[str,Any]) -> None:
        self._mapping_summary=dict(summary)
        calibrated=bool(summary.get("calibrated",False))
        load_error=str(summary.get("load_error") or "").strip()
        self.base_status.setText("Error" if load_error else ("Calibrated" if calibrated else "Not calibrated"))
        self.base_metrics.setText(str(summary.get("metrics") or "—"))
        self.base_matrix.setText(str(summary.get("matrix") or "—"))
        self.base_provenance.setText(
            "Load error: %s" % load_error if load_error else str(summary.get("provenance") or "—")
        )
        self._set_mutation_widget_enabled(self.base_delete,calibrated or bool(load_error))
        default=str(summary.get("default_fov") or "").strip() or None
        self.fov_default.setText("Default: %s" % (default or "—"))
        selected=self._selected_fov()
        blocker=QtCore.QSignalBlocker(self.fov_list)
        self.fov_list.clear()
        for raw in list(summary.get("fov_rows") or ()):
            row=dict(raw)
            name=str(row.get("name") or "")
            suffix=[]
            if row.get("default"):suffix.append("default")
            if str(row.get("compatibility") or "").lower()!="valid":suffix.append("incompatible")
            text=name + ("  ·  " + ", ".join(suffix) if suffix else "")
            item=QtWidgets.QListWidgetItem(text)
            item.setData(QtCore.Qt.UserRole,name)
            item.setData(QtCore.Qt.UserRole+1,row)
            item.setToolTip(str(row.get("provenance") or ""))
            self.fov_list.addItem(item)
        if selected:
            for i in range(self.fov_list.count()):
                item=self.fov_list.item(i)
                if str(item.data(QtCore.Qt.UserRole))==selected:
                    self.fov_list.setCurrentItem(item); break
        del blocker
        self._refresh_fov_actions()
        if self._inspection_kind=="base" and self.workspace_stack.currentWidget() is self.inspection_page:
            self._show_base_inspection()
        elif self._inspection_kind=="fov" and (
            self.workspace_stack.currentWidget() is self.inspection_page
            or self._workspace_widget is self.fov_inspector
        ):
            self._show_fov_inspection()

    def _selected_fov(self) -> str | None:
        item=self.fov_list.currentItem()
        if item is None:return None
        return str(item.data(QtCore.Qt.UserRole) or "").strip() or None

    def _emit_fov(self,signal) -> None:
        name=self._selected_fov()
        if name:signal.emit(self.section_key,self.mapping_name,name)

    def _fov_selected(self) -> None:
        self._refresh_fov_actions()
        if not self._selected_fov():return
        self.sigWorkspaceResetRequested.emit()
        self._inspection_kind="fov"
        self._show_fov_inspection()

    def _refresh_fov_actions(self) -> None:
        selected=bool(self._selected_fov())
        self._set_mutation_widget_enabled(self.fov_default_button,selected)
        self._set_mutation_widget_enabled(self.fov_delete,selected)

    def _show_current_mapping_inspection(self) -> None:
        if self._inspection_kind=="fov" and self._selected_fov():self._show_fov_inspection()
        else:self._show_base_inspection()

    def _show_base_inspection(self) -> None:
        summary=self._mapping_summary
        mapping=self.mapping_name or "Mapping"
        description=self._mapping_descriptions.get(mapping,"")
        lines=[]
        if description:lines.append(description+"\n")
        lines.extend([
            "Base calibration",
            "Status: %s" % self.base_status.text(),
            "kx/µm, ky/µm: %s" % self.base_metrics.text(),
            "Matrix: %s" % self.base_matrix.text(),
            "Provenance: %s" % self.base_provenance.text(),
        ])
        self.show_inspection(mapping,"\n".join(lines))

    def _show_fov_inspection(self) -> None:
        item=self.fov_list.currentItem()
        if item is None:
            self._inspection_kind="base"; self._show_base_inspection(); return
        row=dict(item.data(QtCore.Qt.UserRole+1) or {})
        correction=row.get("correction")
        if correction is None:
            self.show_inspection(
                "%s / %s" % (self.mapping_name,row.get("name") or "FOV correction"),
                "The saved FOV correction could not be loaded.",
            )
            return
        self.fov_inspector.set_correction(
            correction,compatibility=str(row.get("compatibility") or ""),
            is_default=bool(row.get("default")),
        )
        self.show_detail_widget(
            self.fov_inspector,"%s / %s" % (self.mapping_name,correction.name)
        )

    def _request_fov_refit(self,model: str,degree) -> None:
        name=self._selected_fov()
        if name:
            self.sigFovRefitRequested.emit(
                self.section_key,self.mapping_name,name,str(model),degree
            )

    def set_fov_refit_preview(self,correction) -> None:
        self.fov_inspector.set_preview(correction)

    def _request_fov_update(self) -> None:
        name=self._selected_fov(); preview=self.fov_inspector.preview
        if name and preview is not None:
            self.sigFovUpdateRequested.emit(
                self.section_key,self.mapping_name,name,preview
            )

    def _request_fov_rename(self,new_name: str) -> None:
        name=self._selected_fov()
        if name:
            self.sigFovRenameRequested.emit(
                self.section_key,self.mapping_name,name,str(new_name)
            )

    def select_fov(self,name: str | None) -> None:
        target=str(name or "").strip()
        for index in range(self.fov_list.count()):
            item=self.fov_list.item(index)
            if str(item.data(QtCore.Qt.UserRole) or "")==target:
                self.fov_list.setCurrentItem(item); return

    # ------------------------------------------------------------------
    # Observation + geometry tab
    # ------------------------------------------------------------------

    def _build_observation_tab(self) -> QtWidgets.QWidget:
        page=QtWidgets.QWidget(); root=QtWidgets.QHBoxLayout(page); root.setContentsMargins(0,6,0,0)
        splitter=QtWidgets.QSplitter(QtCore.Qt.Horizontal); splitter.setChildrenCollapsible(False); root.addWidget(splitter,1)
        left=QtWidgets.QWidget(); left.setMinimumWidth(270); left.setMaximumWidth(380)
        lv=QtWidgets.QVBoxLayout(left); lv.setContentsMargins(0,0,6,0); lv.setSpacing(7)

        planes=QtWidgets.QGroupBox("Observation planes"); pv=QtWidgets.QVBoxLayout(planes)
        self.observation_list=QtWidgets.QListWidget(); self.observation_list.setMaximumHeight(145)
        pv.addWidget(self.observation_list)
        row=QtWidgets.QHBoxLayout()
        self.observation_new=QtWidgets.QPushButton("+ New")
        self.observation_edit=QtWidgets.QPushButton("Edit")
        self.observation_edit.setVisible(False)
        self.observation_delete=QtWidgets.QPushButton("Delete")
        row.addWidget(self.observation_new); row.addWidget(self.observation_delete); row.addStretch(1)
        pv.addLayout(row); lv.addWidget(planes)

        geometry=QtWidgets.QGroupBox("Orientation by section"); gv=QtWidgets.QVBoxLayout(geometry)
        self.geometry_list=QtWidgets.QListWidget(); self.geometry_list.setMinimumHeight(90)
        gv.addWidget(self.geometry_list,1)
        self.geometry_calibrate=QtWidgets.QPushButton("Calibrate / Recalibrate")
        gv.addWidget(self.geometry_calibrate)
        lv.addWidget(geometry,1)

        registration=QtWidgets.QGroupBox("Registration by section"); rv=QtWidgets.QVBoxLayout(registration)
        self.registration_list=QtWidgets.QListWidget(); self.registration_list.setMinimumHeight(90)
        rv.addWidget(self.registration_list,1)
        registration_buttons=QtWidgets.QHBoxLayout()
        self.registration_calibrate=QtWidgets.QPushButton("Register / Recalibrate")
        self.registration_delete=QtWidgets.QPushButton("Delete")
        registration_buttons.addWidget(self.registration_calibrate); registration_buttons.addWidget(self.registration_delete)
        rv.addLayout(registration_buttons)
        lv.addWidget(registration,1)

        details=QtWidgets.QGroupBox("Observation plane details"); df=QtWidgets.QFormLayout(details)
        self.obs_name=QtWidgets.QLineEdit()
        self.obs_detector_combo=QtWidgets.QComboBox()
        self.obs_detector_other=QtWidgets.QLineEdit(); self.obs_detector_other.setPlaceholderText("Detector/source name")
        detector_widget=QtWidgets.QWidget(); dh=QtWidgets.QHBoxLayout(detector_widget); dh.setContentsMargins(0,0,0,0)
        dh.addWidget(self.obs_detector_combo,1); dh.addWidget(self.obs_detector_other,1)
        self.obs_pixel=QtWidgets.QDoubleSpinBox(); self.obs_pixel.setRange(0.0,1e9); self.obs_pixel.setDecimals(9); self.obs_pixel.setSpecialValueText("Not set")
        self.obs_description=QtWidgets.QPlainTextEdit(); self.obs_description.setMaximumHeight(70)
        df.addRow("Name",self.obs_name); df.addRow("Preferred detector",detector_widget)
        df.addRow("Default pixel size (µm)",self.obs_pixel); df.addRow("Description",self.obs_description)
        buttons=QtWidgets.QHBoxLayout()
        self.obs_save=QtWidgets.QPushButton("Save")
        self.obs_cancel=QtWidgets.QPushButton("Cancel")
        buttons.addWidget(self.obs_save); buttons.addWidget(self.obs_cancel); buttons.addStretch(1); df.addRow(buttons)
        lv.addWidget(details)
        splitter.addWidget(left)

        self.observation_workspace=self._build_workspace(separate=True)
        splitter.addWidget(self.observation_workspace); splitter.setSizes([320,960])

        self.observation_list.currentItemChanged.connect(lambda *_args:self._observation_selected())
        self.observation_new.clicked.connect(self._new_observation)
        self.observation_edit.clicked.connect(self._edit_observation)
        self.observation_delete.clicked.connect(self._delete_observation)
        self.obs_save.clicked.connect(self._save_observation)
        self.obs_cancel.clicked.connect(self._cancel_observation_edit)
        self.obs_detector_combo.currentIndexChanged.connect(self._obs_detector_changed)
        self.geometry_list.currentItemChanged.connect(lambda *_args:self._geometry_selected())
        self.geometry_calibrate.clicked.connect(self._emit_geometry)
        self.registration_list.currentItemChanged.connect(lambda *_args:self._registration_selected())
        self.registration_calibrate.clicked.connect(self._emit_registration)
        self.registration_delete.clicked.connect(self._emit_registration_delete)
        self._set_observation_editing(False)
        return page

    def set_observation_catalog(
        self,planes: Sequence[Mapping[str,Any]],*,detectors: Sequence[str]=(),
        current: str | None=None,
    ) -> None:
        previous=str(current or self.observation_plane_name or "").strip()
        self._detectors=tuple(dict.fromkeys(str(v).strip() for v in detectors if str(v).strip()))
        self._observation_rows={str(row.get("name")):dict(row) for row in planes if str(row.get("name") or "").strip()}
        blocker=QtCore.QSignalBlocker(self.observation_list)
        self.observation_list.clear()
        for name,row in self._observation_rows.items():
            item=QtWidgets.QListWidgetItem(name); item.setData(QtCore.Qt.UserRole,name)
            description=str(row.get("description") or "")
            if description:item.setToolTip(description)
            self.observation_list.addItem(item)
        if self.observation_list.count():
            target=None
            for i in range(self.observation_list.count()):
                item=self.observation_list.item(i)
                if str(item.data(QtCore.Qt.UserRole))==previous:target=item; break
            self.observation_list.setCurrentItem(target or self.observation_list.item(0))
        del blocker
        self._configure_detector_editor(None)
        self._observation_selected()

    def _observation_selected(self) -> None:
        if self._observation_edit_mode=="new":return
        self.sigWorkspaceResetRequested.emit()
        plane=self.observation_plane_name
        self._load_observation_details(plane)
        self.geometry_list.clear(); self.registration_list.clear()
        if plane:self.sigObservationSelectionChanged.emit(plane)
        self._show_observation_inspection()

    def set_geometry_rows(self,rows: Sequence[Mapping[str,Any]]) -> None:
        self._geometry_rows=[dict(row) for row in rows]
        selected=None
        current=self.geometry_list.currentItem()
        if current is not None:selected=str(current.data(QtCore.Qt.UserRole) or "")
        blocker=QtCore.QSignalBlocker(self.geometry_list); self.geometry_list.clear()
        for row in self._geometry_rows:
            section=str(row.get("section") or "")
            title=str(row.get("section_title") or section)
            status=str(row.get("status") or "Not calibrated")
            orientation=str(row.get("orientation") or "—")
            text="%s   ·   %s" % (title,orientation if status=="Calibrated" else status)
            item=QtWidgets.QListWidgetItem(text); item.setData(QtCore.Qt.UserRole,section); item.setData(QtCore.Qt.UserRole+1,row)
            self.geometry_list.addItem(item)
            if section==selected:self.geometry_list.setCurrentItem(item)
        del blocker
        self._set_mutation_widget_enabled(self.geometry_calibrate,bool(self.geometry_list.currentItem()))

    def _geometry_selected(self) -> None:
        self._set_mutation_widget_enabled(self.geometry_calibrate,bool(self.geometry_list.currentItem()))
        item=self.geometry_list.currentItem()
        if item is None:return
        self.sigWorkspaceResetRequested.emit()
        row=dict(item.data(QtCore.Qt.UserRole+1) or {})
        section=str(row.get("section") or "")
        if section:self.select_section(section)
        fields=(
            ("Status",row.get("status")),("Orientation",row.get("orientation")),
            ("Method",row.get("method")),("Created",row.get("created_at")),
            ("Details",row.get("details")),
        )
        body="\n".join("%s: %s" % (label,value) for label,value in fields if value not in (None,""))
        title="%s / %s" % (self.observation_plane_name,row.get("section_title") or section)
        if str(row.get("status") or "") == "Calibrated" and row.get("orientation"):
            inspection=QtWidgets.QWidget()
            layout=QtWidgets.QVBoxLayout(inspection); layout.setContentsMargins(0,0,0,0); layout.setSpacing(8)
            text=QtWidgets.QLabel(body); text.setWordWrap(True)
            text.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            layout.addWidget(text)
            preview=GeometryOrientationPreview(inspection)
            preview.setMinimumSize(420,300)
            preview.set_orientation(str(row.get("orientation")))
            layout.addWidget(preview,1)
            self.show_detail_widget(inspection,title,observation=True)
        else:
            self.show_observation_inspection(title,body)

    def _emit_geometry(self) -> None:
        item=self.geometry_list.currentItem(); plane=self.observation_plane_name
        if item is None or not plane:return
        section=str(item.data(QtCore.Qt.UserRole) or "")
        if section:self.sigGeometryCalibrationRequested.emit(section,plane)

    def set_registration_rows(self,rows: Sequence[Mapping[str,Any]]) -> None:
        self._registration_rows=[dict(row) for row in rows]
        selected=None
        current=self.registration_list.currentItem()
        if current is not None:selected=str(current.data(QtCore.Qt.UserRole) or "")
        blocker=QtCore.QSignalBlocker(self.registration_list); self.registration_list.clear()
        for row in self._registration_rows:
            section=str(row.get("section") or "")
            title=str(row.get("section_title") or section)
            status=str(row.get("status") or "Not registered")
            canonical=str(row.get("canonical_title") or row.get("canonical_section") or "")
            if status=="Registered" and canonical:text="%s   ·   Registered → %s" % (title,canonical)
            elif status=="Canonical":text="%s   ·   Canonical" % title
            else:text="%s   ·   %s" % (title,status)
            item=QtWidgets.QListWidgetItem(text); item.setData(QtCore.Qt.UserRole,section); item.setData(QtCore.Qt.UserRole+1,row)
            if row.get("details"):item.setToolTip(str(row.get("details")))
            self.registration_list.addItem(item)
            if section==selected:self.registration_list.setCurrentItem(item)
        del blocker
        self._update_registration_actions()

    def _registration_selected(self) -> None:
        self._update_registration_actions()
        item=self.registration_list.currentItem()
        if item is None:return
        self.sigWorkspaceResetRequested.emit()
        row=dict(item.data(QtCore.Qt.UserRole+1) or {})
        section=str(row.get("section") or "")
        if section:self.select_section(section)
        fields=(
            ("Status",row.get("status")),("Canonical frame",row.get("canonical_title") or row.get("canonical_section")),
            ("Detector",row.get("detector")),("Image shape",row.get("image_shape")),
            ("Pixel size (µm)",row.get("pixel_size_um")),("Model",row.get("model")),
            ("Points",row.get("point_count")),("Forward RMS (px)",row.get("forward_rms")),
            ("Reverse RMS (px)",row.get("reverse_rms")),("Created",row.get("created_at")),
            ("Provenance",row.get("provenance")),
        )
        body="\n".join("%s: %s" % (label,value) for label,value in fields if value not in (None,""))
        title="%s / %s — Registration" % (self.observation_plane_name,row.get("section_title") or section)
        self.show_observation_inspection(title,body or "No registration saved for this section.")

    def _update_registration_actions(self) -> None:
        item=self.registration_list.currentItem()
        row={} if item is None else dict(item.data(QtCore.Qt.UserRole+1) or {})
        status=str(row.get("status") or "")
        self.registration_calibrate.setText(
            "Inspect / Refit" if status=="Registered" else "Register"
        )
        self._set_mutation_widget_enabled(
            self.registration_calibrate,
            item is not None and self.registration_list.count()>1 and status!="Canonical",
        )
        self._set_mutation_widget_enabled(
            self.registration_delete,status=="Registered",
        )

    def _emit_registration(self) -> None:
        item=self.registration_list.currentItem(); plane=self.observation_plane_name
        if item is None or not plane:return
        row=dict(item.data(QtCore.Qt.UserRole+1) or {})
        if str(row.get("status") or "")=="Canonical":return
        section=str(item.data(QtCore.Qt.UserRole) or "")
        if section:self.sigObservationRegistrationRequested.emit(section,plane)

    def _emit_registration_delete(self) -> None:
        item=self.registration_list.currentItem(); plane=self.observation_plane_name
        if item is None or not plane:return
        section=str(item.data(QtCore.Qt.UserRole) or "")
        if section:self.sigObservationRegistrationDeleteRequested.emit(section,plane)

    def _load_observation_details(self,plane: str) -> None:
        row=self._observation_rows.get(str(plane),{})
        self.obs_name.setText(str(row.get("name") or ""))
        self.obs_pixel.setValue(float(row.get("default_pixel_size_um") or 0.0))
        self.obs_description.setPlainText(str(row.get("description") or ""))
        self._configure_detector_editor(row.get("preferred_detector"))
        if plane:
            self._observation_edit_mode="edit"
            self._editing_observation_name=str(plane)
            self._set_observation_editing(True,new=False)
        else:
            self._observation_edit_mode=None
            self._editing_observation_name=None
            self._set_observation_editing(False)

    def _configure_detector_editor(self,current) -> None:
        value=str(current or "").strip()
        blocker=QtCore.QSignalBlocker(self.obs_detector_combo); self.obs_detector_combo.clear()
        self.obs_detector_combo.addItem("None",None)
        for detector in self._detectors:self.obs_detector_combo.addItem(detector,detector)
        self.obs_detector_combo.addItem("Other…","__other__")
        idx=self.obs_detector_combo.findData(value) if value else 0
        if value and idx<0:
            idx=self.obs_detector_combo.findData("__other__"); self.obs_detector_other.setText(value)
        else:self.obs_detector_other.clear()
        self.obs_detector_combo.setCurrentIndex(max(0,idx)); del blocker
        self._obs_detector_changed()

    def _obs_detector_changed(self,*_args) -> None:
        other=self.obs_detector_combo.currentData()=="__other__"
        self.obs_detector_other.setVisible(bool(other))
        self.obs_detector_other.setEnabled(
            bool(other) and self._observation_edit_mode is not None and self._mutations_enabled
        )

    def _preferred_detector_value(self) -> str | None:
        value=self.obs_detector_combo.currentData()
        if value=="__other__":return self.obs_detector_other.text().strip() or None
        return str(value).strip() if value else None

    def _set_observation_editing(self,editing: bool,new: bool=False) -> None:
        self.obs_name.setReadOnly(not editing or not new)
        self.obs_detector_combo.setEnabled(editing and self._mutations_enabled)
        self.obs_pixel.setEnabled(editing and self._mutations_enabled)
        self.obs_description.setReadOnly(not editing or not self._mutations_enabled)
        self.obs_save.setVisible(editing); self.obs_cancel.setVisible(editing)
        self.obs_save.setText("Create" if new else "Save changes")
        # Existing planes stay directly selectable while their metadata fields
        # remain editable.  New-plane creation is the only modal selection state.
        self.observation_list.setEnabled(not new)
        self._set_mutation_widget_enabled(self.observation_new,not new)
        self._set_mutation_widget_enabled(self.observation_edit,False)
        self._set_mutation_widget_enabled(
            self.observation_delete,not new and bool(self.observation_plane_name),
        )
        self._set_mutation_widget_enabled(self.obs_save,editing)
        self._obs_detector_changed()

    def _new_observation(self) -> None:
        self.sigWorkspaceResetRequested.emit(); self._observation_edit_mode="new"; self._editing_observation_name=None
        self.obs_name.clear(); self.obs_pixel.setValue(0.0); self.obs_description.clear(); self._configure_detector_editor(None)
        self._set_observation_editing(True,new=True)
        self.show_observation_inspection("New observation plane","Fill the observation-plane fields on the left, then Save.")

    def _edit_observation(self) -> None:
        plane=self.observation_plane_name
        if not plane:return
        self._observation_edit_mode="edit"; self._editing_observation_name=plane
        self._set_observation_editing(True,new=False)

    def _cancel_observation_edit(self) -> None:
        self._observation_edit_mode=None; self._editing_observation_name=None
        self._load_observation_details(self.observation_plane_name)
        self._show_observation_inspection()

    def _save_observation(self) -> None:
        payload={
            "name":self.obs_name.text().strip(),
            "preferred_detector":self._preferred_detector_value(),
            "default_pixel_size_um":None if self.obs_pixel.value()<=0 else float(self.obs_pixel.value()),
            "description":self.obs_description.toPlainText().strip(),
        }
        if self._observation_edit_mode=="new":self.sigObservationCreateRequested.emit(payload)
        elif self._observation_edit_mode=="edit" and self._editing_observation_name:
            self.sigObservationUpdateRequested.emit(self._editing_observation_name,payload)

    def finish_observation_edit(self) -> None:
        self._observation_edit_mode=None; self._editing_observation_name=None

    def _delete_observation(self) -> None:
        plane=self.observation_plane_name
        if plane:self.sigObservationDeleteRequested.emit(plane)

    def _show_observation_inspection(self) -> None:
        plane=self.observation_plane_name
        row=self._observation_rows.get(plane,{})
        if not plane:
            self.show_observation_inspection("Observation & Geometry","Select or create an observation plane."); return
        lines=[]
        if row.get("description"):lines.append(str(row["description"])+"\n")
        lines.extend([
            "Preferred detector: %s" % (row.get("preferred_detector") or "—"),
            "Default pixel size: %s" % ("—" if row.get("default_pixel_size_um") is None else "%g µm/px" % row["default_pixel_size_um"]),
            "\nUse Orientation by section for discrete lattice orientation and Registration by section for continuous detector-coordinate registration.",
        ])
        self.show_observation_inspection(plane,"\n".join(lines))

    # ------------------------------------------------------------------
    # Shared dynamic right workspaces
    # ------------------------------------------------------------------

    def _build_workspace(self,separate: bool=False) -> QtWidgets.QWidget:
        container=QtWidgets.QWidget(); layout=QtWidgets.QVBoxLayout(container); layout.setContentsMargins(6,0,0,0)
        header=QtWidgets.QHBoxLayout(); title=QtWidgets.QLabel("Calibration")
        font=title.font(); font.setPointSize(max(font.pointSize()+2,11)); font.setBold(True); title.setFont(font)
        header.addWidget(title,1)
        back=QtWidgets.QPushButton("Back to details"); back.setVisible(False); header.addWidget(back)
        layout.addLayout(header)
        stack=QtWidgets.QStackedWidget(); layout.addWidget(stack,1)
        inspection=QtWidgets.QWidget(); iv=QtWidgets.QVBoxLayout(inspection)
        text=QtWidgets.QLabel(); text.setAlignment(QtCore.Qt.AlignTop|QtCore.Qt.AlignLeft); text.setWordWrap(True)
        text.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse); iv.addWidget(text); iv.addStretch(1)
        host=QtWidgets.QWidget(); hv=QtWidgets.QVBoxLayout(host); hv.setContentsMargins(0,0,0,0)
        stack.addWidget(inspection); stack.addWidget(host)
        if not separate:
            self.workspace_title=title; self.workspace_back=back; self.workspace_stack=stack
            self.inspection_page=inspection; self.inspection_text=text; self.workspace_host=host; self.workspace_host_layout=hv
            back.clicked.connect(self._mapping_back)
        else:
            self.obs_workspace_title=title; self.obs_workspace_back=back; self.obs_workspace_stack=stack
            self.obs_inspection_page=inspection; self.obs_inspection_text=text; self.obs_workspace_host=host; self.obs_workspace_host_layout=hv
            back.clicked.connect(self._observation_back)
        return container

    def show_inspection(self,title: str,body: str) -> None:
        self._clear_workspace_widget()
        self.workspace_title.setText(str(title)); self.inspection_text.setText(str(body)); self.workspace_back.setVisible(False)
        self.workspace_stack.setCurrentWidget(self.inspection_page)

    def show_observation_inspection(self,title: str,body: str) -> None:
        self._clear_workspace_widget(observation=True)
        self.obs_workspace_title.setText(str(title)); self.obs_inspection_text.setText(str(body)); self.obs_workspace_back.setVisible(False)
        self.obs_workspace_stack.setCurrentWidget(self.obs_inspection_page)

    def show_detail_widget(self,widget: QtWidgets.QWidget,title: str,*,observation: bool=False) -> None:
        self._clear_workspace_widget(observation=observation,delete=False)
        host=self.obs_workspace_host if observation else self.workspace_host
        layout=self.obs_workspace_host_layout if observation else self.workspace_host_layout
        widget.setParent(host); widget.setVisible(True); layout.addWidget(widget)
        if observation:
            self.obs_workspace_title.setText(str(title)); self.obs_workspace_back.setVisible(False)
            self.obs_workspace_stack.setCurrentWidget(self.obs_workspace_host)
        else:
            self.workspace_title.setText(str(title)); self.workspace_back.setVisible(False)
            self.workspace_stack.setCurrentWidget(self.workspace_host)
        self._workspace_widget=widget

    def mount_workspace(self,widget: QtWidgets.QWidget,title: str,*,observation: bool=False) -> None:
        self._clear_workspace_widget(observation=observation,delete=False)
        widget.setParent(self.obs_workspace_host if observation else self.workspace_host)
        widget.setVisible(True)
        if observation:
            self.obs_workspace_host_layout.addWidget(widget); self.obs_workspace_title.setText(str(title)); self.obs_workspace_back.setVisible(True)
            self.obs_workspace_stack.setCurrentWidget(self.obs_workspace_host)
        else:
            self.workspace_host_layout.addWidget(widget); self.workspace_title.setText(str(title)); self.workspace_back.setVisible(True)
            self.workspace_stack.setCurrentWidget(self.workspace_host)
        self._workspace_widget=widget

    def _clear_workspace_widget(self,*,observation: bool=False,delete: bool=False) -> None:
        layout=self.obs_workspace_host_layout if observation else self.workspace_host_layout
        mounted=self._workspace_widget
        removed_mounted=False
        while layout.count():
            item=layout.takeAt(0); widget=item.widget()
            if widget is not None:
                if widget is mounted:
                    removed_mounted=True
                widget.setVisible(False); widget.setParent(None)
                if delete:widget.deleteLater()
        if removed_mounted:
            self._workspace_widget=None
        elif mounted is not None:
            # Embedded workflows are owned by their calibration managers and may
            # be destroyed before the management workspace refreshes.  In that
            # case Qt leaves us with a Python wrapper whose C++ object is gone;
            # querying parent() raises RuntimeError rather than returning None.
            try:
                detached=mounted.parent() is None
            except RuntimeError:
                detached=True
            if detached:
                self._workspace_widget=None

    def _mapping_back(self) -> None:
        self.sigWorkspaceResetRequested.emit(); self._show_current_mapping_inspection()

    def _observation_back(self) -> None:
        self.sigWorkspaceResetRequested.emit(); self._show_observation_inspection()



__all__ = [
    "CalibrationManagementDialog",
    "ObservationPlaneEditorDialog",
    "ObservationPlanesManagerDialog",
]
