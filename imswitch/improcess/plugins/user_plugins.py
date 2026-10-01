"""Discovery and tolerant loading of user drop-in analysis plugins.

A plugin is a single ``.py`` file in the user plugins directory
(:func:`user_plugins_directory`) that defines one or more ``Processor`` or
``Reconstructor`` subclasses. Discovery imports each file in isolation,
collects the plugin classes it defines (and honours an optional module-level
``NAPARI_ENDPOINTS`` list), and hands them to the caller.
:func:`load_user_plugins` is the one entry point that runs a scan and installs
what it found into the processor *and* reconstructor enumerations, so a single
scan keeps both in step with the folder.

Loading is deliberately tolerant, mirroring Picasso: a plugin that fails to
import or misbehaves is recorded as an error and skipped, so a single bad file
can never crash ImProcess startup.
"""

from __future__ import annotations

import glob
import importlib.util
import inspect
import os
import shutil
import sys
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

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
ImProcess startup and on Plugins -> Reload plugins. Each ``Processor`` subclass
it defines becomes a fully integrated analysis tool (parameter panel,
result-kind gating, results table / graph) and each ``Reconstructor`` subclass
appears in the reconstructor picker of the Parameters dock -- with no extra UI
code either way.

Files are executed as arbitrary Python at startup — only keep code you trust.
"""

import numpy as np

from imswitch.improcess.processors.base import Processor
from imswitch.improcess.reconstructors.base import Reconstructor
from imswitch.improcess.model.array_result import ArrayProcessingResult


class InvertProcessor(Processor):
    name = "Invert (example)"
    id = "user.invert-example"       # dotted, user-namespaced ids recommended
    category = "User"
    kinds = ("image",)

    @classmethod
    def default_params(cls) -> dict:
        # The parameters a freshly opened widget hands apply(): same keys,
        # same defaults as get_values() below. This declaration is what
        # workflows, replay and the provenance record use when no widget
        # exists. A plugin that does not override it is GUI-only.
        return {}

    @property
    def applies_to(self):
        return lambda result: getattr(result.data, "ndim", 0) >= 2

    def make_param_widget(self, parent):
        from qtpy import QtWidgets

        widget = QtWidgets.QWidget(parent)
        widget.get_values = lambda: dict(self.default_params())
        return widget

    def apply(self, result, params):
        data = np.asarray(result.data)   # may be a lazy view over the file
        return ArrayProcessingResult(
            name=f"{result.name} (inverted)",
            data=data.max() - data,
            axis_labels=list(result.axis_labels),
        )


class MeanFramesReconstructor(Reconstructor):
    """Turns a raw stack into one image: the mean of all its frames.

    A reconstructor takes the raw ``DataObj`` (the file as loaded) rather
    than a result, and every dataset goes through exactly one of them.
    """

    name = "Mean of frames (example)"
    id = "user.mean-frames-example"
    file_extensions = ["hdf5", "tiff", "tif", "zarr"]

    @classmethod
    def default_params(cls) -> dict:
        return {}

    def make_param_widget(self, parent):
        from qtpy import QtWidgets

        widget = QtWidgets.QWidget(parent)
        widget.get_values = lambda: dict(self.default_params())
        return widget

    def make_metadata_dialog(self, parent):
        return None          # no acquisition metadata to ask for

    def process(self, data_obj, params, context=None):
        data_obj.checkAndLoadData()
        data = np.asarray(data_obj.data, dtype=np.float32)
        frames = data.reshape(-1, *data.shape[-2:])   # every leading axis is a frame axis
        return ArrayProcessingResult(
            name=f"{data_obj.name} (mean)",
            data=frames.mean(axis=0),
            axis_labels=["Y", "X"],
        )
'''


@dataclass(frozen=True)
class PluginLoadError:
    """A plugin file that could not be loaded, with its traceback."""

    path: str
    message: str


@dataclass(frozen=True)
class DiscoveredPlugins:
    """The plugin classes one scan of the plugins directory found."""

    processors: dict[str, type] = field(default_factory=dict)
    reconstructors: dict[str, type] = field(default_factory=dict)
    errors: list[PluginLoadError] = field(default_factory=list)


@dataclass(frozen=True)
class LoadedPlugins:
    """The ids one scan installed into the enumerations, and what it skipped."""

    processors: list[str] = field(default_factory=list)
    reconstructors: list[str] = field(default_factory=list)
    errors: list[PluginLoadError] = field(default_factory=list)

    @property
    def ids(self) -> list[str]:
        """Every installed id, processors first."""
        return [*self.processors, *self.reconstructors]


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


def _classes_in(module, base: type) -> list[type]:
    """Return ``base`` subclasses *defined in* ``module`` (not imported)."""
    classes = []
    for _name, obj in vars(module).items():
        if (
            inspect.isclass(obj)
            and issubclass(obj, base)
            and obj is not base
            and obj.__module__ == module.__name__
        ):
            classes.append(obj)
    return classes


def _processor_classes_in(module) -> list[type]:
    """Return ``Processor`` subclasses *defined in* ``module`` (not imported)."""
    return _classes_in(module, Processor)


def _reconstructor_classes_in(module) -> list[type]:
    """Return ``Reconstructor`` subclasses *defined in* ``module``.

    The reconstructor base is imported here rather than at module level: the
    reconstructors package pulls in every built-in modality, which this
    module's other callers (the plugin store, the version stamp) never need.
    """
    from imswitch.improcess.reconstructors.base import Reconstructor

    return _classes_in(module, Reconstructor)


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


def _collect(
    found: Iterable[type],
    into: dict[str, type],
    kind: str,
    path: str,
    errors: list[PluginLoadError],
    logger,
) -> None:
    """Add ``found`` classes to ``into`` by id, recording blank/duplicate ids."""
    for plugin_cls in found:
        plugin_id = str(getattr(plugin_cls, "id", "") or "").strip()
        if not plugin_id:
            errors.append(
                PluginLoadError(
                    path=path,
                    message=f"{plugin_cls.__name__} has no 'id'; skipped.",
                )
            )
            continue
        if plugin_id in into:
            errors.append(
                PluginLoadError(
                    path=path,
                    message=(
                        f"Duplicate plugin {kind} id {plugin_id!r}; "
                        f"keeping the first occurrence."
                    ),
                )
            )
            continue
        into[plugin_id] = plugin_cls
        logger.info(
            f"Discovered ImProcess plugin {kind} "
            f"{plugin_id!r} ({getattr(plugin_cls, 'name', plugin_id)})"
        )


def discover_plugins(directory: str | None = None) -> DiscoveredPlugins:
    """Discover drop-in plugin classes of every kind.

    Scans ``directory`` (default: :func:`user_plugins_directory`) for
    non-underscore ``.py`` files, imports each in isolation and collects the
    ``Processor`` and ``Reconstructor`` subclasses it defines. Registration is
    the caller's concern (see :func:`load_user_plugins`).

    Never raises: an import failure, a plugin with a duplicate/blank id, a
    file that defines nothing, or any other problem is recorded in ``errors``
    and skipped.
    """
    logger = initLogger("ImProcessUserPlugins", tryInheritParent=False)
    if directory is None:
        directory = user_plugins_directory()

    processors: dict[str, type] = {}
    reconstructors: dict[str, type] = {}
    errors: list[PluginLoadError] = []

    for path in _plugin_files(directory):
        try:
            module = _load_module_from_path(path)
        except Exception:
            message = traceback.format_exc()
            logger.warning(f"Failed to load ImProcess plugin {path!r}:\n{message}")
            errors.append(PluginLoadError(path=path, message=message))
            continue

        found_processors = _processor_classes_in(module)
        found_reconstructors = _reconstructor_classes_in(module)
        endpoints = _napari_endpoints_in(module, path, errors)
        if not found_processors and not found_reconstructors and not endpoints:
            errors.append(
                PluginLoadError(
                    path=path,
                    message=(
                        "No Processor subclass, Reconstructor subclass or "
                        "NAPARI_ENDPOINTS list defined in this file."
                    ),
                )
            )
            continue

        _collect(found_processors, processors, "processor", path, errors, logger)
        _collect(
            found_reconstructors, reconstructors, "reconstructor", path, errors, logger
        )

    return DiscoveredPlugins(
        processors=processors, reconstructors=reconstructors, errors=errors
    )


def discover_processor_plugins(
    directory: str | None = None,
) -> tuple[dict[str, type], list[PluginLoadError]]:
    """Discover drop-in processor plugin classes.

    The processor half of :func:`discover_plugins`, kept for callers that only
    deal in processors: ``(classes_by_id, errors)``. The errors are those of
    the whole scan, a reconstructor problem included -- a caller reporting
    what went wrong in the folder should not hide half of it.
    """
    found = discover_plugins(directory)
    return dict(found.processors), list(found.errors)


def load_user_plugins(directory: str | None = None) -> LoadedPlugins:
    """Scan the plugins folder once and install everything it defines.

    Populates the processor and reconstructor user-plugin tables (so the
    discovered plugins show up in every enumeration and can be instantiated
    by id like a built-in) and re-collects the napari endpoint adapters that
    plugin files contribute. Built-in ids always win a collision: a plugin
    that reuses one is rejected with a recorded error. A removed file's
    plugins disappear from the tables on the next call.

    Never raises -- discovery is tolerant.
    """
    from imswitch.improcess import processors as processors_pkg
    from imswitch.improcess import reconstructors as reconstructors_pkg
    from imswitch.improcess.model.napari_endpoints import clear_user_endpoints

    # Endpoint adapters contributed by plugin files are re-collected on
    # every scan, so a removed file's adapters disappear with it.
    clear_user_endpoints()
    found = discover_plugins(directory)
    errors = list(found.errors)
    processor_ids, processor_errors = processors_pkg._install_user_processor_classes(
        found.processors
    )
    reconstructor_ids, reconstructor_errors = (
        reconstructors_pkg._install_user_reconstructor_classes(found.reconstructors)
    )
    return LoadedPlugins(
        processors=processor_ids,
        reconstructors=reconstructor_ids,
        errors=errors + processor_errors + reconstructor_errors,
    )


def load_user_processor_plugins(
    directory: str | None = None,
) -> dict[str, type]:
    """Discover plugins and return the processor ``{id: class}`` map (errors logged).

    Convenience wrapper for the common startup path where the caller only wants
    the successfully-loaded processor classes.
    """
    classes, _errors = discover_processor_plugins(directory)
    return classes


def clear_user_plugins() -> None:
    """Forget every discovered user plugin, of every kind (test/reset helper)."""
    from imswitch.improcess import processors as processors_pkg
    from imswitch.improcess import reconstructors as reconstructors_pkg
    from imswitch.improcess.model.napari_endpoints import clear_user_endpoints

    processors_pkg.clear_user_plugins()
    reconstructors_pkg.clear_user_reconstructors()
    clear_user_endpoints()


def install_plugin_files(
    paths: Iterable[str],
    directory: str | None = None,
    *,
    overwrite: Callable[[str], bool] | None = None,
) -> tuple[list[str], list[str]]:
    """Copy plugin ``.py`` files into the plugins directory.

    The GUI's *Add plugin file...* action: the user picks files anywhere on
    disk and they land in the folder discovery scans, so a reload (or the
    next start) picks them up. ``overwrite(name)`` is asked before an
    existing file of the same name is replaced; without it an existing file
    is left alone. Only files discovery would actually see are copied: a
    non-``.py`` file or an underscore-prefixed one is reported instead of
    silently installed as something inert.

    Returns ``(installed_paths, skipped)``, ``skipped`` holding one
    human-readable reason per file that was not copied.
    """
    if directory is None:
        directory = user_plugins_directory()
    installed: list[str] = []
    skipped: list[str] = []
    for source in paths:
        name = os.path.basename(str(source))
        if not name.endswith(".py"):
            skipped.append(f"{name}: not a .py file")
            continue
        if name.startswith("_"):
            skipped.append(
                f"{name}: underscore-prefixed files are ignored by discovery; "
                f"rename it first"
            )
            continue
        target = os.path.join(directory, name)
        if os.path.exists(target):
            if os.path.realpath(target) == os.path.realpath(str(source)):
                skipped.append(f"{name}: already in the plugins folder")
                continue
            if overwrite is None or not overwrite(name):
                skipped.append(f"{name}: already exists (not replaced)")
                continue
        shutil.copyfile(str(source), target)
        installed.append(target)
    return installed, skipped


__all__ = [
    "DiscoveredPlugins",
    "LoadedPlugins",
    "PluginLoadError",
    "clear_user_plugins",
    "discover_plugins",
    "discover_processor_plugins",
    "install_plugin_files",
    "load_user_plugins",
    "load_user_processor_plugins",
    "user_plugins_directory",
]
