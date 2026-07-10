"""User-facing drop-in analysis plugins for ImProcess.

A Picasso-style plugin system: a plugin is a single ``.py`` file dropped into
the user plugins directory that defines one or more
:class:`~imswitch.improcess.processors.base.Processor` subclasses. They are
discovered and registered at startup and flow through the same runtime-tool
loader, parameter panel and semantic-kind gating as the built-in processors.

This is deliberately separate from the imcontrol *device* plugin system (pip
packages + entry points, for hardware): analysis processors are the low-friction
case and want a low-friction path.
"""

from .user_plugins import (
    PluginLoadError,
    discover_processor_plugins,
    load_user_processor_plugins,
    user_plugins_directory,
)

__all__ = [
    "PluginLoadError",
    "discover_processor_plugins",
    "load_user_processor_plugins",
    "user_plugins_directory",
]
