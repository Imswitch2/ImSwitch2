"""Clearing an embedded viewer's layers must terminate.

``EmbeddedNapari`` protects some layers from deletion by having the layer
list's ``__delitem__`` quietly skip them. ``MutableSequence.clear`` is a
``pop()`` loop that only stops on ``IndexError`` -- so with one protected
layer in the list, ``viewer.layers.clear()`` spun forever at full CPU. The
viewer now clears through :func:`removeUnprotectedLayers`, tested here on a
list with the same "deletion may decline" behaviour, without a GL context.
"""

from __future__ import annotations

from collections.abc import MutableSequence
from types import SimpleNamespace

import pytest

pytest.importorskip("qtpy.QtWidgets")

from imswitch.imcommon.view.guitools.naparitools import (  # noqa: E402
    removeUnprotectedLayers,
)


class _ProtectingList(MutableSequence):
    """A list whose ``__delitem__`` declines protected items, like the viewer's."""

    def __init__(self, items):
        self._items = list(items)

    def __getitem__(self, index):
        return self._items[index]

    def __setitem__(self, index, value):
        self._items[index] = value

    def __delitem__(self, index):
        if not getattr(self._items[index], 'protected', False):
            del self._items[index]

    def __len__(self):
        return len(self._items)

    def insert(self, index, value):
        self._items.insert(index, value)


def _layer(name, protected=False):
    return SimpleNamespace(name=name, protected=protected)


def test_clearing_keeps_protected_layers_and_terminates():
    layers = _ProtectingList([
        _layer("base", protected=True),
        _layer("cloud"),
        _layer("control"),
    ])

    removeUnprotectedLayers(layers)

    assert [layer.name for layer in layers] == ["base"]


def test_clearing_removes_everything_when_nothing_is_protected():
    layers = _ProtectingList([_layer("a"), _layer("b")])

    removeUnprotectedLayers(layers)

    assert len(layers) == 0


def test_clearing_an_empty_list_is_a_no_op():
    layers = _ProtectingList([])

    removeUnprotectedLayers(layers)

    assert len(layers) == 0


def test_the_stock_clear_would_not_terminate_here():
    """Document the hazard the helper exists for, without reproducing it.

    ``pop()`` on this list returns the protected layer and leaves it in place,
    which is exactly what makes ``MutableSequence.clear`` loop.
    """
    layers = _ProtectingList([_layer("base", protected=True)])

    popped = layers.pop()

    assert popped.name == "base"
    assert len(layers) == 1
