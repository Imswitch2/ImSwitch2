"""User-facing drop-in analysis plugins for ImProcess.

A Picasso-style plugin system: a plugin is a single ``.py`` file dropped into
the user plugins directory that defines one or more
:class:`~imswitch.improcess.processors.base.Processor` or
:class:`~imswitch.improcess.reconstructors.base.Reconstructor` subclasses.
They are discovered and registered at startup (and on *Plugins -> Reload
plugins*) and flow through the same runtime-tool loader, parameter panel,
semantic-kind gating and reconstructor picker as the built-ins.

This is deliberately separate from the imcontrol *device* plugin system (pip
packages + entry points, for hardware): analysis plugins are the low-friction
case and want a low-friction path.
"""

from .user_plugins import (
    DiscoveredPlugins,
    LoadedPlugins,
    PluginLoadError,
    clear_user_plugins,
    discover_plugins,
    discover_processor_plugins,
    install_plugin_files,
    load_user_plugins,
    load_user_processor_plugins,
    user_plugins_directory,
)

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
