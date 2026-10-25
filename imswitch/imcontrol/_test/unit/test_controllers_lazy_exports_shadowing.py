"""Every controller module carries the name of the class it defines, so the package
attribute ``controllers.FlipMirrorController`` is the module or the class depending on
import order, and ``import pkg.Module as module`` binds through that attribute.  No
attribute-level trick can serve both readers, so the class is resolved at the factory
boundary with ``resolve_controller`` and the package attribute is left alone."""
import importlib
import sys
import types

import pytest

_PACKAGE = "imswitch.imcontrol.controller.controllers"


def _forget_controllers_package():
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(_PACKAGE + "."):
            del sys.modules[name]
    parent = sys.modules.get("imswitch.imcontrol.controller")
    if parent is not None and hasattr(parent, "controllers"):
        delattr(parent, "controllers")


@pytest.fixture
def freshControllersPackage():
    """Re-import the package from scratch; restore the original modules afterwards."""
    saved = {name: module for name, module in sys.modules.items()
             if name == _PACKAGE or name.startswith(_PACKAGE + ".")}
    parent = sys.modules.get("imswitch.imcontrol.controller")
    savedAttr = getattr(parent, "controllers", None)
    _forget_controllers_package()
    yield
    _forget_controllers_package()
    sys.modules.update(saved)
    if parent is not None and savedAttr is not None:
        parent.controllers = savedAttr


def test_import_as_module_yields_the_module(freshControllersPackage):
    import imswitch.imcontrol.controller.controllers.FlipMirrorController as module
    assert isinstance(module, types.ModuleType)
    assert hasattr(module, "FlipMirrorController")


def test_resolve_controller_returns_the_class_after_the_submodule_was_imported_first(
        freshControllersPackage):
    submodule = importlib.import_module(f"{_PACKAGE}.FlipMirrorController")
    package = importlib.import_module(_PACKAGE)
    # the import machinery bound the *module* on the package ...
    assert isinstance(package.FlipMirrorController, types.ModuleType)
    # ... and the factory boundary still hands out the class
    resolved = package.resolve_controller("FlipMirrorController")
    assert isinstance(resolved, type)
    assert resolved is submodule.FlipMirrorController


def test_resolve_controller_returns_the_class_when_nothing_was_imported_yet(
        freshControllersPackage):
    package = importlib.import_module(_PACKAGE)
    resolved = package.resolve_controller("FlipMirrorController")
    assert isinstance(resolved, type)
    assert resolved.__name__ == "FlipMirrorController"
    # and the module is still what `import ... as module` yields afterwards
    import imswitch.imcontrol.controller.controllers.FlipMirrorController as module
    assert isinstance(module, types.ModuleType)
    assert module.FlipMirrorController is resolved


def test_resolve_controller_rejects_unknown_names(freshControllersPackage):
    package = importlib.import_module(_PACKAGE)
    with pytest.raises(AttributeError):
        package.resolve_controller("NoSuchController")


def test_main_controller_builds_every_controller_through_resolve_controller():
    import inspect
    from imswitch.imcontrol.controller import ImConMainController as mainModule
    source = inspect.getsource(mainModule)
    assert "resolve_controller" in source
    assert "getattr(controllers," not in source
