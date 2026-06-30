"""View package exports.

Resolve exports lazily so tests and scripts can import one view component
without importing the full GUI stack.

Several exports live in submodules whose filename matches the exported class
(e.g. ``PickSetupDialog`` -> ``PickSetupDialog.py``). Importing such a
submodule directly -- as ``ImConMainView`` does with
``from .PickSetupDialog import PickSetupDialog`` -- makes the import system
bind the *module* object into this package's namespace as a side effect. That
binding shadows the lazily-resolved class, so a later
``from imswitch.imcontrol.view import PickSetupDialog`` would hand back the
module (``TypeError: 'module' object is not callable``) because ``__getattr__``
is only consulted when normal attribute lookup fails. We drop those shadowing
module bindings in ``__setattr__`` so ``__getattr__`` always resolves the real
export on demand.
"""

import sys
from importlib import import_module
from types import ModuleType


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


class _LazyExportModule(ModuleType):
    """Module type that refuses to let like-named submodules shadow exports.

    The import system binds a submodule object onto its parent package via
    ``setattr`` once the submodule is imported. For names we export lazily that
    binding would mask the resolved class, so we ignore it and let
    ``__getattr__`` resolve the export instead. ``__getattr__``'s own cache
    write goes through ``globals()[name] = ...`` (a direct ``__dict__`` write),
    which bypasses this hook, so the resolved class still sticks.
    """

    def __setattr__(self, name, value):
        if name in _EXPORTS and isinstance(value, ModuleType):
            return
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _LazyExportModule

__all__ = list(_EXPORTS)
