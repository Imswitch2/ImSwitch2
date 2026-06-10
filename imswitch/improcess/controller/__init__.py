"""ImProcess controller package exports.

Resolve controller classes lazily so importing a small controller submodule for
tests does not initialize the full GUI stack and Napari.
"""

from importlib import import_module


_EXPORTS = {
    "ImProcessMainController": ("ImProcessMainController", "ImProcessMainController"),
    "GraphController": ("GraphController", "GraphController"),
    "ResultProcessorController": ("ResultProcessorController", "ResultProcessorController"),
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
