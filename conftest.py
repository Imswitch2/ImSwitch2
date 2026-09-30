"""
Root conftest.py: stub optional GUI/hardware packages so unit tests can be
collected and run without a full ImSwitch installation.

napari and vispy are only needed for the live-view widgets; the storers,
signal designers, and other unit-tested components do not use them at runtime.

It also puts the repository root on ``sys.path`` so tests can import top-level
modules that ship in the repo but are not part of the installed distribution --
``utility_scripts`` above all. ``python -m pytest`` adds the working directory
implicitly, which is why this is invisible locally, but the ``pytest`` console
script used in CI does not. Without it, a test importing one of those modules
fails to collect at all, taking its whole lane down with it.
"""
import os
import sys
from unittest.mock import MagicMock

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _stub_if_missing(pkg_root, submodules=()):
    """Register MagicMock stubs when optional packages are missing or broken."""
    try:
        __import__(pkg_root)
    except Exception:
        sys.modules[pkg_root] = MagicMock()
    for sub in submodules:
        module_name = f"{pkg_root}.{sub}"
        try:
            __import__(module_name)
        except Exception:
            sys.modules[module_name] = MagicMock()


_stub_if_missing("napari", [
    "utils",
    "utils.translations",
    "utils.colormaps",
    "utils.events",
])

_stub_if_missing("vispy", [
    "color",
    "scene",
    "scene.visuals",
    "visuals",
    "visuals.transforms",
])

_stub_if_missing("matplotlib", [
    "backends",
    "backends.backend_qt5agg",
    "figure",
    "pyplot",
])


import pytest


@pytest.fixture(autouse=True)
def _memory_limits_start_from_the_literals():
    """Every test sees the built-in memory limits unless it configures its own.

    ``memory_limits.configure`` is process-global (the limits are per machine),
    so a test that boots a module would otherwise leave its options in force
    for every later test in the worker, and a test that patches one of the
    literals would then be testing nothing.
    """
    from imswitch.imcommon.model import memory_limits
    memory_limits.reset()
    yield
    memory_limits.reset()
