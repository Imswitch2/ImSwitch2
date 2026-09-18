"""Each result remembers the contrast it was last shown with.

The scenario these exist for: two reconstructions of the same field whose SNR
differs, loaded side by side precisely so they can be compared. Set a contrast
on one, set a contrast on the other, click back -- and the first must come back
as it was left, not rescaled to its own data range again.
"""

from types import SimpleNamespace

import numpy as np

from imswitch.improcess.controller.ReconstructionViewController import (
    ReconstructionViewController,
)
from imswitch.improcess.model.result import DisplayLayerSpec, ProcessingResult
from imswitch.improcess.view import ReconstructionView as _view_module

ReconstructionView = _view_module.ReconstructionView


class _Result(ProcessingResult):
    def __init__(self, name, data, display_layers=None):
        data = np.asarray(data)
        super().__init__(
            name=name,
            data=data,
            axis_labels=["Y", "X"],
            axis_scales=[1.0] * data.ndim,
            scale_unit="px",
        )
        self.view_modes = [SimpleNamespace(name="standard", transpose=[0, 1])]
        self._layers = display_layers or []

    def display_layers(self):
        return list(self._layers)

    def save(self, path, fmt="tiff"):
        pass


class _Layer:
    """A napari image layer, as far as this controller is concerned."""

    def __init__(self, data, name="Reconstruction"):
        self.data = np.asarray(data)
        self.name = name
        self.metadata = {}
        self.colormap = "grayclip"
        self.visible = True
        self.contrast_limits = (0.0, 1.0)
        self.contrast_limits_range = (0.0, 1.0)


class _View:
    """Fake ReconstructionView with the real one's contrast plumbing.

    In particular it announces contrast changes the way the real view does,
    which is how the controller learns about a slider the user dragged.
    """

    def __init__(self):
        self.imgLayer = _Layer(np.zeros((1, 1)))
        self._items = []
        self._current = None
        self._levelsListeners = []
        self.napariViewer = SimpleNamespace(
            layers=SimpleNamespace(selection=SimpleNamespace(active=self.imgLayer)),
            dims=SimpleNamespace(axis_labels=()),
            scale_bar=SimpleNamespace(unit="px"),
        )
        self.sigImageLevelsChanged = SimpleNamespace(
            connect=self._levelsListeners.append
        )

    # -- contrast, which is what these tests are about ---------------------
    def _announceLevels(self, layer):
        for listener in self._levelsListeners:
            listener(dict(layer.metadata), tuple(layer.contrast_limits))

    def userDragsSlider(self, lo, hi, layer=None):
        layer = layer or self.imgLayer
        layer.contrast_limits = (lo, hi)
        self._announceLevels(layer)

    def setImageDisplayLevels(self, lo, hi):
        self.userDragsSlider(float(lo), float(hi))

    def setImageDisplayLevelsRange(self, lo, hi):
        self.imgLayer.contrast_limits_range = (float(lo), float(hi))

    def getImageDisplayLevels(self):
        return self.imgLayer.contrast_limits

    def getActiveImageLayer(self):
        return self.napariViewer.layers.selection.active or self.imgLayer

    def getActiveImageDisplayLevels(self):
        return self.getActiveImageLayer().contrast_limits

    def getActiveImageLayerMetadata(self):
        return dict(self.getActiveImageLayer().metadata)

    def getActiveImageColormap(self):
        return self.getActiveImageLayer().colormap

    def getImageLayerStates(self):
        states = []
        for layer in [self.imgLayer, *getattr(self, "_managed", [])]:
            component = layer.metadata.get("component")
            states.append({
                "id": component or layer.name,
                "name": layer.name,
                "visible": layer.visible,
                "colormap": layer.colormap,
                "display_levels": tuple(layer.contrast_limits),
                "metadata": dict(layer.metadata),
            })
        return states

    # -- rendering ---------------------------------------------------------
    def setImage(self, im, axisLabels, axisScales=None, scaleUnit="px",
                 colormap="grayclip", name=None, identity=None):
        self.imgLayer.name = str(name) if name else "Reconstruction"
        self.imgLayer.data = np.asarray(im)
        self.imgLayer.metadata["axis_labels"] = list(axisLabels)
        if name is not None:
            self.imgLayer.metadata["source_result"] = str(name)
        else:
            self.imgLayer.metadata.pop("source_result", None)
        self.imgLayer.metadata.pop("component", None)

    def setDisplayLayers(self, specs, identity=None):
        self._managed = []
        for index, spec in enumerate(specs):
            layer = self.imgLayer if index == 0 else _Layer(spec.data, spec.name)
            layer.name = spec.name
            layer.data = np.asarray(spec.data)
            layer.visible = bool(spec.visible)
            layer.colormap = spec.colormap
            layer.metadata.update(dict(spec.metadata or {}))
            if spec.display_levels is not None:
                layer.contrast_limits = tuple(spec.display_levels)
                self._announceLevels(layer)
            if index:
                self._managed.append(layer)

    def clearImage(self):
        self.imgLayer.data = np.zeros((1, 1))

    def getImage(self):
        return self.imgLayer.data

    def resetView(self):
        pass

    # -- the result list ---------------------------------------------------
    def add(self, result):
        self._items.append(result)
        self._current = len(self._items) - 1

    def select(self, index):
        self._current = index

    def remove(self, index):
        del self._items[index]
        self._current = min(self._current, len(self._items) - 1) or 0

    def getCurrentItemData(self):
        if self._current is None or not self._items:
            return None
        return self._items[self._current]

    def getCurrentItemIndex(self):
        return self._current

    def getDataAtIndex(self, index):
        if index is None or index < 0 or index >= len(self._items):
            return None
        return self._items[index]

    def getAllItemDatas(self):
        return [(r.name, r) for r in self._items]

    def getViewName(self):
        return "standard"

    def setViewModes(self, modes):
        pass


def _controller(view):
    controller = ReconstructionViewController.__new__(ReconstructionViewController)
    controller._widget = view
    controller._commChannel = SimpleNamespace(
        sigCurrentResultChanged=SimpleNamespace(emit=lambda result: None),
        sigResultsChanged=SimpleNamespace(emit=lambda: None),
    )
    controller._logger = SimpleNamespace(debug=lambda *a, **k: None,
                                         warning=lambda *a, **k: None)
    controller._transposeOrder = [0, 1]
    controller._displayedAxisLabels = []
    controller._axisStep = (0,) * 6
    controller._currItemInd = None
    controller._prevViewId = None
    controller._liveResultUids = set()
    view.sigImageLevelsChanged.connect(controller.imageLevelsChanged)
    return controller


def _dim(name, scale):
    return _Result(name, np.arange(20, dtype=np.float32).reshape(4, 5) * scale)


# --- the scenario -----------------------------------------------------------

def test_two_results_keep_their_own_contrast():
    view = _View()
    controller = _controller(view)
    a, b = _dim("a", 1.0), _dim("b", 10.0)

    view.add(a)
    controller.listItemChanged()
    view.userDragsSlider(3.0, 7.0)

    view.add(b)
    controller.listItemChanged()
    view.userDragsSlider(100.0, 150.0)

    view.select(0)
    controller.listItemChanged()
    assert tuple(view.imgLayer.contrast_limits) == (3.0, 7.0)

    view.select(1)
    controller.listItemChanged()
    assert tuple(view.imgLayer.contrast_limits) == (100.0, 150.0)


def test_a_result_shown_for_the_first_time_scales_to_its_own_data():
    """Inheriting the previous result's contrast is worst when it matters."""
    view = _View()
    controller = _controller(view)

    view.add(_dim("a", 1.0))
    controller.listItemChanged()
    view.userDragsSlider(3.0, 7.0)

    view.add(_dim("b", 10.0))
    controller.listItemChanged()

    # b spans 0..190, a spans 0..19: the levels must describe b, not the
    # (3.0, 7.0) that was set on a.
    low, high = view.imgLayer.contrast_limits
    assert 150.0 < high <= 190.0
    assert view.imgLayer.contrast_limits_range == (0.0, 190.0)


def test_contrast_survives_a_re_render_that_is_not_a_selection_change():
    """Updating a reconstruction, or switching view, must not reset it.

    These re-render without any selection moving, so a contrast only read back
    when the selection moves was lost every time one of them happened.
    """
    view = _View()
    controller = _controller(view)
    a = _dim("a", 1.0)

    view.add(a)
    controller.listItemChanged()
    view.userDragsSlider(3.0, 7.0)

    assert a.getDispLevels() == (3.0, 7.0)


def test_removing_a_result_does_not_move_another_result_contrast():
    """The previous result is held by reference; indices shift, objects do not."""
    view = _View()
    controller = _controller(view)
    a, b, c = _dim("a", 1.0), _dim("b", 10.0), _dim("c", 100.0)

    for result in (a, b, c):
        view.add(result)
        controller.listItemChanged()
    view.userDragsSlider(5.0, 6.0)          # c's contrast

    view.remove(0)                           # a goes; b and c shift down
    view.select(0)                           # b
    controller.listItemChanged()

    assert c.getDispLevels() == (5.0, 6.0)
    assert b.getDispLevels() != (5.0, 6.0)


def test_auto_levels_clip_outliers_like_a_display_layer_does():
    """Both render paths must scale the same picture the same way.

    A MoNaLISA reconstruction renders through the display-layer path, which
    clips 1% off each tail; a duplicate of it renders through the plain-image
    path. Spanning the full range in one and clipping in the other made the
    same data look like two different pictures depending which was clicked.
    """
    view = _View()
    controller = _controller(view)
    data = np.zeros((40, 40), dtype=np.float32)
    data[0, 0] = 1000.0          # one hot pixel, well outside the bulk
    data[1:, :] = 1.0

    view.add(_Result("spiky", data))
    controller.listItemChanged()

    low, high = view.imgLayer.contrast_limits
    assert high < 1000.0, "the hot pixel must not set the top of the scale"
    # ...while the slider still spans everything, so it can be dragged there.
    assert view.imgLayer.contrast_limits_range == (0.0, 1000.0)


def test_a_result_and_a_copy_of_it_open_at_the_same_contrast():
    view = _View()
    controller = _controller(view)
    data = np.linspace(0, 1, 400, dtype=np.float32).reshape(20, 20)

    view.add(_Result("original", data))
    controller.listItemChanged()
    first = tuple(view.imgLayer.contrast_limits)

    view.add(_Result("original (duplicate)", data.copy()))
    controller.listItemChanged()

    assert tuple(view.imgLayer.contrast_limits) == first


# --- results that render several layers -------------------------------------

def _layered(name, components):
    layers = [
        DisplayLayerSpec(
            name=f"{name}_{component}",
            data=np.full((4, 5), value, dtype=np.float32),
            axis_labels=["Y", "X"],
            display_levels=(0.0, float(value)),
            metadata={"source_result": name, "component": component},
            visible=(index == 0),
        )
        for index, (component, value) in enumerate(components)
    ]
    return _Result(name, np.zeros((4, 5), np.float32), display_layers=layers)


def test_every_layer_keeps_its_contrast_not_only_the_active_one():
    """Colormap and visibility were kept per layer; contrast was not.

    A MoNaLISA reconstruction renders one layer per base. Adjusting a layer
    that is not the active one -- which is what selecting it in napari's layer
    list and dragging its slider does -- left that layer rescaled to its own
    percentile range on every later visit.
    """
    view = _View()
    controller = _controller(view)
    result = _layered("recon", [("signal", 10.0), ("background", 20.0)])

    view.add(result)
    controller.listItemChanged()

    background = view._managed[0]
    view.userDragsSlider(1.0, 2.0, layer=background)

    adjusted = {
        layer.metadata["component"]: layer.display_levels
        for layer in result.applyDisplayLayerSettings(result.display_layers())
    }
    assert adjusted["background"] == (1.0, 2.0)


def test_layered_results_keep_their_contrast_across_a_switch():
    view = _View()
    controller = _controller(view)
    first = _layered("first", [("signal", 10.0)])
    second = _layered("second", [("signal", 1000.0)])

    view.add(first)
    controller.listItemChanged()
    view.userDragsSlider(1.0, 2.0)

    view.add(second)
    controller.listItemChanged()
    view.userDragsSlider(300.0, 400.0)

    view.select(0)
    controller.listItemChanged()
    assert tuple(view.imgLayer.contrast_limits) == (1.0, 2.0)

    view.select(1)
    controller.listItemChanged()
    assert tuple(view.imgLayer.contrast_limits) == (300.0, 400.0)


def test_a_plain_result_does_not_inherit_a_component_from_the_last_one():
    """Otherwise its levels are filed under a component it does not have."""
    view = _View()
    controller = _controller(view)
    layered = _layered("recon", [("signal", 10.0)])
    plain = _dim("plain", 1.0)

    view.add(layered)
    controller.listItemChanged()
    view.add(plain)
    controller.listItemChanged()
    view.userDragsSlider(3.0, 7.0)

    assert plain.getDispLevels() == (3.0, 7.0)
    assert plain.displayLayerSettings() == {}


# --- the view's side of the contract ----------------------------------------

def test_view_announces_a_layers_contrast_with_its_metadata():
    layer = _Layer(np.zeros((4, 5)), name="recon_signal")
    layer.metadata = {"source_result": "recon", "component": "signal"}
    layer.contrast_limits = (2.0, 8.0)
    seen = []
    stub = SimpleNamespace(
        sigImageLevelsChanged=SimpleNamespace(
            emit=lambda metadata, levels: seen.append((metadata, levels))
        )
    )

    ReconstructionView._onLevelsChanged(stub, layer)

    assert seen == [({"source_result": "recon", "component": "signal"}, (2.0, 8.0))]


def test_view_reports_every_layers_levels_not_only_the_active_one():
    primary = _Layer(np.zeros((4, 5)), name="recon_signal")
    primary.metadata = {"component": "signal"}
    primary.contrast_limits = (1.0, 2.0)
    secondary = _Layer(np.zeros((4, 5)), name="recon_background")
    secondary.metadata = {"component": "background"}
    secondary.contrast_limits = (3.0, 4.0)
    stub = SimpleNamespace(
        imgLayer=primary,
        _displayLayers=[secondary],
        _imageLayers=lambda: [primary, secondary],
        _imageLayerId=ReconstructionView._imageLayerId,
        _colormapName=ReconstructionView._colormapName,
    )

    states = ReconstructionView.getImageLayerStates(stub)

    assert [state["display_levels"] for state in states] == [(1.0, 2.0), (3.0, 4.0)]
