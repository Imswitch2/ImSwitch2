"""The display contract is checked against the napari implementation by
reading the class, so no napari viewer is built (one crashes offscreen)."""

from __future__ import annotations

import inspect

import pytest

from imswitch.improcess.view import result_display
from imswitch.improcess.view.ReconstructionView import ReconstructionView

PROTOCOLS = (result_display.ResultDisplay, result_display.ResultListPane)


def _protocol_names(protocol):
    return [
        name for name, member in vars(protocol).items()
        if inspect.isfunction(member) and not name.startswith("_")
    ]


def _protocol_attributes(protocol):
    return list(getattr(protocol, "__annotations__", {}))


def _shape(function):
    """Name, kind and default of every parameter after ``self``."""
    params = list(inspect.signature(function).parameters.values())[1:]
    return [(p.name, p.kind, p.default) for p in params]


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_reconstruction_view_defines_every_protocol_method(protocol):
    missing = [n for n in _protocol_names(protocol) if not hasattr(ReconstructionView, n)]
    assert missing == []


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_reconstruction_view_signatures_match(protocol):
    for name in _protocol_names(protocol):
        declared = _shape(getattr(protocol, name))
        actual = _shape(getattr(ReconstructionView, name))
        assert actual == declared, name


def test_signals_are_declared_on_the_view():
    # reconList is created in __init__, so only the class-level signals are checkable.
    for protocol in PROTOCOLS:
        for name in _protocol_attributes(protocol):
            if name.startswith("sig"):
                assert hasattr(ReconstructionView, name), name


def test_protocols_hold_only_methods_the_controllers_call():
    # A guard against the contract growing methods nobody calls: every name
    # must appear in a controller or in the toolbar controller.
    import pathlib

    root = pathlib.Path(result_display.__file__).resolve().parents[1] / "controller"
    text = (root / "ReconstructionViewController.py").read_text() + (
        root / "ImageToolbarController.py").read_text()
    for protocol in PROTOCOLS:
        for name in _protocol_names(protocol):
            assert name in text, name
