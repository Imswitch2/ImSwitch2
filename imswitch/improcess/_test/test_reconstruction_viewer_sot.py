"""Tests for ReconstructionView and Controller single-source-of-truth changes.

These tests verify that:
1. setImage accepts an optional name parameter and sets layer name + metadata
2. clearImage resets name and metadata
3. The controller passes result.name to the view
4. The controller re-activates the main layer after setImage
5. Display settings persistence works correctly with the new naming
"""

from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.improcess.controller.ReconstructionViewController import (
    ReconstructionViewController,
)
from imswitch.improcess.model.result import ProcessingResult


# --- Fake objects for testing without real napari/Qt ---

class _FakeLayer:
    def __init__(self, data, name="Reconstruction", protected=False):
        self.data = np.asarray(data)
        self.name = name
        self.protected = protected
        self.metadata = {}
        self.scale = (1.0,) * self.data.ndim
        self.colormap = "grayclip"
        self.contrast_limits = (0.0, 1.0)
        self.contrast_limits_range = (0.0, 1.0)
        self.visible = True


class _FakeLayerList(list):
    def __init__(self, layers=None, active=None):
        super().__init__(layers or [])
        self.selection = SimpleNamespace(active=active or (layers[0] if layers else None))


class _FakeDims:
    def __init__(self):
        self.axis_labels = ()
        self.order = (0, 1)
        self.ndim = 2
        self.ndisplay = 2
        self.current_step = (0, 0)
        self.nsteps = (1, 1)
        self.range = ((0, 0, 1), (0, 0, 1))
        self.events = SimpleNamespace(connect=lambda x: None)

    def set_current_step(self, axis, step):
        current = list(self.current_step)
        current[axis] = int(step)
        self.current_step = tuple(current)


class _FakeScaleBar:
    def __init__(self):
        self.unit = "px"


class _FakeNapariViewer:
    def __init__(self, image_layer):
        self.layers = _FakeLayerList([image_layer], active=image_layer)
        self.dims = _FakeDims()
        self.scale_bar = _FakeScaleBar()
        self.camera = SimpleNamespace(center=(0.0, 0.0, 0.0), zoom=1.0)

    def add_image(self, data, **kwargs):
        layer = _FakeLayer(data, name=kwargs.get("name", "image"))
        layer.metadata = kwargs.get("metadata", {})
        layer.colormap = kwargs.get("colormap", "grayclip")
        self.layers.append(layer)
        return layer

    def reset_view(self):
        pass

    def get_widget(self):
        return SimpleNamespace()


class _FakeReconstructionView:
    """Minimal ReconstructionView fake for controller tests."""
    def __init__(self):
        self.imgLayer = _FakeLayer(np.zeros((1, 1)))
        self.napariViewer = _FakeNapariViewer(self.imgLayer)
        self._reconList = []
        self._currentIndex = None
        self._activations = []  # Track re-activations
        self.set_image_calls = 0
        self.clear_image_calls = 0
        self.reset_view_calls = 0
        self._viewName = "standard"
        self._lockSlicePerResult = False

    def setImage(self, im, axisLabels, axisScales=None, scaleUnit="px", colormap="grayclip",
                 name=None, identity=None):
        # Simplified version of the actual setImage logic
        self.set_image_calls += 1
        if name is not None:
            self.imgLayer.name = str(name)
            self.imgLayer.metadata["source_result"] = str(name)
        else:
            self.imgLayer.name = 'Reconstruction'
            self.imgLayer.metadata.pop("source_result", None)
        self.imgLayer.data = np.asarray(im)
        self.imgLayer.scale = tuple(axisScales if axisScales is not None else [1.0] * im.ndim)
        self.imgLayer.colormap = colormap
        self.imgLayer.metadata["axis_labels"] = list(axisLabels)
        self.imgLayer.metadata["scale_unit"] = scaleUnit
        for key, value in (identity or {}).items():
            self.imgLayer.metadata[key] = value
        dims = self.napariViewer.dims
        old_steps = tuple(dims.current_step)
        dims.axis_labels = tuple(axisLabels)
        dims.ndim = len(axisLabels)
        dims.nsteps = tuple(int(size) for size in np.shape(im))
        dims.range = tuple((0, max(0, size - 1), 1) for size in dims.nsteps)
        if len(dims.order) != len(axisLabels):
            dims.order = tuple(range(len(axisLabels)))
        dims.current_step = tuple(
            min(old_steps[i] if i < len(old_steps) else 0, max(0, size - 1))
            for i, size in enumerate(dims.nsteps)
        )

    def clearImage(self):
        self.clear_image_calls += 1
        self.imgLayer.name = 'Reconstruction'
        self.imgLayer.metadata.pop("source_result", None)
        self.imgLayer.data = np.zeros((1, 1))

    def setImageDisplayLevels(self, minimum, maximum):
        self.imgLayer.contrast_limits = (minimum, maximum)

    def getCurrentItemData(self):
        if self._currentIndex is not None and 0 <= self._currentIndex < len(self._reconList):
            return self._reconList[self._currentIndex]
        return None

    def getCurrentItemIndex(self):
        return self._currentIndex if self._currentIndex is not None else -1

    def getDataAtIndex(self, index):
        if 0 <= index < len(self._reconList):
            return self._reconList[index]
        return None

    def getViewName(self):
        return self._viewName

    def setViewModes(self, modes, selectedName=None):
        self._viewModes = list(modes or [])
        if selectedName is not None:
            self._viewName = selectedName
        elif modes:
            self._viewName = modes[0].name
        else:
            self._viewName = "standard"

    def isSliceLockedPerResult(self):
        return self._lockSlicePerResult

    def getDimensionStepState(self):
        dims = self.napariViewer.dims
        return dict(zip(dims.axis_labels, dims.current_step))

    def setDimensionStepState(self, state):
        dims = self.napariViewer.dims
        for axis, label in enumerate(dims.axis_labels):
            if label in state:
                hi = max(0, dims.nsteps[axis] - 1)
                dims.set_current_step(axis, max(0, min(int(state[label]), hi)))

    def getDimensionOrder(self):
        return tuple(self.napariViewer.dims.order)

    def setDimensionOrder(self, order=None):
        ndim = self.napariViewer.dims.ndim
        natural = tuple(range(ndim))
        requested = natural if order is None else tuple(order)
        if len(requested) != ndim or sorted(requested) != list(range(ndim)):
            requested = natural
        self.napariViewer.dims.order = requested

    def getDisplayedPlaneAxes(self):
        labels = tuple(self.napariViewer.dims.axis_labels)
        order = tuple(self.napariViewer.dims.order)
        return tuple(labels[index] for index in order[-2:]) if len(labels) == len(order) else ()

    def getCameraState(self):
        camera = self.napariViewer.camera
        return {"center": tuple(camera.center), "zoom": float(camera.zoom)}

    def setCameraState(self, state):
        self.napariViewer.camera.center = tuple(state["center"])
        self.napariViewer.camera.zoom = float(state["zoom"])

    def resetView(self):
        self.reset_view_calls += 1
        self.napariViewer.camera.center = (0.0, 0.0, 0.0)
        self.napariViewer.camera.zoom = 1.0


class _FakeResult(ProcessingResult):
    """Minimal ProcessingResult implementation for controller tests."""
    def __init__(self, name, data, axis_labels=None, axis_scales=None, scale_unit="px"):
        super().__init__(
            name=name,
            data=np.asarray(data),
            axis_labels=axis_labels or ["Y", "X"],
            axis_scales=axis_scales or [1.0] * np.asarray(data).ndim,
            scale_unit=scale_unit,
        )
        self.view_modes = [SimpleNamespace(name="standard", transpose=[0, 1])]

    def save(self, path, fmt="tiff"):
        pass  # Not needed for these tests


class _FakeCurveResult(_FakeResult):
    kind = "curve"


class _FakeCommChannel:
    def __init__(self):
        self.sigScanParamsUpdated = SimpleNamespace(connect=lambda x: None)
        self.sigResultProduced = SimpleNamespace(connect=lambda x: None)
        self.sigLiveResultUpdated = SimpleNamespace(connect=lambda x: None)
        self.current_results = []
        self.sigCurrentResultChanged = SimpleNamespace(
            emit=lambda x: self.current_results.append(x)
        )


# --- Tests for view layer naming behavior (via fake view) ---

def test_fake_view_setImage_with_name_sets_layer_name_and_metadata():
    """Verify the fake view implements setImage name parameter correctly."""
    view = _FakeReconstructionView()
    data = np.ones((4, 5), dtype=np.float32)
    
    view.setImage(data, ["Y", "X"], name="test_result.0")
    
    assert view.imgLayer.name == "test_result.0"
    assert view.imgLayer.metadata["source_result"] == "test_result.0"


def test_fake_view_setImage_without_name_keeps_default_name():
    """Verify the fake view defaults to 'Reconstruction' when no name given."""
    view = _FakeReconstructionView()
    data = np.ones((4, 5), dtype=np.float32)
    
    view.setImage(data, ["Y", "X"])
    
    assert view.imgLayer.name == "Reconstruction"
    assert "source_result" not in view.imgLayer.metadata


def test_fake_view_clearImage_resets_name_and_metadata():
    """Verify the fake view's clearImage resets layer state correctly."""
    view = _FakeReconstructionView()
    data = np.ones((4, 5), dtype=np.float32)
    
    # First set with a name
    view.setImage(data, ["Y", "X"], name="test_result.0")
    assert view.imgLayer.name == "test_result.0"
    assert "source_result" in view.imgLayer.metadata
    
    # Then clear
    view.clearImage()
    
    assert view.imgLayer.name == "Reconstruction"
    assert "source_result" not in view.imgLayer.metadata
    assert view.imgLayer.data.shape == (1, 1)


# --- Tests for ReconstructionViewController ---

def test_controller_passes_result_name_to_setImage():
    """Verify that _setProcessingResultSlice passes result.name to setImage."""
    view = _FakeReconstructionView()
    controller = ReconstructionViewController.__new__(ReconstructionViewController)
    controller._widget = view
    controller._logger = SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None)
    controller._transposeOrder = [0, 1]
    controller._displayedAxisLabels = []
    
    result = _FakeResult("my_result.0", np.ones((4, 5), dtype=np.float32))
    
    controller._setProcessingResultSlice(result)
    
    assert view.imgLayer.name == "my_result.0"
    assert view.imgLayer.metadata["source_result"] == "my_result.0"


def test_controller_reactivates_main_layer_after_setImage():
    """Verify that the controller re-activates imgLayer after setImage."""
    view = _FakeReconstructionView()
    controller = ReconstructionViewController.__new__(ReconstructionViewController)
    controller._widget = view
    controller._logger = SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None)
    controller._transposeOrder = [0, 1]
    controller._displayedAxisLabels = []
    
    result = _FakeResult("my_result.1", np.ones((3, 4), dtype=np.float32))
    
    # Simulate the layer being not active before the call
    view.napariViewer.layers.selection.active = None
    
    controller._setProcessingResultSlice(result)
    
    # After setImage, the imgLayer should be re-activated
    assert view.napariViewer.layers.selection.active is view.imgLayer


def test_controller_does_not_render_curve_result_as_image():
    """Curve/table results should be routed to graph/table UI, not napari images."""
    view = _FakeReconstructionView()
    controller = ReconstructionViewController.__new__(ReconstructionViewController)
    controller._widget = view
    controller._logger = SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None)
    controller._transposeOrder = [0, 1]
    controller._displayedAxisLabels = []

    result = _FakeCurveResult(
        "off_switching",
        np.ones((20, 4), dtype=np.float32),
        axis_labels=["Point", "Column"],
    )

    controller._setProcessingResultSlice(result)

    assert view.set_image_calls == 0
    assert view.clear_image_calls == 1
    assert view.imgLayer.name == "Reconstruction"
    assert view.imgLayer.data.shape == (1, 1)
    assert controller._transposeOrder == []
    assert controller._displayedAxisLabels == []


def test_controller_persistence_roundtrips_with_renamed_layer():
    """Verify that display settings persist correctly with the new naming."""
    view = _FakeReconstructionView()
    controller = ReconstructionViewController.__new__(ReconstructionViewController)
    controller._widget = view
    controller._commChannel = _FakeCommChannel()
    controller._logger = SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None)
    controller._transposeOrder = [0, 1]
    controller._displayedAxisLabels = []
    controller._currItemInd = None
    controller._prevViewId = None
    
    result1 = _FakeResult("result_a.0", np.ones((4, 5), dtype=np.float32))
    result2 = _FakeResult("result_b.0", np.ones((3, 6), dtype=np.float32) * 2)
    
    # Simulate switching between results and persisting settings
    view._reconList = [result1, result2]
    view._currentIndex = 0
    
    controller._setProcessingResultSlice(result1)
    controller.setActiveImageDisplayLevels(10.0, 90.0)
    
    # Persist settings from result1
    controller._persistActiveViewerSettings(result1)
    assert result1.getDispLevels() == (10.0, 90.0)
    
    # Switch to result2
    view._currentIndex = 1
    controller._setProcessingResultSlice(result2)
    assert view.imgLayer.name == "result_b.0"
    
    # Switch back to result1 and verify settings are restored
    view._currentIndex = 0
    controller._setProcessingResultSlice(result1)
    assert view.imgLayer.name == "result_a.0"
    
    # The persistence code should still work correctly
    # (In real usage, listItemChanged would restore levels from result1.getDispLevels())
    retrieved_levels = result1.getDispLevels()
    assert retrieved_levels == (10.0, 90.0)


def test_controller_handles_result_without_name_attribute():
    """Verify graceful handling when result has no name attribute."""
    view = _FakeReconstructionView()
    controller = ReconstructionViewController.__new__(ReconstructionViewController)
    controller._widget = view
    controller._logger = SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None)
    controller._transposeOrder = [0, 1]
    controller._displayedAxisLabels = []
    
    # Create a minimal result without name attribute
    result = SimpleNamespace(
        data=np.ones((4, 5), dtype=np.float32),
        axis_labels=["Y", "X"],
        axis_scales=[1.0, 1.0],
        scale_unit="px",
        view_modes=[SimpleNamespace(name="standard", transpose=[0, 1])]
    )
    
    # Should not crash, should fall back to generic name
    controller._setProcessingResultSlice(result)
    
    assert view.imgLayer.name == "Reconstruction"
    assert "source_result" not in view.imgLayer.metadata


def test_controller_handles_removed_current_item_without_crash():
    """Removing the active result leaves no current item; controller must clear."""
    view = _FakeReconstructionView()
    comm = _FakeCommChannel()
    controller = ReconstructionViewController.__new__(ReconstructionViewController)
    controller._widget = view
    controller._commChannel = comm
    controller._logger = SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None)
    controller._transposeOrder = [0, 1]
    controller._axisStep = (0, 0)
    controller._displayedAxisLabels = []
    controller._currItemInd = 0
    controller._prevViewId = None

    view._reconList = []
    view._currentIndex = None

    controller.listItemChanged()

    assert view.imgLayer.name == "Reconstruction"
    assert view.imgLayer.data.shape == (1, 1)
    assert comm.current_results == [None]


# --------------------------------------------------------------------------
# the mutation token a measured layer is cached on
# --------------------------------------------------------------------------

def _bareController():
    controller = ReconstructionViewController.__new__(ReconstructionViewController)
    controller._widget = _FakeReconstructionView()
    controller._logger = SimpleNamespace(
        debug=lambda *a, **k: None, warning=lambda *a, **k: None
    )
    controller._transposeOrder = [0, 1]
    controller._displayedAxisLabels = []
    return controller


def test_each_render_pass_mints_a_new_mutation_token():
    """The token has to change whenever the layer's pixels can have changed."""
    controller = _bareController()
    result = _FakeResult("recon", np.ones((4, 5), dtype=np.float32))

    controller._setProcessingResultSlice(result)
    first = controller._widget.imgLayer.metadata["mutation_token"]
    controller._setProcessingResultSlice(result)
    second = controller._widget.imgLayer.metadata["mutation_token"]

    assert first and second and first != second


def test_a_live_result_gets_no_mutation_token():
    """A result that can be rewritten in place must never be cached against."""
    controller = _bareController()
    controller.__dict__["_liveResultUids"] = set()
    result = _FakeResult("live", np.ones((4, 5), dtype=np.float32))
    controller.__dict__["_liveResultUids"].add(result.result_uid)

    controller._setProcessingResultSlice(result)
    assert controller._widget.imgLayer.metadata["mutation_token"] is None


# --------------------------------------------------------------------------
# per-result viewer state
# --------------------------------------------------------------------------


def _viewer_state_controller(view):
    controller = ReconstructionViewController.__new__(ReconstructionViewController)
    controller._widget = view
    controller._commChannel = _FakeCommChannel()
    controller._logger = SimpleNamespace(
        debug=lambda *a, **k: None, warning=lambda *a, **k: None
    )
    controller._transposeOrder = [0, 1]
    controller._axisStep = (0, 0)
    controller._displayedAxisLabels = []
    controller._currItemInd = None
    controller._prevViewId = None
    controller._displayedResult = None
    controller._viewerStateByResult = {}
    controller._sharedSliceStateByPlane = {}
    controller._restoringSliceState = False
    controller._liveResultUids = set()
    return controller


def test_switching_same_plane_results_preserves_camera_after_initial_fit():
    view = _FakeReconstructionView()
    controller = _viewer_state_controller(view)
    a = _FakeResult("a", np.ones((4, 5), dtype=np.float32))
    b = _FakeResult("b", np.ones((4, 5), dtype=np.float32))
    view._reconList = [a, b]

    view._currentIndex = 0
    controller.listItemChanged()
    assert view.reset_view_calls == 1
    view.napariViewer.camera.zoom = 7.5
    view.napariViewer.camera.center = (0.0, 12.0, 13.0)

    view._currentIndex = 1
    controller.listItemChanged()

    assert view.reset_view_calls == 1
    assert view.napariViewer.camera.zoom == 7.5
    assert view.napariViewer.camera.center == (0.0, 12.0, 13.0)


def test_direct_napari_axis_roll_is_remembered_per_result_and_new_result_is_natural():
    view = _FakeReconstructionView()
    controller = _viewer_state_controller(view)
    a = _FakeResult("a", np.ones((4, 5), dtype=np.float32))
    b = _FakeResult("b", np.ones((4, 5), dtype=np.float32))
    view._reconList = [a, b]

    view._currentIndex = 0
    controller.listItemChanged()
    view.napariViewer.dims.order = (1, 0)  # napari "roll dimensions"

    view._currentIndex = 1
    controller.listItemChanged()
    assert view.getDimensionOrder() == (0, 1)

    view._currentIndex = 0
    controller.listItemChanged()
    assert view.getDimensionOrder() == (1, 0)


def test_named_view_choice_is_remembered_and_not_replaced_by_automatic_default():
    class _AutoLeftResult(_FakeResult):
        def __init__(self, name):
            super().__init__(name, np.ones((2, 3), dtype=np.float32))
            self.view_modes = [
                SimpleNamespace(name="Standard", transpose=[0, 1]),
                SimpleNamespace(name="Bottom", transpose=[0, 1]),
                SimpleNamespace(name="Left", transpose=[0, 1]),
            ]

        def initial_view_mode_name(self):
            return "Left"

    view = _FakeReconstructionView()
    controller = _viewer_state_controller(view)
    a = _AutoLeftResult("a")
    b = _FakeResult("b", np.ones((2, 3), dtype=np.float32))
    view._reconList = [a, b]

    view._currentIndex = 0
    controller.listItemChanged()
    assert view.getViewName() == "Left"

    # User explicitly chooses top-down for this result.
    view._viewName = "Standard"
    controller._viewChanged()
    assert view.reset_view_calls == 2

    view._currentIndex = 1
    controller.listItemChanged()
    view._currentIndex = 0
    controller.listItemChanged()
    assert view.getViewName() == "Standard"


def test_new_object_does_not_inherit_view_state_from_same_result_uid():
    view = _FakeReconstructionView()
    controller = _viewer_state_controller(view)
    a = _FakeResult("a", np.ones((4, 5), dtype=np.float32))
    b = _FakeResult("b", np.ones((4, 5), dtype=np.float32))
    b.result_uid = a.result_uid  # provenance identity is deliberately shared
    view._reconList = [a, b]

    view._currentIndex = 0
    controller.listItemChanged()
    view.napariViewer.dims.order = (1, 0)

    view._currentIndex = 1
    controller.listItemChanged()

    # Viewer state is object-local, so a fresh reconstruction starts naturally.
    assert view.getDimensionOrder() == (0, 1)


def test_xyz_result_with_only_standard_declared_gets_orthogonal_view_modes():
    view = _FakeReconstructionView()
    controller = _viewer_state_controller(view)

    result = ProcessingResult(
        name="volume",
        data=np.zeros((3, 4, 5), dtype=np.float32),
        axis_labels=["Z", "Y", "X"],
    )
    view._reconList = [result]
    view._currentIndex = 0

    controller.listItemChanged()

    assert [mode.name for mode in view._viewModes] == ["Standard", "Bottom", "Left"]
    assert [tuple(mode.transpose) for mode in view._viewModes] == [
        (0, 1, 2),
        (1, 0, 2),
        (2, 0, 1),
    ]


def test_switching_xy_to_yz_refits_camera_then_restores_each_results_camera():
    class _VolumeResult(ProcessingResult):
        def __init__(self, name, preferred):
            super().__init__(
                name=name,
                data=np.zeros((3, 4, 5), dtype=np.float32),
                axis_labels=["Z", "Y", "X"],
                view_modes=[
                    SimpleNamespace(name="Standard", transpose=(0, 1, 2)),
                    SimpleNamespace(name="Bottom", transpose=(1, 0, 2)),
                    SimpleNamespace(name="Left", transpose=(2, 0, 1)),
                ],
            )
            self._preferred = preferred

        def initial_view_mode_name(self):
            return self._preferred

    view = _FakeReconstructionView()
    controller = _viewer_state_controller(view)
    xy = _VolumeResult("xy", "Standard")
    yz = _VolumeResult("yz", "Left")
    view._reconList = [xy, yz]

    view._currentIndex = 0
    controller.listItemChanged()
    assert view.getDisplayedPlaneAxes() == ("Y", "X")
    view.napariViewer.camera.zoom = 8.0
    view.napariViewer.camera.center = (0.0, 20.0, 30.0)

    view._currentIndex = 1
    controller.listItemChanged()
    assert view.getDisplayedPlaneAxes() == ("Z", "Y")
    assert view.reset_view_calls == 2  # first XY fit + incompatible XY->YZ fit
    assert view.napariViewer.camera.zoom == 1.0
    view.napariViewer.camera.zoom = 3.0
    view.napariViewer.camera.center = (0.0, 5.0, 6.0)

    view._currentIndex = 0
    controller.listItemChanged()
    assert view.getDisplayedPlaneAxes() == ("Y", "X")
    # Returning to XY restores that result's own framing instead of reusing YZ.
    assert view.napariViewer.camera.zoom == 8.0
    assert view.napariViewer.camera.center == (0.0, 20.0, 30.0)


def _orthogonal_volume_result(name, preferred="Standard", shape=(6, 7, 8)):
    class _VolumeResult(ProcessingResult):
        def __init__(self):
            super().__init__(
                name=name,
                data=np.zeros(shape, dtype=np.float32),
                axis_labels=["Z", "Y", "X"],
                view_modes=[
                    SimpleNamespace(name="Standard", transpose=(0, 1, 2)),
                    SimpleNamespace(name="Bottom", transpose=(1, 0, 2)),
                    SimpleNamespace(name="Left", transpose=(2, 0, 1)),
                ],
            )

        def initial_view_mode_name(self):
            return preferred

    return _VolumeResult()


def _move_spatial_step(controller, view, axis_label, step):
    state = view.getDimensionStepState()
    state[axis_label] = step
    view.setDimensionStepState(state)
    controller.axisStepChanged(tuple(view.napariViewer.dims.current_step))


def test_yz_slice_survives_switch_through_2d_xy_result():
    """A 2-D result must not erase the last X slice used in a YZ view."""
    view = _FakeReconstructionView()
    controller = _viewer_state_controller(view)
    yz = _orthogonal_volume_result("yz", preferred="Left", shape=(5, 6, 9))
    xy = _FakeResult("xy", np.ones((6, 9), dtype=np.float32))
    view._reconList = [yz, xy]

    view._currentIndex = 0
    controller.listItemChanged()
    assert view.getDisplayedPlaneAxes() == ("Z", "Y")
    _move_spatial_step(controller, view, "X", 7)
    assert view.getDimensionStepState()["X"] == 7

    view._currentIndex = 1
    controller.listItemChanged()
    assert view.getDisplayedPlaneAxes() == ("Y", "X")

    view._currentIndex = 0
    controller.listItemChanged()
    assert view.getDisplayedPlaneAxes() == ("Z", "Y")
    assert view.getDimensionStepState()["X"] == 7


def test_unlocked_yz_views_share_the_most_recent_x_slice():
    """Default comparison mode follows the same physical slice across YZ results."""
    view = _FakeReconstructionView()
    controller = _viewer_state_controller(view)
    a = _orthogonal_volume_result("a", preferred="Left", shape=(5, 6, 10))
    b = _orthogonal_volume_result("b", preferred="Left", shape=(5, 6, 10))
    view._reconList = [a, b]

    view._currentIndex = 0
    controller.listItemChanged()
    _move_spatial_step(controller, view, "X", 3)

    view._currentIndex = 1
    controller.listItemChanged()
    assert view.getDimensionStepState()["X"] == 3
    _move_spatial_step(controller, view, "X", 6)

    view._currentIndex = 0
    controller.listItemChanged()
    assert view.getDimensionStepState()["X"] == 6


def test_lock_slice_per_result_restores_each_yz_results_own_slice():
    """Locked mode keeps different comparison slices for different results."""
    view = _FakeReconstructionView()
    controller = _viewer_state_controller(view)
    a = _orthogonal_volume_result("a", preferred="Left", shape=(5, 6, 10))
    b = _orthogonal_volume_result("b", preferred="Left", shape=(5, 6, 10))
    view._reconList = [a, b]

    view._currentIndex = 0
    controller.listItemChanged()
    _move_spatial_step(controller, view, "X", 2)

    view._currentIndex = 1
    controller.listItemChanged()
    assert view.getDimensionStepState()["X"] == 2

    view._lockSlicePerResult = True
    controller._sliceLockChanged(True)
    _move_spatial_step(controller, view, "X", 7)

    view._currentIndex = 0
    controller.listItemChanged()
    assert view.getDimensionStepState()["X"] == 2

    view._currentIndex = 1
    controller.listItemChanged()
    assert view.getDimensionStepState()["X"] == 7


def test_slice_memory_is_per_physical_view_within_one_result():
    """XY and YZ remember independent Z and X navigation slices."""
    view = _FakeReconstructionView()
    controller = _viewer_state_controller(view)
    result = _orthogonal_volume_result("volume", preferred="Standard", shape=(6, 7, 8))
    view._reconList = [result]
    view._currentIndex = 0

    controller.listItemChanged()
    assert view.getDisplayedPlaneAxes() == ("Y", "X")
    _move_spatial_step(controller, view, "Z", 4)

    view._viewName = "Left"
    controller._viewChanged()
    assert view.getDisplayedPlaneAxes() == ("Z", "Y")
    _move_spatial_step(controller, view, "X", 5)

    view._viewName = "Standard"
    controller._viewChanged()
    assert view.getDisplayedPlaneAxes() == ("Y", "X")
    assert view.getDimensionStepState()["Z"] == 4

    view._viewName = "Left"
    controller._viewChanged()
    assert view.getDisplayedPlaneAxes() == ("Z", "Y")
    assert view.getDimensionStepState()["X"] == 5


def test_shared_slice_is_clamped_for_smaller_compatible_result():
    """Following a shared slice never addresses beyond a smaller stack."""
    view = _FakeReconstructionView()
    controller = _viewer_state_controller(view)
    large = _orthogonal_volume_result("large", preferred="Left", shape=(5, 6, 10))
    small = _orthogonal_volume_result("small", preferred="Left", shape=(5, 6, 4))
    view._reconList = [large, small]

    view._currentIndex = 0
    controller.listItemChanged()
    _move_spatial_step(controller, view, "X", 8)

    view._currentIndex = 1
    controller.listItemChanged()
    assert view.getDimensionStepState()["X"] == 3
