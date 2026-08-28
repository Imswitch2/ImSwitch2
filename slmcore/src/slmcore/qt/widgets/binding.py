"""Canonical slmcore parameter-path bindings for retained Qt fields."""

from __future__ import annotations

from dataclasses import dataclass,field
from typing import Any,Callable

from ...core.engine.state.base import ParamPath,StateModel
from .fields import ParamField,ParamForm
from ..sections.policy import RenderPolicy


@dataclass
class ParameterBinding:
    """Map relative backend ``ParamPath`` objects to retained fields."""

    fields: dict[ParamPath, ParamField] = field(default_factory=dict)
    forms: list[ParamForm] = field(default_factory=list)

    def bind_field(
        self,
        path: ParamPath,
        param_field: ParamField,
        on_edit: Callable[[ParamPath,Any],None],
    ) -> None:
        path = tuple(path)
        if path in self.fields:
            raise KeyError(f"Duplicate UI binding for parameter path {path}")
        self.fields[path] = param_field
        param_field.sigValueChanged.connect(
            lambda _key,value,bound_path=path:on_edit(bound_path,value)
        )

    def bind_form(
        self,
        form: ParamForm,
        prefix: ParamPath,
        on_edit: Callable[[ParamPath,Any],None],
    ) -> None:
        prefix = tuple(prefix)
        self.forms.append(form)

        for key,param_field in form.fields.items():
            self.bind_field(prefix + (key,),param_field,on_edit)

    def apply_state(self,state: StateModel) -> None:
        for parameter in state.iter_parameters():
            param_field = self.fields.get(parameter.path)
            if param_field is not None:
                param_field.set_value(parameter.value,emit=False)
            self._refresh_form_links()

    def set_parameter(self,path: ParamPath,value: Any) -> bool:
        param_field = self.fields.get(tuple(path))
        if param_field is None:
            return False
        param_field.set_value(value,emit=False)
        self._refresh_form_links()
        return True

    def set_unit_mode(self,mode: str) -> None:
        for param_field in self.fields.values():
            if param_field.definition.conversion_available:
                param_field.set_unit_mode(mode)

    def refresh_conversions(self) -> None:
        for param_field in self.fields.values():
            if param_field.definition.conversion_available:
                param_field.refresh()

    def validate_coverage(
        self,state: StateModel,policy: RenderPolicy,
    ) -> None:
        expected = {
            parameter.path
            for parameter in state.iter_parameters()
            if policy.is_parameter_visible(parameter.spec)
        }
        actual = set(self.fields)
        if expected == actual:
            return
        raise RuntimeError(
            "Invalid parameter binding coverage for "
            f"{type(state).__name__}; "
            f"missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    
    def _refresh_form_links(self) -> None:
        for form in self.forms:
            form.refresh_links()

    @property
    def has_converters(self) -> bool:
        return any(
            param_field.definition.conversion_available
            for param_field in self.fields.values()
        )
