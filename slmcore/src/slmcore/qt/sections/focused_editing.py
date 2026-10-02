"""Presentation-only configuration for compact Focused editing."""

from __future__ import annotations

from qtpy import QtWidgets

from ...core.engine.parameters.spec import ParamRole,make_display_name
from ...core.engine.section.components import AberrationsState,PatternsState
from ...core.engine.section.snapshot import SLMSectionSnapshot
from ...setup import FocusedEditingPreferences


_SUGGESTION_EXCLUDED_GROUPS = frozenset({"optics","corrections","aberrations"})
_GROUP_LABELS = {
    "optics":"General parameters",
    "corrections":"Corrections",
    "patterns":"Patterns",
    "aberrations":"Aberrations",
    "cgh":"CGH",
}


def _group_active(state) -> bool:
    spec = state.param_specs().get("active")
    if spec is None or spec.role is not ParamRole.ACTIVE:
        return True
    return bool(state.get_param_value("active"))


def _item_active(item) -> bool:
    params = getattr(item,"params",None)
    if params is None:
        return True
    spec = params.param_specs().get("active")
    if spec is None or spec.role is not ParamRole.ACTIVE:
        return True
    return bool(params.get_param_value("active"))


def suggested_focused_editing(
    snapshot: SLMSectionSnapshot,
) -> FocusedEditingPreferences:
    """Suggest active daily editors, excluding general/corrections/aberrations."""
    groups = []
    items = {}
    for entry in snapshot.group_entries():
        state = entry.state
        group_key = entry.group_key
        if not state.enabled or group_key in _SUGGESTION_EXCLUDED_GROUPS:
            continue
        if not _group_active(state):
            continue

        if isinstance(state,PatternsState):
            selected = tuple(
                key for key,item in state.items.items()
                if _item_active(item)
            )
            if selected:
                items[group_key] = selected
            continue

        # CGH is intentionally atomic in the first Focused-editing pass.
        groups.append(group_key)

    return FocusedEditingPreferences(groups=tuple(groups),items=items)


class FocusedEditingEditor(QtWidgets.QWidget):
    """Edit the one persistent Focused-editing selection for a section."""

    def __init__(
        self,
        *,
        snapshot: SLMSectionSnapshot,
        selection: FocusedEditingPreferences | None=None,
        parent: QtWidgets.QWidget | None=None,
    ) -> None:
        super().__init__(parent)
        self._snapshot = snapshot
        self._initial = FocusedEditingPreferences.from_value(selection)
        self._group_checkboxes: dict[str,QtWidgets.QCheckBox] = {}
        self._item_checkboxes: dict[str,dict[str,QtWidgets.QCheckBox]] = {}

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(8)

        description = QtWidgets.QLabel(
            "Choose the editors shown by the Focused View button. "
            "This only changes visibility; hidden settings remain unchanged."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        suggested = suggested_focused_editing(snapshot)
        shown = self._initial if self._initial.configured else suggested
        if not self._initial.configured:
            note = QtWidgets.QLabel(
                "Suggested initial selection is based on currently active editors, "
                "excluding General parameters, Corrections and Aberrations."
            )
            note.setWordWrap(True)
            note.setStyleSheet("color: #666;")
            layout.addWidget(note)

        choices = QtWidgets.QWidget(self)
        choices_layout = QtWidgets.QVBoxLayout(choices)
        choices_layout.setContentsMargins(0,0,0,0)
        choices_layout.setSpacing(6)

        for entry in snapshot.group_entries():
            state = entry.state
            if not state.enabled:
                continue
            group_key = entry.group_key
            label = _GROUP_LABELS.get(group_key,state.title())

            if isinstance(state,(PatternsState,AberrationsState)):
                box = QtWidgets.QGroupBox(label,choices)
                box_layout = QtWidgets.QVBoxLayout(box)
                box_layout.setContentsMargins(10,8,8,8)
                box_layout.setSpacing(3)
                item_checks = {}
                for item_key in state.items:
                    checkbox = QtWidgets.QCheckBox(
                        make_display_name(item_key),box,
                    )
                    checkbox.setChecked(
                        item_key in shown.item_keys(group_key)
                    )
                    box_layout.addWidget(checkbox)
                    item_checks[item_key] = checkbox
                if not item_checks:
                    empty = QtWidgets.QLabel("No configured items.",box)
                    empty.setStyleSheet("color: #888;")
                    box_layout.addWidget(empty)
                self._item_checkboxes[group_key] = item_checks
                choices_layout.addWidget(box)
                continue

            checkbox = QtWidgets.QCheckBox(label,choices)
            checkbox.setChecked(group_key in shown.groups)
            self._group_checkboxes[group_key] = checkbox
            choices_layout.addWidget(checkbox)

        choices_layout.addStretch(1)
        layout.addWidget(choices,1)

        suggested_button = QtWidgets.QPushButton("Use suggested selection")
        suggested_button.setToolTip(
            "Select currently active editors, excluding General parameters, "
            "Corrections and Aberrations."
        )
        suggested_button.clicked.connect(
            lambda _checked=False:self.set_selection(suggested_focused_editing(self._snapshot))
        )
        layout.addWidget(suggested_button,0)

    def selection(self) -> FocusedEditingPreferences:
        groups = tuple(
            key for key,checkbox in self._group_checkboxes.items()
            if checkbox.isChecked()
        )
        items = {
            group:tuple(
                key for key,checkbox in checkboxes.items()
                if checkbox.isChecked()
            )
            for group,checkboxes in self._item_checkboxes.items()
        }
        return FocusedEditingPreferences(groups=groups,items=items)

    def set_selection(self,selection: FocusedEditingPreferences) -> None:
        value = FocusedEditingPreferences.from_value(selection)
        for key,checkbox in self._group_checkboxes.items():
            checkbox.setChecked(key in value.groups)
        for group,checkboxes in self._item_checkboxes.items():
            selected = set(value.item_keys(group))
            for key,checkbox in checkboxes.items():
                checkbox.setChecked(key in selected)

    def changed_selection(self) -> FocusedEditingPreferences | None:
        current = self.selection()
        return None if current == self._initial else current
