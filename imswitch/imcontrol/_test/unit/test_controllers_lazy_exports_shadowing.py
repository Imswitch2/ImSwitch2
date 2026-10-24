"""The controllers package exports classes lazily; importing a controller's submodule first
must not turn the package attribute into the module (it broke app boot after any test had
imported a submodule directly: ``'module' object is not callable``)."""
import importlib
import sys
import types


def test_package_attribute_is_the_class_even_after_the_submodule_was_imported_first():
    for name in list(sys.modules):
        if name.startswith("imswitch.imcontrol.controller.controllers"):
            del sys.modules[name]
    submodule = importlib.import_module(
        "imswitch.imcontrol.controller.controllers.FlipMirrorController")
    package = importlib.import_module("imswitch.imcontrol.controller.controllers")
    exported = package.FlipMirrorController
    assert not isinstance(exported, types.ModuleType)
    assert exported is submodule.FlipMirrorController
    from imswitch.imcontrol.controller.controllers import FlipMirrorController as viaFrom
    assert viaFrom is submodule.FlipMirrorController
