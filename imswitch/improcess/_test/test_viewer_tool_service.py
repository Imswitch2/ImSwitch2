"""P-T: the viewer tool broker (see docs/design/plans/improcess-roi-manager-2-0.md).

Covers D-04 (duplicate "Viewer Tools" layers), D-14 (panels erasing each
other's scratch shapes) and D-15 (viewer callbacks that outlive their panel).
"""

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("qtpy")
from qtpy import QtWidgets  # noqa: E402

from imswitch.imcommon.view.guitools.naparitools import ViewerToolManager  # noqa: E402
from imswitch.imcommon.view.guitools.viewer_tools import (  # noqa: E402
    StaleToolToken,
    ViewerToolService,
)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class _Event:
    def __init__(self):
        self._handlers = []

    def connect(self, fn):
        self._handlers.append(fn)

    def emit(self, *args):
        for fn in list(self._handlers):
            fn(*args)


class _Shapes:
    def __init__(self, **kwargs):
        self.name = kwargs.get("name", "Viewer Tools")
        self._data = []
        self.shape_type = []
        self.mode = "pan_zoom"
        self.visible = True
        self.edge_width = 1.0
        self.current_edge_width = 1.0
        self.events = SimpleNamespace(data=_Event(), mode=_Event())

    @property
    def data(self):
        return list(self._data)

    @data.setter
    def data(self, value):
        value = list(value)
        # Keep shape_type aligned the way napari does.
        self.shape_type = self.shape_type[: len(value)]
        self._data = value
        self.events.data.emit(SimpleNamespace(value=value))

    def add_shape(self, vertices, shape_type):
        """Test helper: draw a shape, mirroring what napari would emit."""
        self._data.append(np.asarray(vertices, dtype=float))
        self.shape_type.append(shape_type)
        self.events.data.emit(SimpleNamespace(value=self._data))


class _Viewer:
    def __init__(self):
        self.layers = []
        self.camera = SimpleNamespace(zoom=1.0, events=SimpleNamespace(zoom=_Event()))
        self.dims = SimpleNamespace(
            events=SimpleNamespace(current_step=_Event()), ndisplay=2, current_step=(0, 0)
        )

    def add_shapes(self, **kwargs):
        layer = _Shapes(**kwargs)
        self.layers.append(layer)
        return layer


def _rect(r0, c0, r1, c1):
    return [[r0, c0], [r0, c1], [r1, c1], [r1, c0]]


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# --------------------------------------------------------------------------
# D-04 — one layer per viewer
# --------------------------------------------------------------------------

def test_two_managers_share_one_viewer_tools_layer(qapp):
    """A second manager must adopt the existing layer, not add another."""
    viewer = _Viewer()

    first = ViewerToolManager(viewer)
    first.set_mode("rectangle")
    second = ViewerToolManager(viewer)
    second.set_mode("line")

    names = [layer.name for layer in viewer.layers]
    assert names == ["Viewer Tools"], f"duplicate tool layers: {names}"


def test_service_is_one_per_viewer(qapp):
    viewer = _Viewer()

    assert ViewerToolService.for_viewer(viewer) is ViewerToolService.for_viewer(viewer)


# --------------------------------------------------------------------------
# D-14 — panels must not erase each other's shapes
# --------------------------------------------------------------------------

def test_clearing_one_owner_keeps_another_owners_shapes(qapp):
    """The regression that made sharing a layer dangerous in the first place."""
    viewer = _Viewer()
    service = ViewerToolService(viewer)

    stats = service.acquire("roi-stats", "rectangle")
    layer = service.manager.get_layer()
    layer.add_shape(_rect(0, 0, 4, 4), "rectangle")
    service.claim_new_shapes(stats)

    profile = service.acquire("profile", "line")
    layer.add_shape([[0, 0], [5, 5]], "line")
    service.claim_new_shapes(profile)

    service.clear(profile)  # Profile switching mode used to wipe everything

    assert len(service.manager.get_shapes_data()) == 1
    assert service.manager.get_shape_types() == ["rectangle"]
    assert len(service.shapes(stats)) == 1


def test_owner_only_sees_its_own_shapes(qapp):
    viewer = _Viewer()
    service = ViewerToolService(viewer)

    stats = service.acquire("roi-stats", "rectangle")
    layer = service.manager.get_layer()
    layer.add_shape(_rect(0, 0, 4, 4), "rectangle")
    service.claim_new_shapes(stats)

    profile = service.acquire("profile", "rectangle")
    layer.add_shape(_rect(6, 6, 9, 9), "rectangle")
    service.claim_new_shapes(profile)

    assert len(service.shapes(stats)) == 1
    assert len(service.shapes(profile)) == 1
    assert service.shapes(stats)[0][0] != service.shapes(profile)[0][0]


def test_single_shape_limit_is_per_owner_not_global(qapp):
    """Each panel keeps its newest rectangle; other panels are unaffected."""
    viewer = _Viewer()
    service = ViewerToolService(viewer)

    stats = service.acquire("roi-stats", "rectangle")
    layer = service.manager.get_layer()
    layer.add_shape(_rect(0, 0, 4, 4), "rectangle")

    profile = service.acquire("profile", "rectangle")
    layer.add_shape(_rect(6, 6, 9, 9), "rectangle")
    layer.add_shape(_rect(10, 10, 12, 12), "rectangle")

    # Profile is down to its newest rectangle...
    assert len(service.shapes(profile)) == 1
    # ...and ROI statistics still has its own.
    assert len(service.shapes(stats)) == 1
    assert len(service.manager.get_shapes_data()) == 2


# --------------------------------------------------------------------------
# token semantics
# --------------------------------------------------------------------------

def test_stale_token_raises_instead_of_touching_another_owners_shapes(qapp):
    viewer = _Viewer()
    service = ViewerToolService(viewer)

    old = service.acquire("roi-stats", "rectangle")
    service.acquire("roi-stats", "rectangle")  # supersedes `old`

    with pytest.raises(StaleToolToken):
        service.clear(old)


def test_reacquiring_uses_a_stable_owner_key(qapp):
    """A reopened panel reclaims its own shapes rather than orphaning them."""
    viewer = _Viewer()
    service = ViewerToolService(viewer)

    first = service.acquire("roi-stats", "rectangle")
    layer = service.manager.get_layer()
    layer.add_shape(_rect(0, 0, 4, 4), "rectangle")
    service.claim_new_shapes(first)

    reopened = service.acquire("roi-stats")

    assert len(service.shapes(reopened)) == 1


def test_preemption_notifies_but_does_not_delete(qapp):
    viewer = _Viewer()
    service = ViewerToolService(viewer)
    preempted = []
    service.sigToolPreempted.connect(preempted.append)

    stats = service.acquire("roi-stats", "rectangle")
    layer = service.manager.get_layer()
    layer.add_shape(_rect(0, 0, 4, 4), "rectangle")
    service.claim_new_shapes(stats)

    service.acquire("profile", "line")

    assert preempted == ["roi-stats"]
    assert len(service.manager.get_shapes_data()) == 1


# --------------------------------------------------------------------------
# D-15 — callbacks must not outlive their panel
# --------------------------------------------------------------------------

def test_release_disconnects_callbacks(qapp):
    viewer = _Viewer()
    service = ViewerToolService(viewer)
    token = service.acquire("roi-stats", "rectangle")
    calls = []

    service.add_callback(token, service.sigShapesChanged, lambda: calls.append(1))
    service.manager.get_layer().add_shape(_rect(0, 0, 2, 2), "rectangle")
    assert calls, "handler should fire while the panel is alive"

    service.release(token)
    calls.clear()
    service.manager.get_layer().add_shape(_rect(3, 3, 5, 5), "rectangle")

    assert calls == [], "handler kept firing after release"


def test_release_is_idempotent_and_tolerates_stale_tokens(qapp):
    viewer = _Viewer()
    service = ViewerToolService(viewer)
    token = service.acquire("roi-stats", "rectangle")

    service.release(token)
    service.release(token)  # must not raise


def test_release_drops_that_owners_shapes_only(qapp):
    viewer = _Viewer()
    service = ViewerToolService(viewer)

    stats = service.acquire("roi-stats", "rectangle")
    layer = service.manager.get_layer()
    layer.add_shape(_rect(0, 0, 4, 4), "rectangle")
    service.claim_new_shapes(stats)

    profile = service.acquire("profile", "line")
    layer.add_shape([[0, 0], [5, 5]], "line")
    service.claim_new_shapes(profile)

    service.release(profile)

    assert service.manager.get_shape_types() == ["rectangle"]
    assert len(service.shapes(stats)) == 1


# --------------------------------------------------------------------------
# target layer
# --------------------------------------------------------------------------

def test_target_image_layer_is_shared_and_announced(qapp):
    viewer = _Viewer()
    service = ViewerToolService(viewer)
    seen = []
    service.sigTargetLayerChanged.connect(seen.append)
    layer = object()

    service.target_image_layer = layer

    assert service.target_image_layer is layer
    assert seen == [layer]
    service.target_image_layer = layer  # no duplicate notification
    assert seen == [layer]
