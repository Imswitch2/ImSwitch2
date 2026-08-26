"""Generic collapsible parameter section used by backend-aware group views."""

from __future__ import annotations

from numbers import Number
from typing import Any,Mapping,Sequence

from qtpy import QtCore,QtWidgets

from ...engine.parameters.spec import ParamSpec,make_display_name
from .fields import ParamField,ParamForm
from .uitools import CollapsibleSection,ElidedLabel

PARAM_META_KEY = "_meta"
PARAM_ACTIVE_KEY = "active"


class ParamSection(QtCore.QObject):
    """Collapsible container composed of keyed :class:`ParamForm` objects.

    This is intentionally generic. A backend-aware ``GroupView`` wraps it and
    supplies parameter paths, group semantics and specialized layout.
    """

    sigValueChanged = QtCore.Signal(str,str,object)
    sigActiveChanged = QtCore.Signal(bool)

    VALID_SUMMARY_MODES = {
        "none",
        "active_forms",
        "nonzero_fields",
        "enabled_fields",
        "runtime_value",
    }

    def __init__(
        self,
        name: str,
        title: str,
        active_def: ParamSpec | None=None,
        metadata_definitions: Mapping[str, ParamSpec] | None=None,
        summary_mode: str="none",
        summary_prefix: str="In use:",
        summary_max_width: int=220,
        conversion_context=None,
        collapsible_kwargs: dict | None=None,
        horizontal_spacing: int=10,
        vertical_spacing: int=4,
    ):
        if summary_mode not in self.VALID_SUMMARY_MODES:
            raise ValueError(
                f"Unknown summary mode '{summary_mode}'. Expected one of "
                f"{sorted(self.VALID_SUMMARY_MODES)}."
            )

        widget = CollapsibleSection(
            title,**dict(collapsible_kwargs or {}),
        )
        super().__init__(widget)

        self.name = name
        self.widget = widget
        self.summary_mode = summary_mode
        self._conversion_context = conversion_context
        self._forms: dict[str, ParamForm] = {}
        self._form_definitions: dict[str, dict[str, ParamSpec]] = {}
        self._form_labels: dict[str, str] = {}
        self._runtime_summary: Any = None
        self._next_row = 0

        self.layout = QtWidgets.QGridLayout()
        self.layout.setHorizontalSpacing(horizontal_spacing)
        self.layout.setVerticalSpacing(vertical_spacing)
        self.widget.setContentLayout(self.layout)

        self.metadata_form: ParamForm | None = None
        self.active_field: ParamField | None = None

        metadata = dict(metadata_definitions or {})
        if PARAM_ACTIVE_KEY in metadata:
            raise ValueError(
                f"Section metadata key '{PARAM_ACTIVE_KEY}' must be provided "
                "through active_def, not metadata_definitions."
            )
        if active_def is not None:
            metadata = {PARAM_ACTIVE_KEY:active_def,**metadata}

        if metadata:
            self.metadata_form = ParamForm(
                name=PARAM_META_KEY,definitions=metadata,
                conversion_context=conversion_context,parent=self.widget,
                per_row=1,use_subsection=True,editor_width=60,
                show_complementary=False,
            )
            if active_def is not None:
                self.active_field = self.metadata_form.field(PARAM_ACTIVE_KEY)
                self.widget.addHeaderWidget(self.active_field.editor)

            form_name = self.metadata_form.name
            self.metadata_form.sigValueChanged.connect(
                lambda key,value,name=form_name:
                    self._on_form_value_changed(name,key,value)
            )

        self.summary_prefix_label: QtWidgets.QLabel | None = None
        self.summary_label: ElidedLabel | None = None
        if summary_mode != "none":
            self.summary_prefix_label = QtWidgets.QLabel(summary_prefix)
            self.summary_label = ElidedLabel("None")
            self.summary_label.setMaximumWidth(summary_max_width)
            self.summary_label.setStyleSheet("color: #888;")
            self.widget.addHeaderWidget(self.summary_prefix_label)
            self.widget.addHeaderWidget(self.summary_label)

        self.refresh_summary()

    def add_form(
        self,
        name: str,
        definitions: Mapping[str,ParamSpec],
        *,
        active_def: ParamSpec | None=None,
        use_subsection: bool=True,
        per_row: int=1,
        editor_width: int=60,
        show_complementary: bool=False,
        mount: bool=True,
        layout_spec: Sequence[Sequence[str]] | None=None,
    ) -> ParamForm:
        if name == PARAM_META_KEY:
            raise ValueError(f"'{PARAM_META_KEY}' is reserved for metadata")
        if name in self._forms:
            raise KeyError(
                f"Form '{name}' is already registered in section '{self.name}'"
            )
        if not isinstance(definitions,Mapping):
            raise TypeError(
                f"Definitions for form '{name}' must be a mapping, got "
                f"{type(definitions).__name__}"
            )

        normalized = dict(definitions)
        if active_def is not None:
            if PARAM_ACTIVE_KEY in normalized:
                raise ValueError(
                    f"Form '{name}' already defines reserved parameter "
                    f"'{PARAM_ACTIVE_KEY}'"
                )
            normalized = {PARAM_ACTIVE_KEY:active_def,**normalized}

        form = ParamForm(
            name=name,definitions=normalized,
            conversion_context=self._conversion_context,parent=self.widget,
            per_row=per_row,use_subsection=use_subsection,
            editor_width=editor_width,
            show_complementary=show_complementary,
        )
        self._forms[name] = form
        self._form_definitions[name] = normalized
        self._form_labels[name] = make_display_name(name)
        form.sigValueChanged.connect(
            lambda key,value,form_name=name:
                self._on_form_value_changed(form_name,key,value)
        )

        if mount:
            self._next_row = form.add_to_grid(
                layout=self.layout,start_row=self._next_row,
                layout_spec=layout_spec,
            )
        self.refresh_summary()
        return form

    @property
    def forms(self) -> tuple[ParamForm, ...]:
        forms = []
        if self.metadata_form is not None:
            forms.append(self.metadata_form)
        forms.extend(self._forms.values())
        return tuple(forms)

    @property
    def next_row(self) -> int:
        return self._next_row

    def form(self,name: str) -> ParamForm:
        if self.metadata_form is not None and self.metadata_form.name == name:
            return self.metadata_form
        return self._forms[name]

    def metadata_field(self,key: str) -> ParamField:
        if self.metadata_form is None:
            raise KeyError(f"Section '{self.name}' has no metadata")
        return self.metadata_form.field(key)

    def is_active(self) -> bool:
        return True if self.active_field is None else bool(self.active_field.value())

    def set_active(self,active: bool,emit: bool=False) -> None:
        if self.active_field is None:
            return
        self.active_field.set_value(active,emit=emit)
        self.refresh_summary()

    def set_summary_value(self,value: Any) -> None:
        self._runtime_summary = value
        self.refresh_summary()

    def refresh_summary(self) -> None:
        if self.summary_label is None:
            return
        if not self.is_active():
            summary = "None"
        else:
            items = self._build_summary_items()
            summary = ", ".join(items) if items else "None"
        self.summary_label.set_full_text(summary)

    def _build_summary_items(self) -> list:
        if self.summary_mode == "none":
            return []
        if self.summary_mode == "runtime_value":
            value = self._runtime_summary
            if value is None:
                return []
            if isinstance(value,(list,tuple,set)):
                return [str(item) for item in value if item not in (None,"")]
            return [str(value)] if value != "" else []
        if self.summary_mode == "active_forms":
            return [
                self._form_labels[name]
                for name,form in self._forms.items()
                if bool(form.values().get(PARAM_ACTIVE_KEY,False))
            ]
        if self.summary_mode == "enabled_fields":
            items = []
            for name,form in self._forms.items():
                values = form.values()
                for key,definition in self._form_definitions[name].items():
                    if key == PARAM_ACTIVE_KEY:
                        continue
                    if definition.ptype is bool and bool(values.get(key)):
                        items.append(definition.label_for(key))
            return items
        if self.summary_mode == "nonzero_fields":
            items = []
            for name,form in self._forms.items():
                values = form.values()
                for key,definition in self._form_definitions[name].items():
                    value = values.get(key)
                    if (
                        isinstance(value,Number)
                        and not isinstance(value,bool)
                        and value != 0
                    ):
                        items.append(definition.label_for(key))
            return items
        return []

    def values(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for form in self.forms:
            values = form.values()
            if form.use_subsection:
                result[form.name] = values
            else:
                result.update(values)
        return result

    def set_values(self,values: Mapping[str,Any],emit: bool=False) -> None:
        for form in self.forms:
            form_values = values.get(form.name,{}) if form.use_subsection else values
            if isinstance(form_values,Mapping):
                form.set_values(form_values,emit=emit)
        self.refresh_summary()

    def set_unit_mode(self,mode: str) -> None:
        for form in self.forms:
            form.set_unit_mode(mode)

    def refresh(self) -> None:
        for form in self.forms:
            form.refresh()
        self.refresh_summary()

    def _on_form_value_changed(
        self,form_name: str,key: str,value: Any,
    ) -> None:
        self.refresh_summary()
        if (
            self.metadata_form is not None
            and form_name == self.metadata_form.name
            and self.active_field is not None
            and key == PARAM_ACTIVE_KEY
        ):
            self.sigActiveChanged.emit(bool(value))
        self.sigValueChanged.emit(form_name,key,value)


# Transitional compatibility for code that still imports ParamGroup.
ParamGroup = ParamSection
