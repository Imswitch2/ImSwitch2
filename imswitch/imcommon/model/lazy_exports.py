"""Classes exported lazily by a package of one-class-per-file modules.

The widget and controller packages (``imswitch.imcontrol.view.widgets``,
``imswitch.imcontrol.controller.controllers``) export dozens of classes,
each in a file of its own, and resolve them on demand through a
module-level ``__getattr__`` so a single-widget test does not import the
GUI and hardware dependencies of every other one.

That leaves one seam. A direct ``from package.ImageWidget import
ImageWidget`` makes the import system bind the *submodule* to
``package.ImageWidget``; from then on the attribute exists, the package's
``__getattr__`` is never asked, and ``package.ImageWidget`` is the module.
It has to stay that way: pytest's ``monkeypatch.setattr("package.Module.name")``
and other dotted-path resolvers walk packages with ``getattr`` and need the
module there. So the consumers that want the class -- the widget and
controller factories -- unwrap with :func:`exported_class`, which maps a
module to the class of the same name it exports and leaves a class as it
is.
"""

from __future__ import annotations

from types import ModuleType


def exported_class(value):
    """``value`` itself, or the class a one-class module exports under its own name.

    ``package.ImageWidget`` may be the ``ImageWidget`` class or, after a
    direct import of the file, the module ``package.ImageWidget``; either
    way this returns the class. A module that exports nothing under its
    own name is returned unchanged, so a wrong argument still fails where
    it is used, with the usual message.
    """
    if isinstance(value, ModuleType):
        name = value.__name__.rpartition('.')[2]
        return getattr(value, name, value)
    return value


__all__ = ['exported_class']
