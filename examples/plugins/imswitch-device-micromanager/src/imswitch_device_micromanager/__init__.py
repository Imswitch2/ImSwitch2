"""Micro-Manager device bridge for ImSwitch2, built on pymmcore-plus.

One plugin, five device managers that share one MMCore through a hub (see ``docs/devices/micromanager.rst``):
:class:`~.core.MicroManagerCoreManager` (the hub, an ``rs232``-kind contribution),
:class:`~.camera.MicroManagerCameraManager`, :class:`~.stage.MicroManagerStageManager`,
:class:`~.light.MicroManagerLightManager` and :class:`~.flipmirror.MicroManagerFlipMirrorManager`;
``python -m imswitch_device_micromanager convert`` turns a Micro-Manager ``.cfg`` into a setup file
(:mod:`.convert`).

The manager classes are listed in ``__all__`` but imported lazily, on first attribute access: ImSwitch
resolves them through the manifest's ``python_name`` entries, so importing this package must not pull in
numpy, the plugin API or pymmcore-plus.
"""
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("imswitch-device-micromanager")
except PackageNotFoundError:                  # running from a source tree that was never installed
    __version__ = "0+unknown"

_MANAGERS = {
    "MicroManagerCoreManager": ".core",
    "MicroManagerCameraManager": ".camera",
    "MicroManagerStageManager": ".stage",
    "MicroManagerLightManager": ".light",
    "MicroManagerFlipMirrorManager": ".flipmirror",
}

__all__ = sorted(_MANAGERS)


def __getattr__(name):
    """Import a manager class on first use (PEP 562), so a bare ``import`` stays light."""
    module = _MANAGERS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted([*globals(), *_MANAGERS])
