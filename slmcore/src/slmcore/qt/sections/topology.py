"""Reusable Qt editor for one SLM section's dynamic topology."""

from __future__ import annotations

from typing import Mapping

from qtpy import QtWidgets

from ...core.engine.parameters.spec import make_display_name
from ...core.engine.section.snapshot import SLMSectionSnapshot
from ...core.engine.state.groups import DynamicGroupState
from ...core.engine.state.topology import GroupTopology


class SectionTopologyEditor(QtWidgets.QWidget):
    """Edit enabled dynamic groups and registry-backed items for one section.

    The editor is a pure projection of an :class:`SLMSectionSnapshot`. It does
    not mutate the snapshot or access an ``SLMRuntime``. Call
    :meth:`changed_topologies` to retrieve a partial topology mapping suitable
    for ``SLMRuntime.apply_section_topology``.
    """

    def __init__(
        self,
        *,
        snapshot: SLMSectionSnapshot,
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)

        self._initial_topologies: dict[str, GroupTopology] = {}
        self._group_boxes: dict[str, QtWidgets.QGroupBox] = {}
        self._item_checkboxes: dict[str, dict[str, QtWidgets.QCheckBox]] = {}

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(8)

        editable_count = 0
        for entry in snapshot.group_entries():
            state = entry.state
            if not isinstance(state,DynamicGroupState):
                continue

            editable_count += 1
            topology = GroupTopology(
                enabled=state.enabled,
                item_keys=state.enabled_keys(),
            )
            self._initial_topologies[entry.group_key] = topology

            group_box = QtWidgets.QGroupBox(state.title())
            group_box.setCheckable(True)
            group_layout = QtWidgets.QVBoxLayout(group_box)
            group_layout.setContentsMargins(12,8,8,8)
            group_layout.setSpacing(4)

            item_checkboxes: dict[str, QtWidgets.QCheckBox] = {}
            enabled_items = set(topology.item_keys)
            for item_key in state.available_keys():
                row_widget = QtWidgets.QWidget(group_box)
                row_layout = QtWidgets.QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0,0,0,0)
                row_layout.setSpacing(4)

                checkbox = QtWidgets.QCheckBox(
                    make_display_name(item_key),
                    row_widget,
                )
                checkbox.setChecked(item_key in enabled_items)
                checkbox.setProperty("slmcoreItemKey",item_key)

                row_layout.addWidget(checkbox)
                row_layout.addStretch(1)

                description = state.item_description(item_key)
                tooltip = description or "No description available"

                info_button = QtWidgets.QToolButton(row_widget)
                info_button.setIcon(
                    info_button.style().standardIcon(
                        QtWidgets.QStyle.SP_MessageBoxInformation,
                    )
                )
                info_button.setAutoRaise(True)
                info_button.setFixedSize(18,18)
                info_button.setToolTip(tooltip)
                info_button.setStyleSheet("""
                    QToolButton {
                        border: none;
                        background: transparent;
                        padding: 0px;
                        margin: 0px;
                    }
                    QToolButton:hover {
                        background: transparent;
                    }
                    QToolButton:pressed {
                        background: transparent;
                    }
                """)

                row_layout.addWidget(info_button)
                group_layout.addWidget(row_widget)

                item_checkboxes[item_key] = checkbox

            if not item_checkboxes:
                label = QtWidgets.QLabel("No registered items are available.")
                label.setStyleSheet("color: #888;")
                group_layout.addWidget(label)

            group_box.setChecked(topology.enabled)
            self._group_boxes[entry.group_key] = group_box
            self._item_checkboxes[entry.group_key] = item_checkboxes
            layout.addWidget(group_box)

        if editable_count == 0:
            label = QtWidgets.QLabel(
                "This section has no topology-editable groups."
            )
            label.setWordWrap(True)
            label.setStyleSheet("color: #888;")
            layout.addWidget(label)

        layout.addStretch(1)

    @property
    def editable_group_keys(self) -> tuple[str, ...]:
        return tuple(self._initial_topologies)

    def topologies(self) -> Mapping[str,GroupTopology]:
        """Return the complete topology currently represented by the editor."""
        result = {}
        for group_key,group_box in self._group_boxes.items():
            enabled = group_box.isChecked()
            if enabled:
                selected = {
                    key for key,checkbox
                    in self._item_checkboxes[group_key].items()
                    if checkbox.isChecked()
                }
                initial = self._initial_topologies[group_key].item_keys
                initial_set = set(initial)
                retained = tuple(key for key in initial if key in selected)
                added = tuple(
                    key for key in self._item_checkboxes[group_key]
                    if key in selected and key not in initial_set
                )
                item_keys = retained + added
            else:
                item_keys = ()
            result[group_key] = GroupTopology(
                enabled=enabled,item_keys=item_keys,
            )
        return result

    def changed_topologies(self) -> Mapping[str,GroupTopology]:
        """Return only groups whose requested topology differs from the input."""
        return {
            key:topology
            for key,topology in self.topologies().items()
            if topology != self._initial_topologies[key]
        }

    def has_changes(self) -> bool:
        return bool(self.changed_topologies())


class SectionTopologyDialog(QtWidgets.QDialog):
    """Modal shell around :class:`SectionTopologyEditor`."""

    def __init__(
        self,
        *,
        snapshot: SLMSectionSnapshot,
        title: str="Section settings",
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(440,520)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10,10,10,10)
        layout.setSpacing(8)

        description = QtWidgets.QLabel(
            "Choose which registry-backed groups and items are present in "
            "this section."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        self.editor = SectionTopologyEditor(snapshot=snapshot)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setWidget(self.editor)
        layout.addWidget(scroll,1)

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

    def changed_topologies(self) -> Mapping[str,GroupTopology]:
        return self.editor.changed_topologies()

    @classmethod
    def get_changes(
        cls,
        *,
        snapshot: SLMSectionSnapshot,
        title: str="Section settings",
        parent: QtWidgets.QWidget | None=None,
    ) -> Mapping[str, GroupTopology] | None:
        """Return a partial topology mapping, or ``None`` when cancelled."""
        dialog = cls(snapshot=snapshot,title=title,parent=parent)
        result = dialog.exec_() if hasattr(dialog,"exec_") else dialog.exec()
        if result != QtWidgets.QDialog.Accepted:
            dialog.deleteLater()
            return None
        changes = dict(dialog.changed_topologies())
        dialog.deleteLater()
        return changes
