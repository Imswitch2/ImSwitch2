"""ImageWidget.setStaticLayer, behind a script's show(): the same name
replaces the layer's data, a different dimensionality replaces the layer,
and a live-view layer is never touched."""
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.view.widgets.ImageWidget import ImageWidget


class _Layer(SimpleNamespace):
    def reset_contrast_limits(self):
        self.contrast_reset = True


class _Layers(list):
    def __contains__(self, name):
        return any(layer.name == name for layer in self)

    def __getitem__(self, key):
        if isinstance(key, str):
            return next(layer for layer in self if layer.name == key)
        return super().__getitem__(key)


def _widget():
    layers = _Layers()

    def add_image(data, **kwargs):
        layer = _Layer(data=data, ndim=data.ndim, contrast_limits=(0, 1), **kwargs)
        layers.append(layer)
        return layer

    widget = SimpleNamespace(napariViewer=SimpleNamespace(layers=layers, add_image=add_image))
    widget.addStaticLayer = lambda *a, **k: ImageWidget.addStaticLayer(widget, *a, **k)
    widget._fitScale = ImageWidget._fitScale
    widget._removeProtectedLayer = lambda layer: layers.remove(layer)
    return widget, layers


def test_the_same_name_replaces_the_data_and_fits_scale_and_contrast():
    widget, layers = _widget()
    assert ImageWidget.setStaticLayer(widget, 'mean', np.zeros((4, 4))) is True
    assert ImageWidget.setStaticLayer(widget, 'mean', np.ones((4, 4)), (0.1, 0.2)) is False
    (layer,) = layers
    assert layer.data.max() == 1 and layer.scale == (0.1, 0.2) and layer.contrast_reset


def test_another_dimensionality_replaces_the_layer():
    widget, layers = _widget()
    ImageWidget.setStaticLayer(widget, 'frames', np.zeros((4, 4)))
    assert ImageWidget.setStaticLayer(widget, 'frames', np.zeros((3, 4, 4)), (0.1, 0.1)) is True
    (layer,) = layers
    assert layer.data.ndim == 3 and layer.scale == (1.0, 0.1, 0.1)


def test_a_live_view_layer_is_never_replaced():
    widget, layers = _widget()
    layers.append(_Layer(name='Live: Camera', data=np.zeros((2, 2)), ndim=2, protected=True,
                         contrast_limits=(0, 1)))
    with pytest.raises(ValueError, match='live-view layer'):
        ImageWidget.setStaticLayer(widget, 'Live: Camera', np.ones((2, 2)))
    assert layers[0].data.max() == 0
