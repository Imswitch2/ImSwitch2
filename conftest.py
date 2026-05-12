"""
Root conftest.py: stub optional GUI/hardware packages so unit tests can be
collected and run without a full ImSwitch installation.

napari and vispy are only needed for the live-view widgets; the storers,
signal designers, and other unit-tested components do not use them at runtime.
"""
import sys
from unittest.mock import MagicMock


def _stub_if_missing(pkg_root, submodules=()):
    """Register MagicMock stubs for pkg_root and its submodules when
    the package is not importable."""
    try:
        __import__(pkg_root)
    except Exception:
        sys.modules[pkg_root] = MagicMock()
        for sub in submodules:
            sys.modules[f"{pkg_root}.{sub}"] = MagicMock()


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
