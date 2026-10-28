"""A lazily exported name may be a module or its class; the factories take either."""

import importlib
import sys
import textwrap
from types import SimpleNamespace

import pytest

from imswitch.imcommon.model.lazy_exports import exported_class


@pytest.fixture
def package(tmp_path, monkeypatch):
    root = tmp_path / 'lazypkg'
    root.mkdir()
    (root / '__init__.py').write_text(textwrap.dedent('''
        from importlib import import_module

        _EXPORTS = {'Thing': 'Thing', 'Other': 'others'}

        def __getattr__(name):
            try:
                module_name = _EXPORTS[name]
            except KeyError as exc:
                raise AttributeError(name) from exc
            value = getattr(import_module(f'.{module_name}', __name__), name)
            globals()[name] = value
            return value
    '''))
    (root / 'Thing.py').write_text('class Thing:\n    pass\n')
    (root / 'others.py').write_text('class Other:\n    pass\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    yield 'lazypkg'
    for name in list(sys.modules):
        if name == 'lazypkg' or name.startswith('lazypkg.'):
            del sys.modules[name]


def test_a_direct_submodule_import_binds_the_module_and_exported_class_unwraps_it(package):
    module = importlib.import_module(f'{package}.Thing')
    pkg = sys.modules[package]

    assert pkg.Thing is module                      # what the import system binds, and
                                                    # what dotted-path patchers rely on
    assert exported_class(pkg.Thing) is module.Thing
    assert exported_class(pkg.Other) is pkg.Other   # resolved lazily: already the class


def test_exported_class_leaves_anything_else_alone():
    import os.path

    class Plain:
        pass

    assert exported_class(Plain) is Plain
    assert exported_class(os.path) is os.path       # no attribute of its own name
    assert exported_class(42) == 42


def test_the_widget_factory_accepts_the_module(qtbot):
    from imswitch.imcontrol.view.widgets import basewidgets
    import imswitch.imcontrol.view.widgets.LiveReconWidget as module   # binds the module

    factory = basewidgets.WidgetFactory(SimpleNamespace())
    widget = factory.createWidget(module)
    qtbot.addWidget(widget)

    assert isinstance(widget, module.LiveReconWidget)


def test_the_controller_factory_accepts_the_module():
    from imswitch.imcommon.controller.basecontrollers import WidgetControllerFactory
    import types

    module = types.ModuleType('fake.pkg.Ctrl')

    class Ctrl:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

    module.Ctrl = Ctrl
    factory = WidgetControllerFactory(moduleCommChannel=None)

    controller = factory.createController(module, widget='w')

    assert isinstance(controller, Ctrl) and controller.kwargs['widget'] == 'w'
