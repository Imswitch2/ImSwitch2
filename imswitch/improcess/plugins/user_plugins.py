"""Discovery and tolerant loading of user drop-in analysis plugins.

A plugin is a single ``.py`` file in the user plugins directory
(:func:`user_plugins_directory`) that defines one or more ``Processor``
subclasses. Discovery imports each file in isolation, collects the processor
classes it defines (and honours an optional module-level ``register(registry)``
hook), and hands them to the caller.

Loading is deliberately tolerant, mirroring Picasso: a plugin that fails to
import or misbehaves is recorded as an error and skipped, so a single bad file
can never crash ImProcess startup.
"""

from __future__ import annotations

import glob
import importlib.util
import inspect
import os
import sys
import traceback
from dataclasses import dataclass

from imswitch.imcommon.model import dirtools, initLogger
from imswitch.improcess.processors.base import Processor

#: Subdirectory of the user files root that holds drop-in analysis plugins.
_PLUGINS_SUBDIR = "improcess_plugins"

#: Inert reference template written into the directory on first use. The leading
#: underscore means discovery skips it, so it never registers anything itself.
_TEMPLATE_NAME = "_example_plugin.py"

_TEMPLATE = '''\
"""Example ImProcess drop-in analysis plugin.

Copy this file (remove the leading underscore, e.g. ``invert.py``) and edit it.
Every ``.py`` file in this folder that is NOT underscore-prefixed is scanned at
ImProcess startup; each ``Processor`` subclass it defines becomes a fully
integrated analysis tool (parameter panel, result-kind gating, results table /
graph) with no extra UI code.

Files are executed as arbitrary Python at startup — only keep code you trust.
"""

from imswitch.improcess.processors.base import Processor
from imswitch.improcess.model.array_result import ArrayProcessingResult


class InvertProcessor(Processor):
    name = "Invert (example)"
    id = "user.invert-example"       # dotted, user-namespaced ids recommended
    category = "User"
    kinds = ("image",)

    @property
    def applies_to(self):
        return lambda result: getattr(result.data, "ndim", 0) >= 2

    def make_param_widget(self, parent):
        from qtpy import QtWidgets

        widget = QtWidgets.QWidget(parent)
        widget.get_values = lambda: {}
        return widget

    def apply(self, result, params):
        data = result.data
        return ArrayProcessingResult(
            name=f"{result.name} (inverted)",
            data=data.max() - data,
            axis_labels=list(result.axis_labels),
        )
'''


@dataclass(frozen=True)
class PluginLoadError:
    """A plugin file that could not be loaded, with its traceback."""

    path: str
    message: str


def user_plugins_directory(*, create: bool = True) -> str:
    """Return the drop-in plugins directory, creating it (with a template)
    on first use.

    Located under the shared ImSwitch user files root so it sits alongside the
    other user configuration directories.
    """
    directory = os.path.join(dirtools.UserFileDirs.Root, _PLUGINS_SUBDIR)
    if create:
        try:
            os.makedirs(directory, exist_ok=True)
            template = os.path.join(directory, _TEMPLATE_NAME)
            if not os.path.exists(template):
                with open(template, "w", encoding="utf-8") as handle:
                    handle.write(_TEMPLATE)
        except OSError:
            # A read-only or unavailable home directory must not break startup;
            # discovery will simply find nothing.
            pass
    return directory


def _plugin_files(directory: str) -> list[str]:
    """Return sorted ``.py`` files in ``directory``, skipping ``_``-prefixed."""
    files = sorted(glob.glob(os.path.join(directory, "*.py")))
    return [f for f in files if not os.path.basename(f).startswith("_")]


def _load_module_from_path(path: str):
    """Import a standalone ``.py`` file that is not part of any package."""
    module_name = "improcess_user_plugin_" + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create import spec for {path!r}")
    module = importlib.util.module_from_spec(spec)
    # Registered like any import, so ``inspect`` can find the file behind a
    # class defined here (the runtime versions drop-in plugins by a digest
    # of that file) and dataclasses/pickling inside the plugin work. A
    # reload simply replaces the entry.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _processor_classes_in(module) -> list[type]:
    """Return ``Processor`` subclasses *defined in* ``module`` (not imported)."""
    classes = []
    for _name, obj in vars(module).items():
        if (
            inspect.isclass(obj)
            and issubclass(obj, Processor)
            and obj is not Processor
            and obj.__module__ == module.__name__
        ):
            classes.append(obj)
    return classes


def _napari_endpoints_in(module, path: str, errors: list) -> int:
    """Register a plugin file's ``NAPARI_ENDPOINTS`` adapters; returns how many.

    A drop-in file may describe napari endpoints instead of (or as well as)
    processors: a list of ``NapariEndpoint`` objects or of the dict form the
    setup file uses. They are handed to the endpoint registry so the next
    discovery offers them as verified adapters.
    """
    items = getattr(module, "NAPARI_ENDPOINTS", None)
    if not items:
        return 0
    from imswitch.improcess.model.napari_endpoints import register_user_endpoints

    registered, problems = register_user_endpoints(items, source=path)
    for problem in problems:
        errors.append(PluginLoadError(path=path, message=problem))
    return registered


def discover_processor_plugins(
    directory: str | None = None,
) -> tuple[dict[str, type], list[PluginLoadError]]:
    """Discover drop-in processor plugin classes.

    Scans ``directory`` (default: :func:`user_plugins_directory`) for
    non-underscore ``.py`` files, imports each in isolation and collects the
    ``Processor`` subclasses it defines. An optional module-level
    ``register(registry)`` hook is *not* invoked here (registration is the
    caller's concern); it is detected and its classes are still discovered
    through the normal class scan.

    Returns ``(classes_by_id, errors)``. Never raises: an import failure, a
    processor with a duplicate/blank id, or any other problem is recorded in
    ``errors`` and skipped.
    """
    logger = initLogger("ImProcessUserPlugins", tryInheritParent=False)
    if directory is None:
        directory = user_plugins_directory()

    classes: dict[str, type] = {}
    errors: list[PluginLoadError] = []

    for path in _plugin_files(directory):
        try:
            module = _load_module_from_path(path)
        except Exception:
            message = traceback.format_exc()
            logger.warning(f"Failed to load ImProcess plugin {path!r}:\n{message}")
            errors.append(PluginLoadError(path=path, message=message))
            continue

        found = _processor_classes_in(module)
        endpoints = _napari_endpoints_in(module, path, errors)
        if not found and not endpoints:
            errors.append(
                PluginLoadError(
                    path=path,
                    message=(
                        "No Processor subclass (or NAPARI_ENDPOINTS list) "
                        "defined in this file."
                    ),
                )
            )
            continue

        for processor_cls in found:
            processor_id = str(getattr(processor_cls, "id", "") or "").strip()
            if not processor_id:
                errors.append(
                    PluginLoadError(
                        path=path,
                        message=f"{processor_cls.__name__} has no 'id'; skipped.",
                    )
                )
                continue
            if processor_id in classes:
                errors.append(
                    PluginLoadError(
                        path=path,
                        message=(
                            f"Duplicate plugin processor id {processor_id!r}; "
                            f"keeping the first occurrence."
                        ),
                    )
                )
                continue
            classes[processor_id] = processor_cls
            logger.info(
                f"Discovered ImProcess plugin processor "
                f"{processor_id!r} ({getattr(processor_cls, 'name', processor_id)})"
            )

    return classes, errors


def load_user_processor_plugins(
    directory: str | None = None,
) -> dict[str, type]:
    """Discover plugins and return the ``{id: class}`` map (errors logged).

    Convenience wrapper for the common startup path where the caller only wants
    the successfully-loaded classes.
    """
    classes, _errors = discover_processor_plugins(directory)
    return classes


__all__ = [
    "PluginLoadError",
    "discover_processor_plugins",
    "load_user_processor_plugins",
    "user_plugins_directory",
]
