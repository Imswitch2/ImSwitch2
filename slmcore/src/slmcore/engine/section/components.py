
from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any,ClassVar,Mapping,Iterable

from ..parameters.spec import EditorKind, ParamRole, ParamSpec
from ..state.items import CGHTargetState, ItemState, ParameterSetState
from ..registry import (
    AberrationRegistration,
    CGHAlgorithmRegistration,
    PatternRegistration,
    TargetRegistration,
)
from ..state import DynamicGroupState, StaticGroupState, runtime_field, ParamPath
from ..parameters import param_field
from ..state.loading import ConfigPath,ConfigWarning


@dataclass
class OpticsState(StaticGroupState):
    GROUP_KEY: ClassVar[str] = "optics"

    wavelength_nm: int = param_field(488,min_value=350,max_value=1200)
    pupil_radius_px: int = param_field(0,min_value=0)
    center_offset_x_px: int = param_field(0)
    center_offset_y_px: int = param_field(0)


@dataclass
class CorrectionsState(StaticGroupState):
    GROUP_KEY: ClassVar[str] = "corrections"

    active: bool = param_field(True,role=ParamRole.ACTIVE)
    apply_correction_pattern: bool = param_field(True)
    apply_twopi_value: bool = param_field(True)


_PATTERN_ACTIVE_SPEC = ParamSpec(
    default=False,
    ptype=bool,
    editor=EditorKind.CHECK_BOX,
    role=ParamRole.ACTIVE,
)

@dataclass
class PatternsState(DynamicGroupState[ItemState]):
    GROUP_KEY: ClassVar[str] = "patterns"

    active: bool = param_field(True,role=ParamRole.ACTIVE)
    _registry: Mapping[str, PatternRegistration] = runtime_field(default_factory=dict)

    def _create_item(self, key: str) -> ItemState:
        registration = self._registry[key]
        if "active" in registration.params:
            raise ValueError(
                f"Pattern '{key}' cannot register reserved parameter 'active'"
            )

        specs = {"active": _PATTERN_ACTIVE_SPEC, **registration.params}
        return ItemState(params=ParameterSetState.from_specs(specs))


@dataclass
class AberrationsState(DynamicGroupState[ItemState]):
    GROUP_KEY: ClassVar[str] = "aberrations"

    active: bool = param_field(True,role=ParamRole.ACTIVE)
    _registry: Mapping[str, AberrationRegistration] = runtime_field(default_factory=dict)

    def _create_item(self, key: str) -> ItemState:
        registration = self._registry[key]
        return ItemState(
            params=ParameterSetState.from_specs(registration.params)
        )


@dataclass
class CGHState(DynamicGroupState[CGHTargetState]):
    GROUP_KEY: ClassVar[str] = "cgh"
    SELECTED_TARGET_KEY: ClassVar[str] = "selected_target"

    active: bool = param_field(False,role=ParamRole.ACTIVE)
    selected_target: str | None = param_field(
        None,
        allow_none=True,
        editor=EditorKind.COMBO_BOX,
        role=ParamRole.SELECTOR,
    )
    _registry: Mapping[str, TargetRegistration] = runtime_field(default_factory=dict)
    _algorithm_registry: Mapping[str, CGHAlgorithmRegistration] = (
        runtime_field(default_factory=dict)
    )


    @classmethod
    def selected_target_path(cls) -> ParamPath:
        """Return the group-relative selected-target parameter path."""
        return cls.SELECTED_TARGET_KEY,

    @classmethod
    def target_params_path(cls,target_key: str) -> ParamPath:
        """Return the group-relative path to one target parameter state."""
        return CGHTargetState.params_path(target_key)

    @classmethod
    def computation_params_path(cls,target_key: str) -> ParamPath:
        """Return the group-relative path to one computation parameter state."""
        return CGHTargetState.computation_params_path(target_key)

    def _create_item(self, key: str) -> CGHTargetState:
        target_registration = self._registry[key]
        algorithm_key = target_registration.algorithm
        if algorithm_key not in self._algorithm_registry:
            raise ValueError(
                f"Target '{key}' references unknown algorithm '{algorithm_key}'"
            )

        algorithm_registration = self._algorithm_registry[algorithm_key]
        return CGHTargetState(
            params=ParameterSetState.from_specs(target_registration.params),
            algorithm=algorithm_key,
            feedback_capabilities=target_registration.feedback_capabilities,
            computation=ItemState(
                params=ParameterSetState.from_specs(algorithm_registration.params)
            ),
            lock_state=target_registration.target_class.create_lock_state(),
        )

    def param_specs(self):
        """ CGH overrides param_specs so that selected_target
        choices represents the actual possilbe choices. """
        specs = dict(super().param_specs())
        specs[self.SELECTED_TARGET_KEY] = replace(
            specs[self.SELECTED_TARGET_KEY],
            choices=self.enabled_keys(),
        )
        return MappingProxyType(specs)

    def select_target(self, key: str | None) -> None:
        if key is not None and key not in self.items:
            raise KeyError(f"Target '{key}' is not selected")
        self.selected_target = key

    def remove(self, key: str) -> CGHTargetState:
        item = super().remove(key)
        if self.selected_target == key:
            self.selected_target = None
        return item

    def set_enabled_items(self,keys: Iterable[str]) -> None:
        super().set_enabled_items(keys)

        if self.selected_target not in self.items:
            self.select_target(None)

    def load_dict(
        self,
        data: Mapping[str,object],
        *,
        warnings: list[ConfigWarning] | None = None,
        path: ConfigPath = (),
    ) -> None:
        """Load targets first and restore selected_target last."""
        warnings = [] if warnings is None else warnings

        if not isinstance(data,Mapping):
            warnings.append(ConfigWarning(
                path,
                f"Expected a mapping, got "
                f"{type(data).__name__}; defaults kept",
            ))
            return

        selected_target = data.get(self.SELECTED_TARGET_KEY)
        payload = dict(data)
        payload.pop(self.SELECTED_TARGET_KEY,None)

        super().load_dict(payload,warnings=warnings,path=path)

        if not self.enabled:
            if selected_target is not None:
                warnings.append(ConfigWarning(
                    path + self.selected_target_path(),
                    "Disabled CGH cannot select a target; value skipped",
                ))
            return

        if selected_target is None:
            self.select_target(None)
        elif selected_target in self.items:
            self.select_target(selected_target)
        else:
            warnings.append(ConfigWarning(
                path + self.selected_target_path(),
                f"Target {selected_target!r} is not available; no target selected",
            ))
            self.select_target(None)


    def validate(self) -> None:
        super().validate()

        if self.selected_target is not None and self.selected_target not in self.items:
            raise ValueError(
                f"Selected target '{self.selected_target}' is not present in items"
            )

        for key, target in self.items.items():
            registration = self._registry[key]
            # validation check, cross-parameter 
            registration.target_class.validate_params(target.params.values)

            if target.algorithm != registration.algorithm:
                raise ValueError(
                    f"Target '{key}' uses algorithm '{target.algorithm}', expected "
                    f"'{registration.algorithm}'"
                )
            if target.algorithm not in self._algorithm_registry:
                raise ValueError(
                    f"Target '{key}' uses unknown algorithm '{target.algorithm}'"
                )
            
    def canonicalize_selected_target(
        self,
        changes: Mapping[ParamPath,Any],
        context: Any,
        *,
        force: bool=False,
        require_unchanged: bool=False,
    ) -> tuple[dict[ParamPath, Any], Any | None]:
        """Canonicalize the selected target and return its prepared definition."""
        changed_target,changed_keys = self._extract_target_param_changes(changes)
        selected_changed = self.selected_target_path() in changes

        if changed_target is None:
            if not force and not selected_changed:
                return {},None
            target_key = self.selected_target
        else:
            target_key = changed_target

        if target_key is None:
            return {},None

        target_state = self.items[target_key]
        registration = self._registry[target_key]
        target_class = registration.target_class
        before = target_state.params.to_dict()

        result = target_class.canonicalize_params(
            params=before,changed_keys=changed_keys,context=context,
            lock_state=target_state.lock_state,
        )
        if not isinstance(result,tuple) or len(result) != 2:
            raise TypeError(
                f"Target '{target_key}' canonicalization must return "
                "(params, prepared_definition)"
            )

        canonical_params,prepared_definition = result
        canonical_params = target_class._validated_params(canonical_params)
        target_class.validate_params(canonical_params)

        expected_keys = set(registration.params)
        if set(canonical_params) != expected_keys:
            raise RuntimeError(
                f"Target '{target_key}' returned an invalid canonical mapping"
            )

        target_params_path = self.target_params_path(target_key)
        changed_values = {
            target_params_path + (key,):value
            for key,value in canonical_params.items()
            if before[key] != value
        }
        if require_unchanged and changed_values:
            details = ", ".join(
                f"{path[-1]}: {before[path[-1]]!r} -> {value!r}"
                for path,value in changed_values.items()
            )
            raise ValueError(
                f"Target '{target_key}' is not canonical for the current "
                f"section context ({details})"
            )

        for key,value in canonical_params.items():
            target_state.params.set_param_value(key,value)

        return changed_values,prepared_definition


    def set_target_lock(
        self,target_key: str,kind: str | None,reference,
    ) -> bool:
        """Replace one target's optional persisted lock state."""
        target = self.items[target_key]
        lock = target.lock_state
        if lock is None:
            raise ValueError(f"Target '{target_key}' does not support lattice locks")
        before = lock.to_dict()
        setter = getattr(lock,"set",None)
        if not callable(setter):
            raise TypeError(f"Target '{target_key}' lock state is not mutable")
        setter(kind,reference)
        return lock.to_dict() != before

    def refresh_target_lock_reference_from_changes(
        self,target_key: str,changed_keys: Iterable[str],
    ) -> bool:
        """Update an active lock from explicitly edited locked quantities."""
        target = self.items[target_key]
        lock = target.lock_state
        if lock is None or getattr(lock,"kind",None) is None:
            return False
        changed = set(changed_keys)
        kind = str(lock.kind)
        if kind == "fov":
            keys = ("fov_x_px","fov_y_px")
        elif kind == "n_foci":
            keys = ("n_foci_x","n_foci_y")
        else:
            return False
        if not any(key in changed for key in keys):
            return False
        previous = tuple(lock.reference)
        values = list(previous)
        for index,key in enumerate(keys):
            if key in changed:
                values[index] = target.params.get_param_value(key)
        lock.set(kind,tuple(values))
        return tuple(lock.reference) != previous

    def _extract_target_param_changes(
        self,changes: Mapping[ParamPath,Any],
    ) -> tuple[str | None, tuple[str, ...]]:
        """Extract target-parameter changes for the single selected target.

        Paths received by CGHState are relative to the CGH group:

            (target_key,"params",parameter_key)

        Algorithm-computation paths and local CGH parameters are ignored.
        A patch cannot modify target parameters belonging to more than one target,
        and the affected target must be the currently selected target.
        """
        target_keys = set()
        changed_keys = []

        for path in changes:
            if (
                len(path) != 3
                or path[1] != CGHTargetState.PARAMS_STATE_KEY
            ):
                continue

            target_key,param_key = path[0],path[2]
            target_keys.add(target_key)
            changed_keys.append(param_key)

        if not target_keys:
            return None,()

        if len(target_keys) > 1:
            raise ValueError(
                f"Cannot modify parameters for multiple targets: {sorted(target_keys)}"
            )

        target_key = next(iter(target_keys))

        if self.selected_target is None:
            raise ValueError(
                f"Cannot modify target '{target_key}': no CGH target is selected"
            )

        if target_key != self.selected_target:
            raise ValueError(
                f"Cannot modify target '{target_key}': selected target is "
                f"'{self.selected_target}'"
            )

        if target_key not in self.items:
            raise RuntimeError(
                f"Selected target '{target_key}' is not present in CGH items"
            )

        return target_key,tuple(changed_keys)