"""Common controller package exports.

Resolve controller classes lazily so importing a lightweight base controller
does not initialize GUI modules or user configuration files.
"""

from importlib import import_module


_EXPORTS = {
    "MainController": ("basecontrollers", "MainController"),
    "ModuleCommunicationChannel": (
        "ModuleCommunicationChannel",
        "ModuleCommunicationChannel",
    ),
    "MultiModuleWindowController": (
        "MultiModuleWindowController",
        "MultiModuleWindowController",
    ),
    "PickDatasetsController": ("PickDatasetsController", "PickDatasetsController"),
    "WidgetController": ("basecontrollers", "WidgetController"),
    "WidgetControllerFactory": ("basecontrollers", "WidgetControllerFactory"),
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
