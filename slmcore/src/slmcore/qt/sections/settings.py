"""Reusable SLM-level settings dialog for section topology and layout info."""

from __future__ import annotations

from dataclasses import dataclass,replace
from typing import Mapping

from qtpy import QtCore,QtGui,QtWidgets

from ...core.engine.section.geometry import (
    SectionSplitLayout,create_split_section_geometries,split_layout_signature,
)
from ...core.engine.section.presentation import SectionPresentation
from ...core.engine.section.snapshot import SLMSectionSnapshot
from ...core.engine.device import SLMGeometry
from ...core.engine.state.topology import GroupTopology
from ..application.interaction import RuntimeViewInteractionSettings
from .display import SectionsDisplayMode
from .topology import SectionTopologyEditor


@dataclass(frozen=True)
class SLMSectionsSettingsChanges:
    """Per-section settings changes split by persistence domain."""

    topologies: Mapping[str,Mapping[str,GroupTopology]]
    presentations: Mapping[str,SectionPresentation]
    layout: SectionSplitLayout | None = None
    display_mode: SectionsDisplayMode | None = None
    interaction_settings: RuntimeViewInteractionSettings | None = None

    def __bool__(self) -> bool:
        return bool(
            self.topologies
            or self.presentations
            or self.layout
            or self.display_mode is not None
            or self.interaction_settings is not None
        )


class SLMSectionsSettingsDialog(QtWidgets.QDialog):
    """Edit section presentation/topology and optional split layout policy."""

    def __init__(
        self,
        *,
        section_snapshots: Mapping[str,SLMSectionSnapshot],
        section_titles: Mapping[str, str] | None=None,
        section_layout_customizable: bool=False,
        display_mode: SectionsDisplayMode | None=None,
        interaction_settings: RuntimeViewInteractionSettings | None=None,
        title: str="SLM settings",
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)

        snapshots = dict(section_snapshots)
        if not snapshots:
            raise ValueError("SLM settings require at least one section")

        self._section_snapshots = snapshots
        self._section_titles = {
            key:str((section_titles or {}).get(key) or f"Section {index + 1}")
            for index,key in enumerate(snapshots)
        }
        self._editors: dict[str, SectionTopologyEditor] = {}
        self._presentation_controls: dict[str, QtWidgets.QCheckBox] = {}
        self._layout_customizable = bool(section_layout_customizable)
        self._display_mode_initial = (
            None
            if display_mode is None
            else SectionsDisplayMode.normalize(display_mode)
        )
        self._interaction_settings_initial = interaction_settings
        self._interaction_spinboxes: dict[str, QtWidgets.QSpinBox] = {}
        self._display_mode_combo = None
        self._layout_axis_combo = None
        self._layout_mode_combo = None
        self._layout_size_edits: dict[str, QtWidgets.QLineEdit] = {}
        self._layout_range_labels: dict[str, QtWidgets.QLabel] = {}

        self.setWindowTitle(title)
        self.resize(620,620)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10,10,10,10)
        layout.setSpacing(10)

        if self._interaction_settings_initial is not None:
            layout.addWidget(self._create_interaction_group())
        if self._display_mode_initial is not None:
            layout.addWidget(self._create_display_group())
        layout.addWidget(self._create_layout_group())

        description = QtWidgets.QLabel(
            "Choose which interface controls, groups and items are present "
            "in each section."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        self.section_tabs = QtWidgets.QTabWidget()
        for section_key,snapshot in snapshots.items():
            editor = SectionTopologyEditor(snapshot=snapshot)
            self._editors[section_key] = editor

            page = QtWidgets.QWidget()
            page_layout = QtWidgets.QVBoxLayout(page)
            page_layout.setContentsMargins(6,6,6,6)
            page_layout.setSpacing(8)
            page_layout.addWidget(
                self._create_interface_group(section_key,snapshot),
            )

            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            scroll.setWidget(editor)
            page_layout.addWidget(scroll,1)

            self.section_tabs.addTab(
                page,self._section_titles[section_key],
            )
        layout.addWidget(self.section_tabs,1)

        warning = QtWidgets.QLabel(
            "Disabling a group or removing an item discards its current "
            "values. Adding it again later recreates it from registry "
            "defaults."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #8a6d3b;")
        layout.addWidget(warning)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok
            | QtWidgets.QDialogButtonBox.Cancel,
        )
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Apply")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _create_interaction_group(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("Interaction settings")
        layout = QtWidgets.QFormLayout(box)
        layout.setContentsMargins(10,8,10,8)
        layout.setSpacing(6)

        note = QtWidgets.QLabel("Applies to all SLMs")
        note.setStyleSheet("color: #666;")
        layout.addRow(note)

        settings = self._interaction_settings_initial
        labels = (
            ("standard_patch_debounce_ms","Standard parameter debounce:"),
            ("target_patch_debounce_ms","CGH target debounce:"),
        )
        for name,label in labels:
            spin = QtWidgets.QSpinBox()
            spin.setRange(500 if name == "target_patch_debounce_ms" else 0,10000)
            spin.setSuffix(" ms")
            spin.setSingleStep(50)
            spin.setValue(int(getattr(settings,name)))
            spin.setToolTip(
                "Global SLM interaction timing. Target debounce is kept at "
                "500 ms or longer to avoid accidental automatic recomputes."
                if name == "target_patch_debounce_ms" else
                "Global SLM interaction timing. 0 ms commits immediately."
            )
            self._interaction_spinboxes[name] = spin
            layout.addRow(label,spin)
        return box

    def changed_interaction_settings(
        self,
    ) -> RuntimeViewInteractionSettings | None:
        initial = self._interaction_settings_initial
        if initial is None:
            return None
        current = RuntimeViewInteractionSettings(**{
            name:spin.value()
            for name,spin in self._interaction_spinboxes.items()
        })
        return None if current == initial else current

    def _create_interface_group(
        self,section_key: str,snapshot: SLMSectionSnapshot,
    ) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("Interface")
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(10,8,10,8)
        layout.setSpacing(6)

        checkbox = QtWidgets.QCheckBox("Calibration / physical units")
        checkbox.setChecked(
            snapshot.presentation.show_calibration_interface,
        )
        self._presentation_controls[section_key] = checkbox
        layout.addWidget(checkbox)

        return box

    def _create_display_group(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("Display")
        layout = QtWidgets.QHBoxLayout(box)
        layout.setContentsMargins(10,8,10,8)
        layout.setSpacing(6)

        combo = QtWidgets.QComboBox()
        combo.addItem("Tabs",SectionsDisplayMode.TABS.value)
        combo.addItem("Side by side",SectionsDisplayMode.HORIZONTAL.value)
        if self._display_mode_initial is not None:
            index = combo.findData(self._display_mode_initial.value)
            combo.setCurrentIndex(max(0,index))
        self._display_mode_combo = combo

        layout.addWidget(QtWidgets.QLabel("Section view:"))
        layout.addWidget(combo)
        layout.addStretch(1)
        return box

    def _create_layout_group(self) -> QtWidgets.QGroupBox:
        title = (
            "Section layout"
            if self._layout_customizable else "Section layout (read only)"
        )
        box = QtWidgets.QGroupBox(title)
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(10,8,10,8)
        layout.setSpacing(6)

        geometries = [
            snapshot.geometry for snapshot in self._section_snapshots.values()
        ]
        width = max(geometry.x + geometry.width for geometry in geometries)
        height = max(geometry.y + geometry.height for geometry in geometries)
        self._layout_geometry = SLMGeometry(
            width=width,height=height,pixel_size_um=1.0,
        )
        self._layout_initial_signature = split_layout_signature(
            self._layout_geometry,
            {
                key:snapshot.geometry
                for key,snapshot in self._section_snapshots.items()
            },
        )

        if not self._layout_customizable:
            note = QtWidgets.QLabel(
                "Section layout is fixed by the SLM definition. Change the "
                "definition options to edit it."
            )
            note.setWordWrap(True)
            layout.addWidget(note)

        summary = QtWidgets.QLabel(
            f"SLM extent: {width} x {height} px    "
            f"Sections: {len(geometries)}"
        )
        layout.addWidget(summary)

        if self._layout_customizable:
            layout.addLayout(self._create_layout_controls())
        else:
            layout.addLayout(self._create_readonly_layout_summary())

        return box

    def _create_readonly_layout_summary(self) -> QtWidgets.QLayout:
        outer = QtWidgets.QVBoxLayout()
        signature = self._layout_initial_signature
        outer.addWidget(
            QtWidgets.QLabel(f"Split direction: {signature.axis.upper()}")
        )

        form = QtWidgets.QFormLayout()
        offset = 0
        for index,section_key in enumerate(signature.section_keys):
            size = signature.sizes[index]
            range_label = (
                f"{signature.axis.upper()} {offset}-{offset + size - 1}"
            )
            form.addRow(
                f"{self._section_titles[section_key]}:",
                QtWidgets.QLabel(f"{size} px    {range_label}"),
            )
            offset += size
        outer.addLayout(form)
        return outer

    def _create_layout_controls(self) -> QtWidgets.QLayout:
        outer = QtWidgets.QVBoxLayout()

        row = QtWidgets.QHBoxLayout()
        axis_combo = QtWidgets.QComboBox()
        axis_combo.addItem("X","x")
        axis_combo.addItem("Y","y")
        mode_combo = QtWidgets.QComboBox()
        mode_combo.addItem("Even","even")
        mode_combo.addItem("Manual","manual")

        signature = self._layout_initial_signature
        axis_index = axis_combo.findData(signature.axis)
        axis_combo.setCurrentIndex(max(0,axis_index))
        initial_mode = (
            "even"
            if signature.sizes == self._even_layout_sizes(signature.axis)
            else "manual"
        )
        mode_combo.setCurrentIndex(mode_combo.findData(initial_mode))

        self._layout_axis_combo = axis_combo
        self._layout_mode_combo = mode_combo

        row.addWidget(QtWidgets.QLabel("Split axis:"))
        row.addWidget(axis_combo)
        row.addSpacing(12)
        row.addWidget(QtWidgets.QLabel("Mode:"))
        row.addWidget(mode_combo)
        row.addStretch(1)
        outer.addLayout(row)

        form = QtWidgets.QFormLayout()
        validator = QtGui.QIntValidator(1,999999,form)
        auto_section_key = signature.section_keys[-1]
        for index,section_key in enumerate(signature.section_keys):
            edit = QtWidgets.QLineEdit(str(signature.sizes[index]))
            edit.setValidator(validator)
            edit.setAlignment(QtCore.Qt.AlignRight)
            edit.setFixedWidth(80)
            range_label = QtWidgets.QLabel()
            range_label.setStyleSheet("color: #666;")
            row_widget = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0,0,0,0)
            row_layout.setSpacing(6)
            row_layout.addWidget(edit)
            row_layout.addWidget(QtWidgets.QLabel("px"))
            row_layout.addWidget(range_label,1)
            if section_key == auto_section_key:
                edit.setToolTip("Automatically filled from remaining pixels.")
            else:
                edit.textChanged.connect(self._update_layout_remainder)
            self._layout_size_edits[section_key] = edit
            self._layout_range_labels[section_key] = range_label
            form.addRow(f"{self._section_titles[section_key]}:",row_widget)
        outer.addLayout(form)

        axis_combo.currentIndexChanged.connect(self._on_layout_axis_changed)
        mode_combo.currentIndexChanged.connect(self._on_layout_mode_changed)
        self._sync_layout_controls()
        return outer

    def _on_layout_axis_changed(self,*_ignored) -> None:
        if self._layout_axis_combo is None:
            return
        self._set_layout_sizes(
            self._even_layout_sizes(str(self._layout_axis_combo.currentData()))
        )
        self._sync_layout_controls()

    def _on_layout_mode_changed(self,*_ignored) -> None:
        if (
            self._layout_axis_combo is not None
            and self._layout_mode_combo is not None
            and str(self._layout_mode_combo.currentData()) == "even"
        ):
            self._set_layout_sizes(
                self._even_layout_sizes(
                    str(self._layout_axis_combo.currentData())
                )
            )
        self._sync_layout_controls()

    def _sync_layout_controls(self) -> None:
        mode = (
            "even" if self._layout_mode_combo is None
            else str(self._layout_mode_combo.currentData())
        )
        manual = mode == "manual"
        auto_key = self._auto_layout_section_key()
        if self._layout_axis_combo is not None:
            self._layout_axis_combo.setEnabled(self._layout_customizable)
        if self._layout_mode_combo is not None:
            self._layout_mode_combo.setEnabled(self._layout_customizable)
        for section_key,edit in self._layout_size_edits.items():
            editable = (
                self._layout_customizable
                and manual
                and section_key != auto_key
            )
            edit.setEnabled(True)
            edit.setReadOnly(not editable)
        if manual:
            self._update_layout_remainder()
        else:
            self._update_layout_ranges()

    def _set_layout_sizes(self,sizes) -> None:
        edits = tuple(self._layout_size_edits.values())
        previous = [edit.blockSignals(True) for edit in edits]
        try:
            for edit,size in zip(edits,sizes):
                edit.setText(str(int(size)))
        finally:
            for edit,blocked in zip(edits,previous):
                edit.blockSignals(blocked)
        self._update_layout_remainder()

    def _update_layout_remainder(self,*_ignored) -> None:
        if self._layout_axis_combo is None:
            return
        auto_key = self._auto_layout_section_key()
        if auto_key is None:
            return
        auto_edit = self._layout_size_edits.get(auto_key)
        if auto_edit is None:
            return

        total = self._layout_total_size(
            str(self._layout_axis_combo.currentData())
        )
        fixed_total = 0
        for section_key,edit in self._layout_size_edits.items():
            if section_key == auto_key:
                continue
            text = edit.text().strip()
            fixed_total += 0 if not text else int(text)
        previous = auto_edit.blockSignals(True)
        try:
            auto_edit.setText(str(total - fixed_total))
        finally:
            auto_edit.blockSignals(previous)
        self._update_layout_ranges()

    def _update_layout_ranges(self) -> None:
        if self._layout_axis_combo is None:
            return
        axis = str(self._layout_axis_combo.currentData()).lower()
        prefix = axis.upper()
        offset = 0
        valid = True

        for section_key in self._layout_initial_signature.section_keys:
            label = self._layout_range_labels.get(section_key)
            edit = self._layout_size_edits.get(section_key)
            if label is None or edit is None:
                continue

            text = edit.text().strip()
            if not text or not valid:
                valid = False
                label.setText(f"{prefix} -")
                continue

            size = int(text)
            if size <= 0:
                valid = False
                label.setText(f"{prefix} invalid")
                continue

            label.setText(f"{prefix} {offset}-{offset + size - 1}")
            offset += size

    def _even_layout_sizes(self,axis: str):
        total = self._layout_total_size(axis)
        count = len(self._section_snapshots)
        base,remainder = divmod(total,count)
        return tuple(base + (1 if index < remainder else 0)
                     for index in range(count))

    def _layout_total_size(self,axis: str) -> int:
        return (
            self._layout_geometry.width
            if str(axis).lower() == "x" else self._layout_geometry.height
        )

    def _auto_layout_section_key(self) -> str | None:
        keys = self._layout_initial_signature.section_keys
        return None if not keys else keys[-1]

    def changed_topologies(
        self,
    ) -> Mapping[str,Mapping[str,GroupTopology]]:
        """Return topology changes grouped by section key."""
        result = {}
        for section_key,editor in self._editors.items():
            changes = dict(editor.changed_topologies())
            if changes:
                result[section_key] = changes
        return result

    def changed_presentations(
        self,
    ) -> Mapping[str,SectionPresentation]:
        """Return presentation changes grouped by section key."""
        result = {}
        for section_key,checkbox in self._presentation_controls.items():
            presentation = replace(
                self._section_snapshots[section_key].presentation,
                show_calibration_interface=checkbox.isChecked(),
            )
            if presentation != self._section_snapshots[section_key].presentation:
                result[section_key] = presentation
        return result

    def changed_display_mode(self) -> SectionsDisplayMode | None:
        if (
            self._display_mode_initial is None
            or self._display_mode_combo is None
        ):
            return None
        mode = SectionsDisplayMode.normalize(
            self._display_mode_combo.currentData()
        )
        return None if mode == self._display_mode_initial else mode

    def changed_layout(self) -> SectionSplitLayout | None:
        if not self._layout_customizable:
            return None
        if self._layout_axis_combo is None or self._layout_mode_combo is None:
            return None

        axis = str(self._layout_axis_combo.currentData())
        mode = str(self._layout_mode_combo.currentData())
        sizes = None
        if mode == "manual":
            self._update_layout_remainder()
            sizes = []
            for section_key in self._layout_initial_signature.section_keys:
                text = self._layout_size_edits[section_key].text().strip()
                if not text:
                    raise ValueError("Manual section sizes are required.")
                sizes.append(int(text))
            sizes = tuple(sizes)

        layout = SectionSplitLayout(
            n_sections=len(self._section_snapshots),
            axis=axis,
            mode=mode,
            sizes=sizes,
        )
        sections = create_split_section_geometries(
            self._layout_geometry,layout,
        )
        signature = split_layout_signature(self._layout_geometry,sections)
        if signature == self._layout_initial_signature:
            return None
        return layout

    def has_changes(self) -> bool:
        return bool(
            self.changed_topologies()
            or self.changed_presentations()
            or self.changed_layout()
            or self.changed_display_mode()
            or self.changed_interaction_settings()
        )

    def changes(self) -> SLMSectionsSettingsChanges:
        return SLMSectionsSettingsChanges(
            topologies={
                key:dict(value)
                for key,value in self.changed_topologies().items()
            },
            presentations=dict(self.changed_presentations()),
            layout=self.changed_layout(),
            display_mode=self.changed_display_mode(),
            interaction_settings=self.changed_interaction_settings(),
        )

    def accept(self) -> None:
        try:
            self.changed_layout()
        except Exception as error:
            QtWidgets.QMessageBox.warning(
                self,"Section layout",str(error),
            )
            return
        super().accept()

    @classmethod
    def get_changes(
        cls,
        *,
        section_snapshots: Mapping[str,SLMSectionSnapshot],
        section_titles: Mapping[str, str] | None=None,
        section_layout_customizable: bool=False,
        display_mode: SectionsDisplayMode | None=None,
        interaction_settings: RuntimeViewInteractionSettings | None=None,
        title: str="SLM settings",
        parent: QtWidgets.QWidget | None=None,
    ) -> SLMSectionsSettingsChanges | None:
        """Return per-section settings changes, or ``None`` when cancelled."""
        dialog = cls(
            section_snapshots=section_snapshots,
            section_titles=section_titles,
            section_layout_customizable=section_layout_customizable,
            display_mode=display_mode,
            interaction_settings=interaction_settings,
            title=title,
            parent=parent,
        )
        result = dialog.exec_() if hasattr(dialog,"exec_") else dialog.exec()
        if result != QtWidgets.QDialog.Accepted:
            dialog.deleteLater()
            return None
        changes = dialog.changes()
        dialog.deleteLater()
        return changes
