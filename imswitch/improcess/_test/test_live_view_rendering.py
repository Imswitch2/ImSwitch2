"""Guards for the live render path's log-and-continue fallbacks.

``tryFastLiveUpdate`` and ``moveDimStep`` swallow exceptions into
``self._logger.debug`` and carry on. That is deliberate -- a render hiccup must
not kill a live run -- but it means a genuine breakage degrades *silently*: the
fast path falls back to a full rebuild on every frame, or the timepoint slider
quietly stops advancing, and the only trace is a debug line.

So these tests assert on the log, not just the return value. Each pairs a
happy-path "logged nothing" check with a positive control that the logging
fires when it should -- otherwise the guard could pass for the wrong reason.

Real napari layers are not used: constructing widgets crashes the test process
in this environment (see test_live_view_entries). The methods only need
``.data`` / ``.refresh()`` from a layer and ``.dims`` from the viewer, so those
are faked and the shipping methods are bound to the stub.
"""

from unittest.mock import MagicMock

import numpy as np
import pytest

from imswitch.improcess.view.ReconstructionView import ReconstructionView


class _FakeLayer:
    def __init__(self, data):
        self.data = data
        self.refreshes = 0

    def refresh(self):
        self.refreshes += 1


class _FakeDims:
    def __init__(self, ndim=3, hi=9):
        self.ndim = ndim
        self.range = [(0, hi, 1)] * ndim
        self.steps = {}

    def set_current_step(self, axis, step):
        self.steps[axis] = step


class _ViewStub:
    """Carries the shipping render methods, with fakes for what they touch."""

    tryFastLiveUpdate = ReconstructionView.tryFastLiveUpdate
    moveDimStep = ReconstructionView.moveDimStep

    def __init__(self, img_layer=None, display_layers=(), anchor=False, dims=None):
        self.imgLayer = img_layer
        self._displayLayers = list(display_layers)
        self._imgLayerIsDisplayAnchor = anchor
        self.napariViewer = MagicMock()
        self.napariViewer.dims = dims if dims is not None else _FakeDims()
        self._logger = MagicMock()

    @property
    def debug_logs(self):
        return self._logger.debug.call_args_list


class _Result:
    """Minimal result exposing the two shapes tryFastLiveUpdate consumes."""

    def __init__(self, layer_arrays=None, data=None):
        self._layer_arrays = layer_arrays
        self.data = data

    def display_layer_data(self):
        if self._layer_arrays is None:
            raise RuntimeError("display_layer_data exploded")
        return self._layer_arrays


# --- tryFastLiveUpdate -----------------------------------------------------

def test_fast_update_swaps_layer_data_and_logs_nothing():
    """The steady-state path must succeed silently -- any log means it degraded."""
    old_a, old_b = np.zeros((2, 3)), np.zeros((2, 3))
    a, b = _FakeLayer(old_a), _FakeLayer(old_b)
    view = _ViewStub(img_layer=a, display_layers=[b], anchor=True)

    new_a, new_b = np.full((2, 3), 1.0), np.full((2, 3), 2.0)
    assert view.tryFastLiveUpdate(_Result([new_a, new_b]), [0, 1]) is True

    assert view.debug_logs == []
    assert a.data is new_a and b.data is new_b
    assert (a.refreshes, b.refreshes) == (1, 1)


def test_fast_update_without_display_layers_uses_result_data():
    layer = _FakeLayer(np.zeros((2, 3)))
    view = _ViewStub(img_layer=layer)

    result = _Result(layer_arrays=[], data=np.arange(6).reshape(3, 2))
    assert view.tryFastLiveUpdate(result, [1, 0]) is True     # transposes to (2, 3)

    assert view.debug_logs == []
    assert layer.data.shape == (2, 3)
    assert layer.refreshes == 1


def test_fast_update_declines_on_layer_count_mismatch():
    a = _FakeLayer(np.zeros((2, 3)))
    view = _ViewStub(img_layer=a, anchor=True)     # one target

    arrays = [np.ones((2, 3)), np.ones((2, 3))]    # two arrays
    assert view.tryFastLiveUpdate(_Result(arrays), [0, 1]) is False
    assert a.refreshes == 0                        # nothing touched


def test_fast_update_declines_on_shape_mismatch_without_partial_update():
    """Validation runs before any assignment -- a late mismatch must not
    leave earlier layers already swapped."""
    a, b = _FakeLayer(np.zeros((2, 3))), _FakeLayer(np.zeros((2, 3)))
    first = a.data
    view = _ViewStub(img_layer=a, display_layers=[b], anchor=True)

    arrays = [np.ones((2, 3)), np.ones((5, 5))]    # second one is wrong
    assert view.tryFastLiveUpdate(_Result(arrays), [0, 1]) is False

    assert a.data is first                         # untouched, not half-updated
    assert (a.refreshes, b.refreshes) == (0, 0)


def test_fast_update_logs_when_the_result_raises():
    """Positive control: the guard above is only meaningful if this logs."""
    view = _ViewStub(img_layer=_FakeLayer(np.zeros((2, 3))), anchor=True)

    assert view.tryFastLiveUpdate(_Result(layer_arrays=None), [0, 1]) is False
    assert len(view.debug_logs) == 1


# --- moveDimStep -----------------------------------------------------------

def test_move_dim_step_sets_the_slider_and_logs_nothing():
    dims = _FakeDims(ndim=3, hi=9)
    view = _ViewStub(dims=dims)

    view.moveDimStep(2, 4)

    assert dims.steps == {2: 4}
    assert view.debug_logs == []


@pytest.mark.parametrize("step, expected", [(-5, 0), (99, 9), (0, 0), (9, 9)])
def test_move_dim_step_clamps_into_range(step, expected):
    dims = _FakeDims(ndim=3, hi=9)
    view = _ViewStub(dims=dims)

    view.moveDimStep(2, step)

    assert dims.steps == {2: expected}
    assert view.debug_logs == []


def test_move_dim_step_ignores_an_axis_outside_the_viewer():
    dims = _FakeDims(ndim=3)
    view = _ViewStub(dims=dims)

    view.moveDimStep(7, 1)

    assert dims.steps == {}          # no axis 7 -> nothing set
    assert view.debug_logs == []     # and not an error either


def test_move_dim_step_logs_when_the_viewer_raises():
    """Positive control for the silent-degradation guards above."""
    dims = _FakeDims()
    dims.set_current_step = MagicMock(side_effect=RuntimeError("no viewer"))
    view = _ViewStub(dims=dims)

    view.moveDimStep(1, 1)

    assert len(view.debug_logs) == 1


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))


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
