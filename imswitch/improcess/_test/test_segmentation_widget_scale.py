"""World-space scale handling in the interactive SegmentationWidget."""

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

# Direct import to avoid the view package import chain (matplotlib/napari).
_view_path = Path(__file__).parent.parent / "view"
sys.path.insert(0, str(_view_path))
try:
    from SegmentationWidget import SegmentationWidget
finally:
    sys.path.pop(0)


class _FakeLayer:
    def __init__(self, data, scale, name="image", metadata=None):
        self.data = data
        self.scale = scale
        self.name = name
        self.visible = True
        self.metadata = metadata or {}


class _FakeLayerList(list):
    def __init__(self, layers, active):
        super().__init__(layers)
        self.selection = SimpleNamespace(active=active)


class _FakeViewer:
    def __init__(self, layer, current_step=None):
        self.layers = _FakeLayerList([layer], active=layer)
        self.dims = SimpleNamespace(current_step=current_step or ())
        self.added_labels = []

    def add_labels(self, data, **kwargs):
        self.added_labels.append((np.asarray(data), kwargs))


class _Combo:
    def __init__(self, text):
        self._text = text

    def currentText(self):
        return self._text


class _Spin:
    def __init__(self, value):
        self._value = value

    def value(self):
        return self._value


class _Check:
    def __init__(self, checked=False):
        self._checked = checked

    def isChecked(self):
        return self._checked


class _Summary:
    def __init__(self):
        self.text = ""

    def setText(self, text):
        self.text = str(text)


def _widget_for_viewer(viewer):
    widget = SegmentationWidget.__new__(SegmentationWidget)
    widget._viewer = viewer
    widget._last_analysis = None
    widget.methodCombo = _Combo("manual")
    widget.thresholdSpin = _Spin(0.5)
    widget.minAreaSpin = _Spin(1)
    widget.smoothSpin = _Spin(0.0)
    widget.backgroundSpin = _Spin(0.0)
    widget.morphologySpin = _Spin(0)
    widget.fillHolesCheck = _Check(False)
    widget.clearBorderCheck = _Check(False)
    widget.localBlockSpin = _Spin(51)
    widget.localOffsetSpin = _Spin(0.0)
    widget.watershedDistanceSpin = _Spin(5)
    widget.summaryLabel = _Summary()
    return widget


def test_segmentation_labels_use_source_layer_spatial_scale():
    data = np.zeros((2, 8, 9), dtype=np.float32)
    data[1, 2:5, 3:7] = 10.0
    layer = _FakeLayer(
        data,
        scale=[4.0, 0.25, 0.5],
        metadata={"scale_unit": "um"},
    )
    viewer = _FakeViewer(layer, current_step=(1, 0, 0))
    widget = _widget_for_viewer(viewer)

    widget.run()

    assert len(viewer.added_labels) == 1
    labels, kwargs = viewer.added_labels[0]
    assert labels.shape == (8, 9)
    assert labels.max() == 1
    assert kwargs["scale"] == [0.25, 0.5]
    assert kwargs["metadata"]["axis_labels"] == ["Y", "X"]
    assert kwargs["metadata"]["scale_unit"] == "um"


def test_segmentation_label_scale_falls_back_to_unit_pixels():
    layer = _FakeLayer(np.ones((4, 5), dtype=np.float32), scale=[2.0])

    assert SegmentationWidget._spatial_layer_scale(layer) == [1.0, 1.0]
