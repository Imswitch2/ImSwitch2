"""Live preview feature for the SegmentationWidget."""

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


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
            translate=kwargs.get("translate", [0.0, 0.0]),
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
            translate=kwargs.get("translate", [0.0, 0.0]),
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


@pytest.fixture(autouse=True, scope="module")
def qapp():
    """The panel draws a threshold marker onto its histogram, and a
    pyqtgraph item is a real QGraphicsObject -- constructing one without an
    application segfaults rather than raising."""
    from qtpy import QtWidgets

    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _DataCombo:
    """A combo whose selection is its ``currentData`` rather than its text."""

    def __init__(self, data=None):
        self._items = [("", data)]
        self._index = 0
        self.enabled = True

    # -- what the widget drives it with
    def blockSignals(self, _blocked):
        pass

    def clear(self):
        self._items = []
        self._index = -1

    def addItem(self, text, data=None):
        self._items.append((text, data))
        if self._index < 0:
            self._index = 0

    def findData(self, data):
        for index, (_text, value) in enumerate(self._items):
            if value == data:
                return index
        return -1

    def setCurrentIndex(self, index):
        self._index = index

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)

    def setToolTip(self, _text):
        pass

    # -- what the tests read back
    def count(self):
        return len(self._items)

    def itemText(self, index):
        return self._items[index][0]

    def currentData(self):
        if 0 <= self._index < len(self._items):
            return self._items[self._index][1]
        return None


class _FakePlot:
    """Records what the histogram was asked to draw."""

    def __init__(self):
        self.curves = []
        self.items = []

    def clear(self):
        self.curves = []
        self.items = []

    def plot(self, *args, **kwargs):
        self.curves.append((args, kwargs))

    def addItem(self, item):
        self.items.append(item)


def _widget_for_viewer(viewer):
    widget = SegmentationWidget.__new__(SegmentationWidget)
    widget._viewer = viewer
    widget._last_analysis = None
    widget._currentResult = None
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
    widget.previewModeCombo = _Combo("Segmentation labels")
    widget.summaryLabel = _Summary()

    # The preview is a button now, so there is no checkbox and no debounce
    # timer to stand in for: calling _update_preview() IS pressing it.
    widget._roiManagerWidget = None
    widget._rois = []
    widget.roiCombo = _DataCombo(None)
    widget.roiModeCombo = _DataCombo("mask")
    widget.histogramPlot = _FakePlot()
    widget._thresholdLine = None
    widget._last_preview_threshold = None

    return widget


def test_preview_creates_single_layer():
    """Toggling preview on creates exactly one preview layer."""
    data = np.zeros((8, 9), dtype=np.float32)
    data[2:5, 3:7] = 10.0
    layer = _FakeLayer(data, scale=[0.25, 0.5])
    viewer = _FakeViewer(layer)
    widget = _widget_for_viewer(viewer)
    
    initial_layer_count = len(viewer.layers)
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
    
    widget._update_preview()
    layer_count_after_first = len(viewer.layers)
    preview_layer = viewer.layers[-1]
    first_data_id = id(preview_layer.data)
    
    # Change a parameter and fire the timer
    widget.thresholdSpin._value = 0.7
    widget._update_preview()          # pressing Preview a second time
    
    # Should still be the same layer count
    assert len(viewer.layers) == layer_count_after_first
    assert viewer.layers[-1] is preview_layer
    # Data should be updated (different object)
    assert id(preview_layer.data) != first_data_id


def test_hiding_the_preview_removes_the_layer():
    """Toggling preview off removes the preview layer."""
    data = np.zeros((8, 9), dtype=np.float32)
    data[2:5, 3:7] = 10.0
    layer = _FakeLayer(data, scale=[0.25, 0.5])
    viewer = _FakeViewer(layer)
    widget = _widget_for_viewer(viewer)
    
    widget._update_preview()
    assert len(viewer.layers) == 2  # image + preview
    
    # Toggle off
    widget._hide_preview()
    assert len(viewer.layers) == 1  # only image
    
    # Verify preview layer is gone
    for layer in viewer.layers:
        assert getattr(layer, "name", "") != "Segmentation preview"


def test_preview_with_no_image_layer():
    """Preview with no image layer shows friendly summary text, no exception."""
    viewer = _FakeViewer(None)
    viewer.layers = _FakeLayerList([], active=None)
    widget = _widget_for_viewer(viewer)
    
    widget._update_preview()
    
    assert "Preview: no image layer selected" in widget.summaryLabel.text


def test_run_commits_independent_layer():
    """run() now emits sigRunRequested instead of creating a layer directly."""
    from types import SimpleNamespace
    
    data = np.zeros((8, 9), dtype=np.float32)
    data[2:5, 3:7] = 10.0
    layer = _FakeLayer(data, scale=[0.25, 0.5])
    viewer = _FakeViewer(layer)
    widget = _widget_for_viewer(viewer)
    
    # Set a fake current result (new run() requires a current result)
    fake_result = SimpleNamespace(name="test_result", data=data)
    widget._currentResult = fake_result
    
    # Add signal tracking with a proper fake signal object
    class FakeSignal:
        def __init__(self):
            self.emitted = []
        def emit(self, result, params):
            self.emitted.append((result, params))
    
    widget.sigRunRequested = FakeSignal()
    
    # Turn on preview (should still work as before)
    widget._update_preview()
    layer_count_with_preview = len(viewer.layers)
    
    # Preview layer should exist
    assert any(layer.name == "Segmentation preview" for layer in viewer.layers)
    
    # Run a commit (should emit signal, not create layer)
    widget.run()
    
    # Preview layer should still be there (unchanged)
    assert any(layer.name == "Segmentation preview" for layer in viewer.layers)
    
    # Should have emitted sigRunRequested (but not created a committed layer)
    assert len(widget.sigRunRequested.emitted) == 1
    assert widget.sigRunRequested.emitted[0][0] is fake_result


def test_preview_updates_summary_label():
    """Preview updates summary label with threshold and region count."""
    data = np.zeros((8, 9), dtype=np.float32)
    data[2:5, 3:7] = 10.0
    layer = _FakeLayer(data, scale=[0.25, 0.5])
    viewer = _FakeViewer(layer)
    widget = _widget_for_viewer(viewer)
    
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


def test_changing_a_setting_does_not_recompute_on_its_own():
    """A click is the whole trigger. Segmentation over a large frame is not
    cheap, and a value should be typeable without the panel running under it."""
    data = np.zeros((8, 9), dtype=np.float32)
    data[2:5, 3:7] = 10.0
    viewer = _FakeViewer(_FakeLayer(data, scale=[1.0, 1.0]))
    widget = _widget_for_viewer(viewer)

    widget._update_preview()
    drawn = viewer.layers[-1].data

    widget.thresholdSpin._value = 0.9
    widget.minAreaSpin._value = 500

    assert viewer.layers[-1].data is drawn


def test_a_changed_view_drops_the_preview_rather_than_leaving_it():
    """Not merely stale: a labelling of one slice drawn over another."""
    data = np.zeros((8, 9), dtype=np.float32)
    data[2:5, 3:7] = 10.0
    viewer = _FakeViewer(_FakeLayer(data, scale=[1.0, 1.0]))
    widget = _widget_for_viewer(viewer)

    widget._update_preview()
    assert len(viewer.layers) == 2

    widget._invalidate_preview()

    assert len(viewer.layers) == 1
    assert "Preview cleared" in widget.summaryLabel.text


def test_invalidating_without_a_preview_says_nothing():
    """No preview on screen means nothing happened worth reporting."""
    viewer = _FakeViewer(_FakeLayer(np.zeros((8, 9), dtype=np.float32), scale=[1.0, 1.0]))
    widget = _widget_for_viewer(viewer)
    widget.summaryLabel.setText("untouched")

    widget._invalidate_preview()

    assert widget.summaryLabel.text == "untouched"


# --------------------------------------------------------------------------
# segmenting inside a region, and showing where the threshold landed
# --------------------------------------------------------------------------

def _two_population_image():
    """Dim signal on the left, bright signal on the right, dark elsewhere.

    A global threshold sits between the bright population and everything else,
    so the dim structure is missed -- which is exactly what restricting to a
    region is meant to fix.
    """
    image = np.zeros((16, 16), dtype=np.float32)
    image[4:8, 2:6] = 10.0        # dim
    image[4:8, 10:14] = 1000.0    # bright
    return image


def _roi(name, bounds, uid, roi_type="rectangle"):
    from imswitch.imcommon.algorithms.roi import ROIRecord

    return ROIRecord(name, roi_type, bounds, uid=uid)


class _ROIPanel:
    def __init__(self, rois):
        self._rois = list(rois)

    def rois(self):
        return list(self._rois)


def _widget_with_rois(viewer, rois):
    widget = _widget_for_viewer(viewer)
    widget._roiManagerWidget = _ROIPanel(rois)
    widget.methodCombo._text = "otsu"
    widget.refreshROIChoices()
    return widget


def test_only_rois_with_an_extent_are_offered():
    viewer = _FakeViewer(_FakeLayer(_two_population_image(), scale=[1.0, 1.0]))
    widget = _widget_with_rois(viewer, [
        _roi("left", (4, 8, 2, 6), "u1"),
        _roi("edge", (0, 1, 0, 5), "u2", roi_type="line"),
    ])

    labels = [widget.roiCombo.itemText(i) for i in range(widget.roiCombo.count())]
    assert labels == ["Whole image", "left (rectangle)"]


def test_the_threshold_comes_from_the_regions_own_pixels():
    """The point of restricting: nothing outside the region may influence the
    level chosen inside it. Changing pixels the ROI does not cover moves the
    whole-image threshold and must leave the region's alone."""
    layer = _FakeLayer(_two_population_image(), scale=[1.0, 1.0])
    viewer = _FakeViewer(layer)
    widget = _widget_with_rois(viewer, [_roi("left", (4, 8, 2, 6), "u1")])

    widget._update_preview()
    whole_before = widget._last_preview_threshold
    widget.roiCombo.setCurrentIndex(1)
    widget._update_preview()
    roi_before = widget._last_preview_threshold

    # Dim the bright structure, which lies entirely outside the ROI.
    changed = _two_population_image()
    changed[4:8, 10:14] = 40.0
    layer.data = changed

    widget._update_preview()
    roi_after = widget._last_preview_threshold
    widget.roiCombo.setCurrentIndex(0)
    widget._update_preview()
    whole_after = widget._last_preview_threshold

    assert roi_after == pytest.approx(roi_before)
    assert whole_after != pytest.approx(whole_before)
    assert roi_before != pytest.approx(whole_before)


def test_masked_pixels_do_not_join_the_histogram():
    """Zero-filling outside the region would add a large dark population and
    drag every automatic threshold down; NaN is ignored instead."""
    viewer = _FakeViewer(_FakeLayer(_two_population_image(), scale=[1.0, 1.0]))
    widget = _widget_with_rois(viewer, [_roi("left", (4, 8, 2, 6), "u1")])
    widget.roiCombo.setCurrentIndex(1)

    restricted, considered = widget._restrict_preview_image(_two_population_image())

    assert np.isnan(restricted[0, 0])              # outside the ROI
    assert restricted[5, 3] == 10.0                # inside it
    assert considered.sum() == 16                  # a 4x4 region


def test_the_histogram_shows_the_threshold_it_found():
    viewer = _FakeViewer(_FakeLayer(_two_population_image(), scale=[1.0, 1.0]))
    widget = _widget_with_rois(viewer, [])

    widget._update_preview()

    assert widget.histogramPlot.curves          # counts drawn
    assert widget._thresholdLine is not None    # and where the cut landed


def test_the_histogram_is_replaced_not_appended():
    """Two previews must not leave two sets of bars on one plot."""
    viewer = _FakeViewer(_FakeLayer(_two_population_image(), scale=[1.0, 1.0]))
    widget = _widget_with_rois(viewer, [])

    widget._update_preview()
    widget._update_preview()

    assert len(widget.histogramPlot.curves) == 1


def test_hiding_the_preview_clears_the_histogram():
    viewer = _FakeViewer(_FakeLayer(_two_population_image(), scale=[1.0, 1.0]))
    widget = _widget_with_rois(viewer, [])
    widget._update_preview()

    widget._hide_preview()

    assert widget.histogramPlot.curves == []
    assert widget._thresholdLine is None


def test_the_commit_carries_the_region_for_the_processor_to_apply():
    """The panel asks for the restriction the shared machinery already knows
    how to apply, rather than inventing a second way to crop."""
    from imswitch.improcess.analysis.roi_restriction import ROI_PARAM, ROIRestriction

    viewer = _FakeViewer(_FakeLayer(_two_population_image(), scale=[1.0, 1.0]))
    widget = _widget_with_rois(viewer, [_roi("left", (4, 8, 2, 6), "u1")])
    widget.prefixEdit = SimpleNamespace(text=lambda: "Seg")

    assert ROI_PARAM not in widget.parameterValues()

    widget.roiCombo.setCurrentIndex(1)
    restriction = widget.parameterValues()[ROI_PARAM]

    assert isinstance(restriction, ROIRestriction)
    assert restriction.mode == "mask"
    assert [roi.name for roi in restriction.rois] == ["left"]


def test_a_cropped_preview_is_drawn_where_the_region_is():
    """The overlay is a smaller array than the image it describes, so without
    a translate it lands at the origin -- labels for one part of the frame
    sitting over another."""
    layer = _FakeLayer(_two_population_image(), scale=[0.5, 0.25])
    viewer = _FakeViewer(layer)
    widget = _widget_with_rois(viewer, [_roi("left", (4, 8, 2, 6), "u1")])
    widget.roiModeCombo = _DataCombo("crop")
    widget.roiCombo.setCurrentIndex(1)

    widget._update_preview()

    preview = viewer.layers[-1]
    assert preview.data.shape == (4, 4)                 # cropped
    assert list(preview.translate) == [4 * 0.5, 2 * 0.25]


def test_an_uncropped_preview_is_not_displaced():
    layer = _FakeLayer(_two_population_image(), scale=[0.5, 0.25])
    viewer = _FakeViewer(layer)
    widget = _widget_with_rois(viewer, [])

    widget._update_preview()

    assert list(viewer.layers[-1].translate) == [0.0, 0.0]


def test_a_masked_preview_covers_the_whole_frame():
    """Mask mode keeps the frame, so it is aligned already."""
    layer = _FakeLayer(_two_population_image(), scale=[1.0, 1.0])
    viewer = _FakeViewer(layer)
    widget = _widget_with_rois(viewer, [_roi("left", (4, 8, 2, 6), "u1")])
    widget.roiCombo.setCurrentIndex(1)

    widget._update_preview()

    assert viewer.layers[-1].data.shape == (16, 16)
    assert list(viewer.layers[-1].translate) == [0.0, 0.0]
