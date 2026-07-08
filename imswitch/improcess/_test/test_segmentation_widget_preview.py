"""Live preview feature for the SegmentationWidget."""

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

    def remove(self, layer):
        if layer in self:
            super().remove(layer)


class _FakeViewer:
    def __init__(self, layer, current_step=None):
        self.layers = _FakeLayerList([layer], active=layer)
        self.dims = SimpleNamespace(current_step=current_step or ())
        self.added_labels = []
        self.added_images = []

    def add_labels(self, data, **kwargs):
        label_obj = SimpleNamespace(
            data=np.asarray(data),
            scale=kwargs.get("scale", [1.0, 1.0]),
            name=kwargs.get("name", "labels"),
            metadata=kwargs.get("metadata", {}),
            opacity=kwargs.get("opacity", 1.0),
        )
        self.layers.append(label_obj)
        self.added_labels.append((np.asarray(data), kwargs))

    def add_image(self, data, **kwargs):
        image_obj = SimpleNamespace(
            data=np.asarray(data),
            scale=kwargs.get("scale", [1.0, 1.0]),
            name=kwargs.get("name", "image"),
            metadata=kwargs.get("metadata", {}),
            opacity=kwargs.get("opacity", 1.0),
            colormap=kwargs.get("colormap"),
            blending=kwargs.get("blending"),
        )
        self.layers.append(image_obj)
        self.added_images.append((np.asarray(data), kwargs))


class _Combo:
    def __init__(self, text):
        self._text = text

    def currentText(self):
        return self._text

    def setText(self, text):
        self._text = text

    def addItems(self, items):
        pass


class _Spin:
    def __init__(self, value):
        self._value = value
        self._signals = []

    def value(self):
        return self._value

    def valueChanged(self):
        return self

    def connect(self, fn):
        self._signals.append(fn)


class _Check:
    def __init__(self, checked=False):
        self._checked = checked
        self._signals = []
        self._toggled_signals = []

    def isChecked(self):
        return self._checked

    def setChecked(self, checked):
        self._checked = checked
        for fn in self._toggled_signals:
            fn(checked)

    def toggled(self):
        return self

    def connect(self, fn):
        self._signals.append(fn)
        self._toggled_signals.append(fn)
        return self


class _Summary:
    def __init__(self):
        self.text = ""

    def setText(self, text):
        self.text = str(text)


class _Timer:
    def __init__(self):
        self._timeout_fn = None
        self._running = False
        self._interval = 0
        self._single_shot = False

    def setSingleShot(self, single_shot):
        self._single_shot = single_shot

    def setInterval(self, ms):
        self._interval = ms

    def timeout(self):
        return self

    def connect(self, fn):
        self._timeout_fn = fn

    def start(self):
        self._running = True

    def stop(self):
        self._running = False

    def fire(self):
        """Test helper to manually fire the timer."""
        if self._timeout_fn:
            self._timeout_fn()


def _widget_for_viewer(viewer):
    widget = SegmentationWidget.__new__(SegmentationWidget)
    widget._viewer = viewer
    widget._last_analysis = None
    widget._preview_layer_name = "Segmentation preview"
    widget._dims_connection = None
    widget._layer_selection_connection = None
    
    widget.methodCombo = _Combo("manual")
    widget.methodCombo.addItems(["otsu", "manual", "triangle", "yen", "local", "watershed"])
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
    widget.previewCheck = _Check(False)
    widget.previewModeCombo = _Combo("Segmentation labels")
    widget.summaryLabel = _Summary()
    
    widget._preview_timer = _Timer()
    widget._preview_timer.setSingleShot(True)
    widget._preview_timer.setInterval(350)
    widget._preview_timer.connect(widget._update_preview)
    
    return widget


def test_preview_creates_single_layer():
    """Toggling preview on creates exactly one preview layer."""
    data = np.zeros((8, 9), dtype=np.float32)
    data[2:5, 3:7] = 10.0
    layer = _FakeLayer(data, scale=[0.25, 0.5])
    viewer = _FakeViewer(layer)
    widget = _widget_for_viewer(viewer)
    
    initial_layer_count = len(viewer.layers)
    widget.previewCheck.setChecked(True)
    widget._update_preview()
    
    assert len(viewer.layers) == initial_layer_count + 1
    preview_layer = viewer.layers[-1]
    assert preview_layer.name == "Segmentation preview"
    assert preview_layer.opacity == 0.5
    assert viewer.added_labels
    assert not viewer.added_images


def test_binarization_preview_creates_mask_image_layer():
    """Mask mode previews the binary threshold mask instead of labels."""
    data = np.zeros((8, 9), dtype=np.float32)
    data[2:5, 3:7] = 10.0
    layer = _FakeLayer(data, scale=[0.25, 0.5])
    viewer = _FakeViewer(layer)
    widget = _widget_for_viewer(viewer)

    widget.previewModeCombo.setText("Binarization mask")
    widget.previewCheck.setChecked(True)
    widget._update_preview()

    assert len(viewer.layers) == 2
    preview_layer = viewer.layers[-1]
    assert preview_layer.name == "Binarization preview"
    assert preview_layer.opacity == 0.45
    assert preview_layer.colormap == "green"
    assert preview_layer.metadata["preview_mode"] == "mask"
    assert preview_layer.data.dtype == np.uint8
    assert int(preview_layer.data.sum()) == 12
    assert viewer.added_images
    assert not viewer.added_labels
    assert "Preview mask:" in widget.summaryLabel.text


def test_preview_reuses_same_layer():
    """Changing parameters and firing the timer reuses the same preview layer."""
    data = np.zeros((8, 9), dtype=np.float32)
    data[2:5, 3:7] = 10.0
    layer = _FakeLayer(data, scale=[0.25, 0.5])
    viewer = _FakeViewer(layer)
    widget = _widget_for_viewer(viewer)
    
    widget.previewCheck.setChecked(True)
    widget._update_preview()
    layer_count_after_first = len(viewer.layers)
    preview_layer = viewer.layers[-1]
    first_data_id = id(preview_layer.data)
    
    # Change a parameter and fire the timer
    widget.thresholdSpin._value = 0.7
    widget._preview_timer.fire()
    
    # Should still be the same layer count
    assert len(viewer.layers) == layer_count_after_first
    assert viewer.layers[-1] is preview_layer
    # Data should be updated (different object)
    assert id(preview_layer.data) != first_data_id


def test_preview_toggle_off_removes_layer():
    """Toggling preview off removes the preview layer."""
    data = np.zeros((8, 9), dtype=np.float32)
    data[2:5, 3:7] = 10.0
    layer = _FakeLayer(data, scale=[0.25, 0.5])
    viewer = _FakeViewer(layer)
    widget = _widget_for_viewer(viewer)
    
    widget.previewCheck.setChecked(True)
    widget._update_preview()
    assert len(viewer.layers) == 2  # image + preview
    
    # Toggle off
    widget._on_preview_toggled(False)
    assert len(viewer.layers) == 1  # only image
    
    # Verify preview layer is gone
    for layer in viewer.layers:
        assert getattr(layer, "name", "") != "Segmentation preview"


def test_preview_with_no_image_layer():
    """Preview with no image layer shows friendly summary text, no exception."""
    viewer = _FakeViewer(None)
    viewer.layers = _FakeLayerList([], active=None)
    widget = _widget_for_viewer(viewer)
    
    widget.previewCheck.setChecked(True)
    widget._update_preview()
    
    assert "Preview: no image layer selected" in widget.summaryLabel.text


def test_run_commits_independent_layer():
    """run() still commits a new layer independent of preview state."""
    data = np.zeros((8, 9), dtype=np.float32)
    data[2:5, 3:7] = 10.0
    layer = _FakeLayer(data, scale=[0.25, 0.5])
    viewer = _FakeViewer(layer)
    widget = _widget_for_viewer(viewer)
    
    # Turn on preview
    widget.previewCheck.setChecked(True)
    widget._update_preview()
    layer_count_with_preview = len(viewer.layers)
    
    # Run a commit
    widget.run()
    
    # Should have both preview and committed layer
    assert len(viewer.layers) == layer_count_with_preview + 1
    
    # Last layer should be the committed one (not "Segmentation preview")
    committed_layer = viewer.layers[-1]
    assert committed_layer.name != "Segmentation preview"
    assert "region" in committed_layer.name.lower()


def test_preview_updates_summary_label():
    """Preview updates summary label with threshold and region count."""
    data = np.zeros((8, 9), dtype=np.float32)
    data[2:5, 3:7] = 10.0
    layer = _FakeLayer(data, scale=[0.25, 0.5])
    viewer = _FakeViewer(layer)
    widget = _widget_for_viewer(viewer)
    
    widget.previewCheck.setChecked(True)
    widget._update_preview()
    
    summary = widget.summaryLabel.text
    assert "Preview:" in summary
    assert "threshold" in summary
    assert "region" in summary


def test_preview_layer_never_becomes_its_own_source():
    """Even when the preview layer is napari's active layer (napari activates
    freshly added layers), the source resolution must skip it and keep
    segmenting the underlying image."""
    data = np.zeros((8, 9), dtype=np.float32)
    data[2:5, 3:7] = 10.0
    layer = _FakeLayer(data, scale=[0.25, 0.5])
    viewer = _FakeViewer(layer)
    widget = _widget_for_viewer(viewer)

    widget.previewCheck.setChecked(True)
    widget._update_preview()
    preview_layer = viewer.layers[-1]
    assert preview_layer.name == "Segmentation preview"

    # Simulate napari having made the preview layer active.
    viewer.layers.selection.active = preview_layer
    assert widget._active_image_layer() is layer

    widget._update_preview()
    # Still exactly one preview layer, recomputed from the image (2 layers total).
    assert len(viewer.layers) == 2
    assert viewer.layers[-1] is preview_layer


def test_switching_preview_modes_removes_inactive_preview_layer():
    data = np.zeros((8, 9), dtype=np.float32)
    data[2:5, 3:7] = 10.0
    layer = _FakeLayer(data, scale=[0.25, 0.5])
    viewer = _FakeViewer(layer)
    widget = _widget_for_viewer(viewer)

    widget.previewCheck.setChecked(True)
    widget._update_preview()
    assert viewer.layers[-1].name == "Segmentation preview"

    widget.previewModeCombo.setText("Binarization mask")
    widget._update_preview()

    assert len(viewer.layers) == 2
    assert viewer.layers[-1].name == "Binarization preview"
    assert all(
        getattr(layer, "name", "") != "Segmentation preview"
        for layer in viewer.layers
    )


def test_schedule_preview_only_when_checked():
    """_schedule_preview only starts the timer when preview is checked."""
    data = np.zeros((8, 9), dtype=np.float32)
    data[2:5, 3:7] = 10.0
    layer = _FakeLayer(data, scale=[0.25, 0.5])
    viewer = _FakeViewer(layer)
    widget = _widget_for_viewer(viewer)
    
    # Preview unchecked
    widget._schedule_preview()
    assert not widget._preview_timer._running
    
    # Preview checked
    widget.previewCheck.setChecked(True)
    widget._schedule_preview()
    assert widget._preview_timer._running
