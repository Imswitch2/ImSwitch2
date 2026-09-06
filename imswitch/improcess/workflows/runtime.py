"""A plugin registry for a headless run.

The GUI fills the process-wide registry from the setup file at startup. A
workflow run gets a registry of its own: fresh, so two runs in one process
cannot see each other's plugins; every built-in available unless a config
narrows it, because a workflow file names what it needs; drop-in plugins
discovered the same way the GUI discovers them; and versions stamped at
registration, as always.
"""

from __future__ import annotations

from typing import Any


class BootstrapError(RuntimeError):
    """The registry could not be built as asked."""


def bootstrap_registry(
    config: dict | None = None,
    *,
    user_plugins: bool = True,
    reconstructor_ids=None,
    processor_ids=None,
):
    """A fresh ``PluginRegistry`` with the requested plugins registered.

    ``config`` is a setup file's ``processing`` block; when it names
    reconstructors/processors those are registered, otherwise every
    built-in is. ``reconstructor_ids`` / ``processor_ids`` override both.
    Duplicate ids are an error rather than a silent replacement.
    """
    from imswitch.improcess.processors import (
        _all_processor_classes,
        load_user_plugins,
    )
    from imswitch.improcess.reconstructors import _AVAILABLE_RECONSTRUCTOR_CLASSES
    from imswitch.improcess.reconstructors.registry import PluginRegistry

    if user_plugins:
        _loaded, errors = load_user_plugins()
        for error in errors:
            # Tolerant, as the GUI is: a broken drop-in file must not stop a batch.
            _log().warning("Skipped drop-in plugin %s: %s", error.path, error.message.splitlines()[-1])

    wanted_reconstructors = list(reconstructor_ids) if reconstructor_ids is not None else None
    wanted_processors = list(processor_ids) if processor_ids is not None else None
    if config and (wanted_reconstructors is None or wanted_processors is None):
        from imswitch.improcess.model.processing_config import plugin_ids_from_config

        cfg_recon, cfg_proc, explicit = plugin_ids_from_config(config)
        if explicit:
            wanted_reconstructors = wanted_reconstructors if wanted_reconstructors is not None else list(cfg_recon)
            wanted_processors = wanted_processors if wanted_processors is not None else list(cfg_proc)

    reconstructors = dict(_AVAILABLE_RECONSTRUCTOR_CLASSES)
    processors = dict(_all_processor_classes())
    if wanted_reconstructors is None:
        wanted_reconstructors = sorted(reconstructors)
    if wanted_processors is None:
        wanted_processors = sorted(processors)

    registry = PluginRegistry()
    seen: set[str] = set()
    for pid in wanted_reconstructors:
        if pid not in reconstructors:
            raise BootstrapError(f"unknown reconstructor {pid!r}")
        if pid in seen:
            raise BootstrapError(f"reconstructor {pid!r} requested twice")
        seen.add(pid)
        registry.register_reconstructor(reconstructors[pid]())
    seen.clear()
    for pid in wanted_processors:
        if pid not in processors:
            raise BootstrapError(f"unknown processor {pid!r}")
        if pid in seen:
            raise BootstrapError(f"processor {pid!r} requested twice")
        seen.add(pid)
        registry.register_processor(processors[pid]())
    return registry


def _log():
    from imswitch.imcommon.model import initLogger

    return initLogger("ImProcessWorkflows", tryInheritParent=False)


def describe_registry(registry) -> dict[str, Any]:
    """Plugin ids, names, versions and default params, for ``--list``."""
    out: dict[str, Any] = {"reconstructors": {}, "processors": {}}
    for plugin in registry.reconstructors():
        out["reconstructors"][plugin.id] = {
            "name": plugin.name, "version": getattr(plugin, "version", ""),
            "params": type(plugin).default_params(),
        }
    for plugin in registry.processors():
        out["processors"][plugin.id] = {
            "name": plugin.name, "version": getattr(plugin, "version", ""),
            "kinds": list(getattr(plugin, "kinds", ())),
            "inputs": [getattr(plugin, "min_inputs", 1), getattr(plugin, "max_inputs", 1)],
            "ports": plugin.output_spec(type(plugin).default_params()).describe(),
            "params": type(plugin).default_params(),
        }
    return out


__all__ = ["BootstrapError", "bootstrap_registry", "describe_registry"]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
