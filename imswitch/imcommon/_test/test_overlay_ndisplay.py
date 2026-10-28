"""The flat overlays step aside in 3D display instead of breaking the switch.

Each overlay redraws from the viewer's ``ndisplay`` event. One that raised
there aborted the callbacks queued behind it -- napari's own layer update
among them -- so a visible grid, crosshair or ROI left the viewer half-way
into 3D. The overlay is a flat scene node: in 3D it hides, and it is back
when the viewer shows two dims again.
"""

from types import SimpleNamespace

import numpy as np
import pytest

naparitools = pytest.importorskip('imswitch.imcommon.view.guitools.naparitools')


class _FakeSubvisual:
    def __init__(self):
        self.calls = []

    def set_data(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class _FakeNode:
    def __init__(self):
        self._subvisuals = [_FakeSubvisual()]
        self.visible = True


def _attached_crosshair(displayed):
    overlay = naparitools.VispyCrosshairVisual()
    overlay.node = _FakeNode()
    overlay._nodes = [overlay.node]
    overlay._line_data2D = np.zeros((4, 3))
    overlay._attached = True
    overlay._viewer = SimpleNamespace(dims=SimpleNamespace(displayed=list(displayed)))
    return overlay


def test_a_visible_overlay_hides_in_3d_and_returns_in_2d():
    overlay = _attached_crosshair(displayed=[0, 1, 2])

    overlay._on_data_change(None)                           # no raise

    assert overlay.node.visible is False
    assert overlay.node._subvisuals[0].calls == []

    overlay._viewer.dims.displayed = [1, 2]
    overlay._on_data_change(None)

    assert overlay.node.visible is True
    assert len(overlay.node._subvisuals[0].calls) == 1


def test_an_overlay_the_user_hid_stays_hidden_when_the_viewer_returns_to_2d():
    overlay = _attached_crosshair(displayed=[0, 1, 2])
    overlay.setVisible(False)
    overlay._viewer.dims.displayed = [1, 2]

    overlay._on_data_change(None)

    assert overlay.node.visible is False
    assert overlay.node._subvisuals[0].calls == []
