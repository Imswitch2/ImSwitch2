from pathlib import Path
import sys
import types

import pytest


pytestmark = [pytest.mark.nohardware, pytest.mark.ui]


PROFILE_PATH = (
    Path(__file__).resolve().parent
    / "_data"
    / "user_defaults"
    / "imcontrol_setups"
    / "example_no_hardware.json"
)


def _install_gui_dependency_stubs(*, matplotlib=True):
    """Install minimal napari/vispy/matplotlib stubs for headless UI smoke.

    The smoke test verifies ImControl's own startup graph. It intentionally
    avoids importing the real napari stack because local/CI environments can
    have incompatible napari/pydantic or binary GUI dependencies.

    ``matplotlib=False`` leaves matplotlib and colour real and stubs only the
    napari/vispy canvas, which is what cannot start offscreen (no OpenGL
    context). The scripting-tutorial runner uses that: setups with a
    BeadRec widget need the real matplotlib through pyqtgraph's colour maps.
    """
    import numpy as np
    from qtpy import QtCore, QtWidgets

    class _Signal:
        def __init__(self):
            self.connected = []

        def connect(self, slot):
            self.connected.append(slot)

    class _Layer:
        def __init__(self, data, name='Layer', **kwargs):
            self.data = np.asarray(data)
            self.name = name
            self.scale = kwargs.get('scale', (1.0,) * self.data.ndim)
            self.blending = kwargs.get('blending', 'additive')
            self.colormap = types.SimpleNamespace(name=kwargs.get('colormap', 'grayclip'))
            self.contrast_limits = kwargs.get('contrast_limits', (0, 1))
            self.contrast_limits_range = self.contrast_limits
            self.protected = kwargs.get('protected', False)

    class _LayerList(list):
        def __init__(self):
            super().__init__()
            self.selection = []

        def _delitem_indices(self, key):
            return []

        def remove(self, layer):
            super().remove(layer)

        def move(self, old_index, new_index):
            layer = self.pop(old_index)
            self.insert(new_index, layer)

        def __contains__(self, item):
            if isinstance(item, str):
                return any(layer.name == item for layer in self)
            return super().__contains__(item)

        def __getitem__(self, item):
            if isinstance(item, str):
                for layer in self:
                    if layer.name == item:
                        return layer
                raise KeyError(item)
            return super().__getitem__(item)

    class _FakeNapariWindow:
        def __init__(self):
            self._qt_window = QtWidgets.QMainWindow()
            self.file_menu = self._qt_window.menuBar().addMenu('File')
            dock_layer_list = QtWidgets.QDockWidget('Layers')
            dock_layer_list.qt_area = QtCore.Qt.LeftDockWidgetArea
            self.qt_viewer = types.SimpleNamespace(
                dockLayerList=dock_layer_list,
                canvas=types.SimpleNamespace(view=types.SimpleNamespace(scene=None)),
                view=types.SimpleNamespace(scene=None),
            )

        def add_dock_widget(self, widget, name=None, area='left'):
            dock = QtWidgets.QDockWidget(name or widget.objectName())
            dock.setWidget(widget)
            self._qt_window.addDockWidget(QtCore.Qt.LeftDockWidgetArea, dock)
            return dock

    class _FakeViewer:
        def __init__(self, *args, show=False, **kwargs):
            self.layers = _LayerList()
            self.window = _FakeNapariWindow()
            self.scale_bar = types.SimpleNamespace(visible=False, unit=None)
            self.dims = types.SimpleNamespace(
                ndisplay=2,
                order=(0, 1),
                current_step=(0, 0),
                events=types.SimpleNamespace(ndisplay=_Signal()),
            )
            self.active_layer = types.SimpleNamespace(name='')

        def add_image(self, data, **kwargs):
            layer = _Layer(data, **kwargs)
            self.layers.append(layer)
            self.layers.selection = [layer]
            self.active_layer = layer
            return layer

        def reset_view(self):
            pass

    napari_module = types.ModuleType('napari')
    napari_module.Viewer = _FakeViewer
    utils_module = types.ModuleType('napari.utils')
    colormaps_module = types.ModuleType('napari.utils.colormaps')
    colormaps_module.AVAILABLE_COLORMAPS = {}
    utils_module.colormaps = colormaps_module
    utils_module.Colormap = lambda **kwargs: types.SimpleNamespace(**kwargs)
    translations_module = types.ModuleType('napari.utils.translations')
    translations_module.trans = types.SimpleNamespace(_=lambda text: text)
    utils_module.translations = translations_module
    napari_module.utils = utils_module

    vispy_module = types.ModuleType('vispy')
    vispy_color_module = types.ModuleType('vispy.color')
    vispy_color_module.Color = lambda color=None: types.SimpleNamespace(rgba=color)
    vispy_scene_module = types.ModuleType('vispy.scene')
    vispy_scene_visuals_module = types.ModuleType('vispy.scene.visuals')
    vispy_visuals_module = types.ModuleType('vispy.visuals')
    vispy_transforms_module = types.ModuleType('vispy.visuals.transforms')

    class _Visual:
        def __init__(self, *args, **kwargs):
            self.transform = None

        def attach(self, *args, **kwargs):
            pass

        def set_data(self, *args, **kwargs):
            pass

    vispy_scene_visuals_module.Compound = _Visual
    vispy_scene_visuals_module.Line = _Visual
    vispy_scene_visuals_module.Markers = _Visual
    vispy_transforms_module.STTransform = _Visual

    matplotlib_module = types.ModuleType('matplotlib')
    matplotlib_module.use = lambda *args, **kwargs: None
    matplotlib_backends_module = types.ModuleType('matplotlib.backends')
    matplotlib_qt_module = types.ModuleType('matplotlib.backends.backend_qt5agg')

    class _FigureCanvas(QtWidgets.QWidget):
        def __init__(self, *args, **kwargs):
            super().__init__()

        def draw(self):
            pass

    matplotlib_qt_module.FigureCanvasQTAgg = _FigureCanvas
    matplotlib_figure_module = types.ModuleType('matplotlib.figure')

    class _Axes:
        def __init__(self):
            label = types.SimpleNamespace(set_color=lambda *args, **kwargs: None)
            self.xaxis = types.SimpleNamespace(label=label)
            self.yaxis = types.SimpleNamespace(label=label)
            self.title = label
            self.spines = {
                'left': types.SimpleNamespace(set_color=lambda *args, **kwargs: None),
                'right': types.SimpleNamespace(set_color=lambda *args, **kwargs: None),
                'top': types.SimpleNamespace(set_color=lambda *args, **kwargs: None),
                'bottom': types.SimpleNamespace(set_color=lambda *args, **kwargs: None),
            }

        def cla(self):
            pass

        def clear(self):
            pass

        def set_facecolor(self, *args, **kwargs):
            pass

        def tick_params(self, *args, **kwargs):
            pass

        def grid(self, *args, **kwargs):
            pass

        def set_title(self, *args, **kwargs):
            pass

        def set_xlabel(self, *args, **kwargs):
            pass

        def set_ylabel(self, *args, **kwargs):
            pass

        def plot(self, *args, **kwargs):
            pass

        def legend(self, *args, **kwargs):
            pass

        def autoscale(self, *args, **kwargs):
            pass

    class _Figure:
        def __init__(self, *args, **kwargs):
            self.patch = types.SimpleNamespace(
                set_facecolor=lambda *args, **kwargs: None
            )

        def add_subplot(self, *args, **kwargs):
            return _Axes()

        def tight_layout(self, *args, **kwargs):
            pass

    matplotlib_figure_module.Figure = _Figure
    matplotlib_pyplot_module = types.ModuleType('matplotlib.pyplot')
    matplotlib_patches_module = types.ModuleType('matplotlib.patches')
    matplotlib_patches_module.Rectangle = object
    colour_module = types.ModuleType('colour')
    colour_module.wavelength_to_XYZ = lambda wavelength: np.array([1.0, 1.0, 1.0])
    colour_module.XYZ_to_sRGB = lambda xyz: np.asarray(xyz, dtype=float)

    stubs = {
        'napari': napari_module,
        'napari.utils': utils_module,
        'napari.utils.colormaps': colormaps_module,
        'napari.utils.translations': translations_module,
        'vispy': vispy_module,
        'vispy.color': vispy_color_module,
        'vispy.scene': vispy_scene_module,
        'vispy.scene.visuals': vispy_scene_visuals_module,
        'vispy.visuals': vispy_visuals_module,
        'vispy.visuals.transforms': vispy_transforms_module,
        'matplotlib': matplotlib_module,
        'matplotlib.backends': matplotlib_backends_module,
        'matplotlib.backends.backend_qt5agg': matplotlib_qt_module,
        'matplotlib.figure': matplotlib_figure_module,
        'matplotlib.pyplot': matplotlib_pyplot_module,
        'matplotlib.patches': matplotlib_patches_module,
        'colour': colour_module,
    }
    for name, module in stubs.items():
        if not matplotlib and name.split('.')[0] in ('matplotlib', 'colour'):
            continue
        sys.modules[name] = module


def test_no_hardware_profile_constructs_imcontrol_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _install_gui_dependency_stubs()

    from imswitch import imcontrol
    from imswitch.imcommon import prepareApp
    from imswitch.imcommon.model import dirtools
    from imswitch.imcontrol.model import Options
    from imswitch.imcontrol.view import ViewSetupInfo

    user_root = tmp_path / "home" / "ImSwitchConfig"
    monkeypatch.setattr(dirtools, "_baseUserFilesDir", user_root)
    monkeypatch.setattr(dirtools.UserFileDirs, "Root", str(user_root))
    monkeypatch.setattr(dirtools.UserFileDirs, "Config", str(user_root / "config"))
    assert Path(dirtools.UserFileDirs.Root).is_relative_to(tmp_path / "home")

    from imswitch.imcommon.controller import ModuleCommunicationChannel
    from imswitch.imcontrol.controller.ImConMainController import ImConMainController

    # view.close() below asks "Save the current widget state?" in a modal box
    # that nobody answers offscreen; the test hung there after passing.
    monkeypatch.setattr(ImConMainController, "_shouldSaveWidgetStateOnClose",
                        lambda self: False)

    view_setup_info = ViewSetupInfo.from_json(PROFILE_PATH.read_text(), infer_missing=True)
    options = Options(setupFileName=PROFILE_PATH.name)
    module_comm_channel = ModuleCommunicationChannel()
    module_comm_channel.register(imcontrol)

    app = prepareApp()
    view = None
    controller = None

    try:
        view, controller = imcontrol.getMainViewAndController(
            module_comm_channel,
            overrideSetupInfo=view_setup_info,
            overrideOptions=options,
        )
        app.processEvents()

        expected_widgets = {
            "Settings",
            "View",
            "Recording",
            "Image",
            "Positioner",
            "ViewerTools",
            "LineProfile",
        }
        assert expected_widgets.issubset(view.widgets.keys())
        assert expected_widgets.issubset(controller.controllers.keys())
        assert view.centralWidget() is not None
        assert view.centralWidget().layout().count() == 1
        assert view.docks
    finally:
        if view is not None:
            view.close()
            app.processEvents()
