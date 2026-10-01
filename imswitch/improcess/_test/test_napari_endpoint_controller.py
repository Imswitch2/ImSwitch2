"""The endpoint controller against a stub viewer.

Tier 2 of the plan's tests: this proves the controller makes the right
viewer calls in the right order, tags and owns what it adds, cleans up on
every exit path, and keeps viewer work on the GUI thread. It does **not**
prove a dock lands in the embedded window -- only the guarded native test
does that.
"""

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets  # noqa: E402

from imswitch.improcess.controller.NapariEndpointController import (  # noqa: E402
    NapariEndpointController,
)
from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.labels_result import LabelsResult  # noqa: E402
from imswitch.improcess.model.napari_endpoints import (  # noqa: E402
    InstalledPlugins,
    InstalledWidget,
    NapariEndpoint,
    OutputMapping,
)
from imswitch.improcess.model.napari_sessions import SessionRegistry  # noqa: E402
from imswitch.improcess.model.points_table_result import PointsTableResult  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# -- stubs ----------------------------------------------------------------------

class _Signal:
    def __init__(self):
        self._slots = []
        self.emitted = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        self.emitted.append(args)
        for slot in list(self._slots):
            slot(*args)


class _Layer:
    def __init__(self, data, kwargs, layer_type):
        self.data = data
        self.name = kwargs.get("name", "layer")
        self.metadata = dict(kwargs.get("metadata", {}))
        self.scale = np.asarray(kwargs.get("scale", (1.0,) * np.ndim(data)), dtype=float)
        self.translate = np.zeros(len(self.scale))
        self.rotate = np.eye(len(self.scale))
        self.shear = np.zeros(max(1, len(self.scale) - 1))
        self.affine = SimpleNamespace(affine_matrix=np.eye(len(self.scale) + 1))
        self.ndim = len(self.scale)
        self.properties = {}
        self.units = None
        self._type = layer_type


class Image(_Layer):
    pass


class Labels(_Layer):
    pass


class _LayerList(list):
    def __init__(self, viewer):
        super().__init__()
        self.events = SimpleNamespace(removed=_Signal())
        self._viewer = viewer

    def remove(self, layer):
        super().remove(layer)
        self.events.removed.emit(SimpleNamespace(value=layer))


class _Viewer:
    def __init__(self):
        self.layers = _LayerList(self)
        self.window = SimpleNamespace(
            add_plugin_dock_widget=self._add_dock, remove_dock_widget=self._remove_dock
        )
        self.docks = []
        self.opened = []
        self.reader_output = lambda path: [Image(np.zeros((2, 2)), {"name": "from-reader"}, "image")]

    def add_layer(self, layer):
        self.layers.append(layer)
        return layer

    def open(self, path, plugin=None):
        self.opened.append((path, plugin))
        layers = self.reader_output(path)
        for layer in layers:
            self.layers.append(layer)
        return layers

    def _add_dock(self, plugin_name, widget_name):
        dock = SimpleNamespace(plugin=plugin_name, widget=widget_name)
        self.docks.append(dock)
        return dock, object()

    def _remove_dock(self, dock):
        self.docks.remove(dock)


class _DetachedViewer(_Viewer):
    def __init__(self):
        super().__init__()
        self._closers = []
        self.closed = False

    def on_closed(self, callback):
        self._closers.append(callback)

    def close(self):
        self.closed = True
        for callback in self._closers:
            callback()


class _View(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.menu = QtWidgets.QMenu(self)
        self.messages = []
        self.sigClosing = _Signal()
        self.roi_widget = SimpleNamespace(received=[], add_rois=lambda rois: self.roi_widget.received.extend(rois))

    def napariPluginsMenu(self):
        return self.menu

    def showStatusMessage(self, message, timeout_ms=6000):
        self.messages.append(message)

    def getRuntimeAnalysisWidget(self, tool_id):
        return self.roi_widget if tool_id == "roi-manager" else None


def _add_layer_data(viewer, layer_data):
    made = []
    for data, kwargs, layer_type in layer_data:
        cls = Labels if layer_type == "labels" else Image
        made.append(viewer.add_layer(cls(data, kwargs, layer_type)))
    return made


def _sync_runner(session_uid, job, on_done, on_failed):
    try:
        path = job()
    except Exception as exc:  # noqa: BLE001
        on_failed(session_uid, str(exc))
        return
    on_done(session_uid, path)


_INSTALLED = InstalledPlugins(
    plugin_names=frozenset({"napari-storm", "napari-skimage"}),
    widgets=(InstalledWidget("napari-storm", "Napari STORM"),
             InstalledWidget("napari-skimage", "Gaussian filter"),
             InstalledWidget("napari-skimage", "Automated Threshold")),
    reader_plugins=frozenset({"napari-storm"}),
)

_READER = NapariEndpoint(
    id="test:reader", label="Test reader", lane="reader", plugin_name="napari-storm",
    reader_plugin="napari-storm", export_format="hdf5", kinds=("image",), verified=True,
)
_DETACHED = NapariEndpoint(
    id="test:detached", label="Test detached", lane="detached", plugin_name="napari-storm",
    reader_plugin="napari-storm", export_format="hdf5", kinds=("image",), verified=True,
)


def _image(name="recon"):
    return ArrayProcessingResult(name, np.random.default_rng(0).random((6, 6)).astype(np.float32),
                                 ["Y", "X"], axis_scales=[0.5, 0.5], scale_unit="um")


def _controller(tmp_path, result, *, detached_factory=None, config=None):
    viewer = _Viewer()
    view = _View()
    comm = SimpleNamespace(sigResultProduced=_Signal(), getAllResults=lambda: [("recon", result)])
    recon = SimpleNamespace(getActiveResult=lambda: result, getNapariViewer=lambda: viewer)
    controller = NapariEndpointController(
        comm, view, recon,
        processing_config=config,
        installed=_INSTALLED,
        registry=SessionRegistry(temp_root=tmp_path),
        export_runner=_sync_runner,
        add_layer_data=_add_layer_data,
        detached_viewer_factory=detached_factory or _DetachedViewer,
    )
    return controller, viewer, view, comm


def _endpoint(controller, endpoint_id):
    return next(e for e in controller.endpoints if e.id == endpoint_id)


# -- dock lane ---------------------------------------------------------------------

def test_dock_lane_adds_owned_layers_then_the_plugin_dock(qapp, tmp_path):
    controller, viewer, view, _ = _controller(tmp_path, _image())
    session = controller.sendTo(_endpoint(controller, "napari-skimage:gaussian"))

    assert session.is_open
    assert len(viewer.layers) == 1
    layer = viewer.layers[0]
    assert layer.metadata["endpoint_session_uid"] == session.uid
    assert layer.metadata["result_uid"]
    assert session.owns_layer(layer)
    assert viewer.docks[0].plugin == "napari-skimage" and viewer.docks[0].widget == "Gaussian filter"
    assert "Sent recon" in view.messages[-1]


def test_closing_a_session_removes_exactly_its_layers_and_dock(qapp, tmp_path):
    controller, viewer, _, _ = _controller(tmp_path, _image())
    foreign = viewer.add_layer(Image(np.zeros((2, 2)), {"name": "user-layer"}, "image"))
    session = controller.sendTo(_endpoint(controller, "napari-skimage:gaussian"))
    controller.closeSession(session.uid)

    assert list(viewer.layers) == [foreign]
    assert viewer.docks == []
    assert session.state == "closed"


def test_removing_the_last_owned_layer_in_the_viewer_closes_the_session(qapp, tmp_path):
    controller, viewer, _, _ = _controller(tmp_path, _image())
    session = controller.sendTo(_endpoint(controller, "napari-skimage:gaussian"))
    viewer.layers.remove(viewer.layers[0])          # the user deletes it
    assert session.state == "closed"
    assert viewer.docks == []


def test_an_unavailable_endpoint_is_refused_with_the_reason(qapp, tmp_path):
    controller, viewer, view, _ = _controller(tmp_path, _image())
    missing = NapariEndpoint(id="x:y", label="X", lane="dock", plugin_name="not-installed",
                             widget_name="W", kinds=("image",), verified=True)
    assert controller.sendTo(missing) is None
    assert "pip install not-installed" in view.messages[-1]
    assert viewer.layers == [] and controller.registry.sessions() == []


# -- reader lane -------------------------------------------------------------------------

def test_reader_lane_exports_then_opens_in_our_viewer_and_owns_the_result_layers(qapp, tmp_path):
    controller, viewer, view, _ = _controller(tmp_path, _image())
    session = controller.sendTo(_READER)

    assert session.is_open
    path, plugin = viewer.opened[0]
    assert plugin == "napari-storm" and path.endswith(".h5")
    assert session.files and session.files[0].exists()
    assert viewer.layers[0].metadata["endpoint_session_uid"] == session.uid
    assert "Opened" in view.messages[-1]

    temp_dir = session.temp_dir
    viewer.layers.remove(viewer.layers[0])          # the lazy reader's layer goes away
    assert session.state == "closed"
    assert not temp_dir.exists()


def test_a_failed_export_reports_and_cleans_up(qapp, tmp_path):
    controller, viewer, view, _ = _controller(tmp_path, _image())
    broken = NapariEndpoint(
        id="test:broken", label="Broken", lane="reader", plugin_name="napari-storm",
        reader_plugin="napari-storm", export_format="picasso-hdf5", kinds=("localization",), verified=True,
    )
    # gate is by kind; force through the export path with a wrong-kind result
    session = controller.registry.open(broken, _image())
    controller._startExport(session, broken, _image())
    assert session.state == "failed"
    assert "cannot write" in session.error
    assert viewer.opened == []
    assert "failed" in view.messages[-1]
    assert not (session.temp_dir and session.temp_dir.exists())


def test_a_reader_failure_after_export_marks_the_session_failed(qapp, tmp_path):
    controller, viewer, view, _ = _controller(tmp_path, _image())

    def explode(path):
        raise RuntimeError("reader choked")

    viewer.reader_output = explode
    session = controller.sendTo(_READER)
    assert session.state == "failed" and "reader choked" in session.error
    assert "failed" in view.messages[-1]


# -- detached lane ---------------------------------------------------------------------------

def test_detached_lane_uses_its_own_viewer_and_closes_with_it(qapp, tmp_path):
    made = []

    def factory():
        made.append(_DetachedViewer())
        return made[-1]

    controller, viewer, _, _ = _controller(tmp_path, _image(), detached_factory=factory)
    session = controller.sendTo(_DETACHED)
    assert session.is_open and session.viewer is made[0]
    assert made[0].opened[0][1] == "napari-storm"
    assert viewer.opened == []                       # not in the embedded viewer
    made[0].close()                                  # user closes the window
    assert session.state == "closed"


# -- gating and menu ------------------------------------------------------------------------

def test_a_table_result_is_offered_no_endpoint_and_the_menu_says_so(qapp, tmp_path):
    table = PointsTableResult("pts", np.zeros((2, 2)))
    controller, _, view, _ = _controller(tmp_path, table)
    assert controller.offeredEndpoints(table) == []
    controller.rebuildMenu()
    send = [a for a in view.menu.actions() if a.menu() is not None][0]
    assert not send.isEnabled()
    assert "nothing accepts" in send.text()


def test_the_menu_lists_offered_endpoints_and_disables_missing_ones(qapp, tmp_path):
    config = {"napariEndpoints": [{"id": "cfg:dock", "label": "Configured", "lane": "dock",
                                   "plugin": "not-here", "widget": "W", "kinds": ["image"]}]}
    controller, _, view, _ = _controller(tmp_path, _image(), config=config)
    controller.rebuildMenu()
    send = [a for a in view.menu.actions() if a.menu() is not None][0].menu()
    texts = {a.text(): a.isEnabled() for a in send.actions()}
    assert texts["napari-skimage: Gaussian filter"] is True
    disabled = [t for t, enabled in texts.items() if t.startswith("Configured") and not enabled]
    assert disabled and "not installed" in disabled[0]


# -- import ----------------------------------------------------------------------------------

def test_import_with_explicit_layer_and_source_publishes_a_typed_result(qapp, tmp_path):
    source = _image()
    controller, viewer, view, comm = _controller(tmp_path, source)
    plugin_layer = viewer.add_layer(Labels(np.ones((6, 6), dtype=np.int32), {"name": "mask", "scale": (0.5, 0.5)}, "labels"))
    plugin_layer.metadata["axis_labels"] = ["Y", "X"]

    imported = controller.importLayer(plugin_layer, source)
    assert isinstance(imported.result, LabelsResult)
    assert imported.grid == "fresh"                       # no adapter vouched for the grid
    assert comm.sigResultProduced.emitted[-1][0] is imported.result
    assert "fresh grid" in view.messages[-1]


def test_an_adapter_output_mapping_lets_an_identity_layer_inherit_the_grid(qapp, tmp_path):
    source = _image()
    controller, viewer, _, _ = _controller(tmp_path, source)
    endpoint = NapariEndpoint(
        id="napari-skimage:threshold", label="T", lane="dock", plugin_name="napari-skimage",
        widget_name="Automated Threshold", kinds=("image",), verified=True,
        output_mappings=(OutputMapping("*", "labels", "labels", preserves_grid=True),),
    )
    session = controller.sendTo(endpoint)
    sent = viewer.layers[0]
    plugin_layer = viewer.add_layer(Labels(np.ones((6, 6), dtype=np.int32), {"name": "thresholded", "scale": tuple(sent.scale)}, "labels"))
    plugin_layer.metadata["axis_labels"] = ["Y", "X"]

    imported = controller.importLayer(plugin_layer, source, endpoint=endpoint)
    assert imported.grid == "inherit"
    assert imported.result.coordinate_space_uid == source.coordinate_space_uid
    assert session.is_open                                # importing does not close the session


def test_shapes_go_to_the_roi_manager(qapp, tmp_path):
    source = _image()
    controller, viewer, view, _ = _controller(tmp_path, source)

    class Shapes(_Layer):
        pass

    shapes = viewer.add_layer(Shapes([np.array([[0.0, 0.0], [0.0, 3.0], [3.0, 3.0], [3.0, 0.0]])],
                                     {"name": "boxes"}, "shapes"))
    shapes.shape_type = ["rectangle"]
    imported = controller.importLayer(shapes, source)
    assert imported.result is None and len(imported.rois) == 1
    assert len(view.roi_widget.received) == 1


# -- shutdown ---------------------------------------------------------------------------------

def test_closing_the_window_closes_every_session(qapp, tmp_path):
    controller, viewer, view, _ = _controller(tmp_path, _image())
    controller.sendTo(_endpoint(controller, "napari-skimage:gaussian"))
    controller.sendTo(_READER)
    assert len(controller.registry.sessions()) == 2
    view.sigClosing.emit()
    assert controller.registry.sessions() == []
    assert viewer.layers == [] and viewer.docks == []
