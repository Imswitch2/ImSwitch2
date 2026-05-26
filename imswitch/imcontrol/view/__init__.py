"""View package exports.

Resolve exports lazily so tests and scripts can import one view component
without importing the full GUI stack.
"""

from importlib import import_module


_EXPORTS = {
    "ImConMainView": ("ImConMainView", "ImConMainView"),
    "PickSetupDialog": ("PickSetupDialog", "PickSetupDialog"),
    "SLMDisplay": ("SLMDisplay", "SLMDisplay"),
    "ViewSetupInfo": ("guitools", "ViewSetupInfo"),
}


def __getattr__(name):
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    module = import_module(f".{module_name}", __name__)
    exported = getattr(module, attr_name)
    globals()[name] = exported
    return exported


__all__ = list(_EXPORTS)
