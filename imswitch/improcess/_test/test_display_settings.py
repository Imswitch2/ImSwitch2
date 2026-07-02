"""Tests for persistent image display settings."""

from types import SimpleNamespace

import numpy as np

from imswitch.improcess.controller.ReconstructionViewController import (
    ReconstructionViewController,
)
from imswitch.improcess.model.result import DisplayLayerSpec, ProcessingResult


class _Result(ProcessingResult):
    def __init__(self, name="source", display_layers=None):
        super().__init__(
            name=name,
            data=np.zeros((3, 4), dtype=np.float32),
            axis_labels=["Y", "X"],
        )
        self._display_layers = display_layers or []

    def display_layers(self):
        return list(self._display_layers)

    def save(self, path, fmt):
        pass


class _Widget:
    def __init__(self, result, *, metadata=None, layer_name="source"):
        self.result = result
        self.metadata = dict(metadata or {})
        self.layer_name = layer_name
        self.levels = None
        self.colormap = "grayclip"

    def getCurrentItemData(self):
        return self.result

    def setActiveImageDisplayLevels(self, minimum, maximum):
        self.levels = (float(minimum), float(maximum))

    def getActiveImageDisplayLevels(self):
        return self.levels

    def setActiveImageColormap(self, colormap):
        self.colormap = str(colormap)

    def getActiveImageColormap(self):
        return self.colormap

    def getActiveImageLayerMetadata(self):
        return dict(self.metadata)

    def getActiveImageLayerName(self):
        return self.layer_name


def _controller(widget):
    controller = ReconstructionViewController.__new__(ReconstructionViewController)
    controller._widget = widget
    controller._commChannel = SimpleNamespace()
    return controller


def test_primary_result_display_colormap_persists():
    result = _Result()
    widget = _Widget(result)
    controller = _controller(widget)

    controller.setActiveImageColormap("magma")
    controller.setActiveImageDisplayLevels(2.0, 8.0)

    assert result.getDisplayColormap() == "magma"
    assert result.getDispLevels() == (2.0, 8.0)


def test_display_layer_lut_and_levels_persist_by_component():
    layer = DisplayLayerSpec(
        name="source_signal",
        data=np.zeros((3, 4), dtype=np.float32),
        axis_labels=["Y", "X"],
        display_levels=(0.0, 1.0),
        colormap="grayclip",
        metadata={"source_result": "source", "component": "signal"},
    )
    result = _Result(display_layers=[layer])
    widget = _Widget(
        result,
        metadata={"source_result": "source", "component": "signal"},
        layer_name="source_signal",
    )
    controller = _controller(widget)

    controller.setActiveImageColormap("green")
    controller.setActiveImageDisplayLevels(3.0, 9.0)
    controller.setDisplayLayerVisible("signal", False)

    adjusted = result.applyDisplayLayerSettings(result.display_layers())
    assert adjusted[0].colormap == "green"
    assert adjusted[0].display_levels == (3.0, 9.0)
    assert adjusted[0].visible is False
    assert result.getDispLevels() is None
