"""Native-platform smoke test: a plugin dock really lands in the embedded viewer.

This is the only test that constructs ``EmbeddedNapari``. It needs a real
Qt platform with OpenGL (the offscreen platform segfaults in vispy on
macOS), so it is skipped unless ``IMSWITCH_NATIVE_GUI_TESTS=1``. Run it by
hand on a workstation::

    IMSWITCH_NATIVE_GUI_TESTS=1 python -m pytest -p no:napari \
        imswitch/improcess/_test/test_napari_endpoints_native.py -s

Everything else about the endpoints is covered by the GUI-independent unit
tests and the stubbed controller test; this one exists so that "the dock is
inside our window" is something that has been observed, not assumed.
"""

import os
import sys

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("IMSWITCH_NATIVE_GUI_TESTS") != "1",
    reason="needs a native Qt platform with OpenGL; set IMSWITCH_NATIVE_GUI_TESTS=1",
)


@pytest.fixture
def fake_plugin():
    from npe2 import DynamicPlugin, PluginManager
    from qtpy import QtWidgets

    seen = {}

    class FakeDock(QtWidgets.QLabel):
        def __init__(self, napari_viewer, parent=None):
            super().__init__("fake dock", parent)
            seen["layers_at_open"] = [layer.name for layer in napari_viewer.layers]

    plugin = DynamicPlugin("fake-endpoint", plugin_manager=PluginManager.instance())
    plugin.contribute.widget(display_name="Fake Dock")(FakeDock)

    @plugin.contribute.reader(filename_patterns=["*.h5"])
    def fake_reader(path):
        def read(p):
            return [(np.full((8, 8), 7, dtype=np.uint16), {"name": "from-reader"}, "image")]
        return read

    plugin.register()
    yield seen
    try:
        PluginManager.instance().unregister(plugin.name)
    except Exception:
        pass


def test_dock_and_reader_lanes_inside_the_embedded_viewer(fake_plugin, tmp_path):
    from qtpy import QtWidgets

    from imswitch.imcommon.view.guitools import naparitools
    from imswitch.improcess.controller.NapariEndpointController import NapariEndpointController
    from imswitch.improcess.model.array_result import ArrayProcessingResult
    from imswitch.improcess.model.napari_endpoints import (
        NapariEndpoint, inspect_installed_plugins,
    )
    from imswitch.improcess.model.napari_sessions import SessionRegistry

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    viewer = naparitools.EmbeddedNapari()
    host = QtWidgets.QMainWindow()
    host.setCentralWidget(viewer.get_widget())
    host.show()

    result = ArrayProcessingResult("recon", np.random.rand(16, 16).astype(np.float32), ["Y", "X"],
                                   axis_scales=[0.1, 0.1], scale_unit="um")
    view = type("View", (), {})()
    view.messages = []
    view.showStatusMessage = lambda message, timeout_ms=6000: view.messages.append(message)
    recon = type("Recon", (), {})()
    recon.getActiveResult = lambda: result
    recon.getNapariViewer = lambda: viewer
    comm = type("Comm", (), {})()
    comm.getAllResults = lambda: [("recon", result)]

    def sync_runner(uid, job, on_done, on_failed):
        try:
            on_done(uid, job())
        except Exception as exc:  # noqa: BLE001
            on_failed(uid, str(exc))

    installed = inspect_installed_plugins()
    controller = NapariEndpointController(
        comm, view, recon, installed=installed,
        registry=SessionRegistry(temp_root=tmp_path), export_runner=sync_runner,
    )

    dock_endpoint = NapariEndpoint(id="fake:dock", label="Fake", lane="dock", plugin_name="fake-endpoint",
                                   widget_name="Fake Dock", kinds=("image",), verified=True)
    session = controller.sendTo(dock_endpoint)
    app.processEvents()
    assert session.is_open
    titles = [d.windowTitle() for d in viewer.window._qt_window.findChildren(QtWidgets.QDockWidget)]
    assert any("Fake Dock" in title for title in titles)
    assert "recon" in fake_plugin["layers_at_open"]

    reader_endpoint = NapariEndpoint(id="fake:reader", label="Fake reader", lane="reader",
                                     plugin_name="fake-endpoint", reader_plugin="fake-endpoint",
                                     export_format="hdf5", kinds=("image",), verified=True)
    reader_session = controller.sendTo(reader_endpoint)
    app.processEvents()
    assert reader_session.is_open
    assert any(layer.name == "from-reader" for layer in viewer.layers)

    controller.closeAll()
    app.processEvents()
    # napari's remove_dock_widget hides the dock; the controller also deletes
    # it, which takes effect once deferred deletes run.
    app.sendPostedEvents(None, 0)
    app.processEvents()
    visible_docks = [
        d.windowTitle() for d in viewer.window._qt_window.findChildren(QtWidgets.QDockWidget)
        if "Fake Dock" in d.windowTitle() and d.isVisible()
    ]
    assert visible_docks == []
    assert not any(layer.name == "from-reader" for layer in viewer.layers)
    viewer.close()
