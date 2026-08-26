from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass,field
from typing import (
    Any,
    ClassVar,
    Generic,
    Iterable,
    Mapping,
    TypeVar,
)

from .base import StateModel, runtime_field
from .items import ItemState
from .loading import ConfigPath,ConfigWarning
from ..parameters import ParamRole, make_display_name

ItemStateT = TypeVar("ItemStateT", bound=ItemState)

class GroupStateModel(StateModel):
    """ Base class for GroupStates, ie StateModels representing
    a collection of fields, items or other StateModels. """
    GROUP_KEY: ClassVar[str]
    GROUP_TITLE: ClassVar[str | None] = None

    @classmethod
    def title(cls) -> str:
        return cls.GROUP_TITLE or make_display_name(cls.GROUP_KEY)
    
    # by default, groups are always enabled
    # Child such as Dynamic groups  can be disabled 
    # via their own method set_enabled
    @property
    def enabled(self) -> bool:
        return True


class StaticGroupState(GroupStateModel):
    """Group whose structure is fully declared by its dataclass fields."""

@dataclass
class DynamicGroupState(GroupStateModel, Generic[ItemStateT]):
    """Registry-backed group containing a selectable set of item states."""

    RESERVED_ITEM_KEYS: ClassVar[frozenset] = frozenset({"items", "values", "params", "computation"})

    items: dict[str, ItemStateT] = field(default_factory=dict)
    _registry: Mapping[str, Any] = runtime_field(default_factory=dict)

    enabled: bool = field(default=True)

    def set_enabled(self,enabled: bool) -> None:
        """Enable or destructively disable this group.

        Disabling clears all selected items, resets local parameters to their
        defaults and forces active parameters to False. Re-enabling leaves the
        group empty and inactive until explicitly configured again.
        """
        self.enabled = bool(enabled)
        if self.enabled:
            return

        self.items.clear()
        for key,spec in self.param_specs().items():
            value = False if spec.role is ParamRole.ACTIVE else deepcopy(spec.default)
            self.set_param_value(key,value)

    def child_states(self) -> Mapping[str, StateModel]:
        """ Merge items StateModels with parent behavior child_states"""
        base_children = dict(super().child_states())
        overlap = base_children.keys() & self.items.keys()
        if overlap:
            raise ValueError(
                f"Conflicting child keys in {type(self).__name__}: {sorted(overlap)}"
            )

        children: dict[str, StateModel] = dict(base_children)
        for key, item in self.items.items():
            if not isinstance(item, ItemState):
                raise TypeError(
                    f"Dynamic item '{key}' must be an ItemState, got {type(item).__name__}"
                )
            children[key] = item
        return children

    def available_keys(self) -> tuple[str, ...]:
        return tuple(self._registry.keys())

    def item_description(self,key: str) -> str:
        """Return the user-facing description of one registered item."""
        if key not in self._registry:
            raise KeyError(
                f"Unknown item '{key}'. Available: {sorted(self._registry)}"
            )

        registration = self._registry[key]
        description = getattr(registration,"description","")
        return str(description or "").strip()

    def enabled_keys(self) -> tuple[str, ...]:
        """Return the registry items currently enabled in this group."""
        return tuple(self.items)

    def add(self, key: str) -> ItemStateT:
        if not self.enabled:
            raise RuntimeError(
                f"Cannot add item '{key}' to disabled group '{self.GROUP_KEY}'"
            )
        if key in self.items:
            raise KeyError(f"Item '{key}' is already selected")
        if key in self.RESERVED_ITEM_KEYS:
            raise ValueError(f"Item key '{key}' is reserved")
        if key not in self._registry:
            raise KeyError(
                f"Unknown item '{key}'. Available: {sorted(self._registry)}"
            )
        if key in super().child_states():
            raise ValueError(
                f"Item key '{key}' conflicts with a typed child state"
            )

        item = self._create_item(key)
        if not isinstance(item, ItemState):
            raise TypeError("_create_item() must return an ItemState")
        self.items[key] = item
        return item

    def remove(self, key: str) -> ItemStateT:
        if key not in self.items:
            raise KeyError(f"Item '{key}' is not selected")
        return self.items.pop(key)
    
    def set_enabled_items(self,keys: Iterable[str]) -> None:
        """Replace the currently enabled items while preserving existing requested items."""
        requested = tuple(keys)
        if len(set(requested)) != len(requested):
            raise ValueError("Dynamic selection contains duplicate keys")
        
        if not self.enabled and requested:
            raise ValueError(
                f"Disabled group '{self.GROUP_KEY}' cannot contain selected items"
            )

        old_items = self.items
        self.items = {}
        try:
            for key in requested:
                if key in old_items: # preserve old items 
                    self.items[key] = old_items[key]
                else: 
                    self.add(key)
        except Exception:
            self.items = old_items
            raise

    def validate(self) -> None:
        
        if not self.enabled:
            if self.items:
                raise ValueError(
                    f"Disabled group '{self.GROUP_KEY}' cannot contain selected items"
                )
            
            for key, spec in self.param_specs().items():
                if spec.role is ParamRole.ACTIVE and self.get_param_value(key):
                    raise ValueError(
                        f"Disabled group '{self.GROUP_KEY}' cannot be active"
                    )
                
        for key,item in self.items.items():
            if key not in self._registry:
                raise ValueError(
                    f"Selected item '{key}' is not present in the registry"
                )

            if not isinstance(item,ItemState):
                raise TypeError(
                    f"Dynamic item '{key}' must be a ItemState, "
                    f"got {type(item).__name__}"
                )

        super().validate()

    def load_dict(
        self,
        data: Mapping[str,Any],
        *,
        warnings: list[ConfigWarning] | None = None,
        path: ConfigPath = (),
    ) -> None:
        """Load group structure first, then local and item values."""
        warnings = [] if warnings is None else warnings

        if not isinstance(data,Mapping):
            warnings.append(ConfigWarning(
                path,
                f"Expected a mapping, got {type(data).__name__}; defaults kept",
            ))
            return

        local_specs = self.param_specs()
        allowed = set(local_specs) | {"enabled","items"}

        for key in set(data) - allowed:
            warnings.append(ConfigWarning(
                path + (str(key),),
                "Unknown field; value skipped",
            ))

        if "enabled" in data:
            enabled = data["enabled"]

            if isinstance(enabled,bool):
                self.set_enabled(enabled)
            else:
                warnings.append(ConfigWarning(
                    path + ("enabled",),
                    f"Expected bool, got {type(enabled).__name__}; default kept",
                ))
        else:
            warnings.append(ConfigWarning(
                path + ("enabled",),
                "Missing enabled state; default kept",
            ))

        if not self.enabled:
            for key in local_specs:
                if  key in data and data[key] != self.get_param_value(key):
                    warnings.append(ConfigWarning(
                        path + (key,),
                        "Disabled group cannot restore this value; value skipped",
                    ))

            if data.get("items"):
                warnings.append(ConfigWarning(
                    path + ("items",),
                    "Disabled group cannot contain items; items skipped",
                ))

            return

        for key in local_specs:
            if key not in data:
                continue

            try:
                self.set_param_value(key,data[key])
            except (TypeError,ValueError) as error:
                warnings.append(ConfigWarning(
                    path + (key,),
                    f"Invalid value {data[key]!r}; default kept ({error})",
                ))

        if "items" not in data:
            warnings.append(ConfigWarning(
                path + ("items",),
                "Missing item mapping; default selection kept",
            ))
            return

        items_data = data["items"]

        if not isinstance(items_data,Mapping):
            warnings.append(ConfigWarning(
                path + ("items",),
                f"Expected a mapping, got {type(items_data).__name__}; default selection kept",
            ))
            return

        selected = []

        for key in items_data:
            if key not in self._registry:
                warnings.append(ConfigWarning(
                    path + ("items",str(key)),
                    "Unknown registered item; item skipped",
                ))
                continue

            selected.append(key)

        # The item keys in the config are authoritative.
        self.set_enabled_items(selected)

        for key in selected:
            self.items[key].load_dict(
                items_data[key],
                warnings=warnings,
                path=path + ("items",key),
            )

    def _create_item(self, key: str) -> ItemStateT:
        raise NotImplementedError