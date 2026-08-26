from dataclasses import dataclass



@dataclass(frozen=True)
class GroupTopology:
    """UI/runtime structure of one section group."""

    enabled: bool
    item_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.enabled,bool):
            raise TypeError("Group topology enabled state must be a bool")

        item_keys = tuple(self.item_keys)
        if any(not isinstance(key,str) or not key for key in item_keys):
            raise TypeError("Group topology item keys must be non-empty strings")
        if len(set(item_keys)) != len(item_keys):
            raise ValueError("Group topology cannot contain duplicate item keys")
        if not self.enabled and item_keys:
            raise ValueError("Disabled group topology cannot contain items")

        object.__setattr__(self,"item_keys",item_keys)
