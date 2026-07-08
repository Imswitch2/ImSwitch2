"""Phase 3 result-unification: Multicolor Register/Apply publish results.

The producing actions must emit ProcessingResults via ``sigResultProduced``
(reconstruction-list entries) and never create floating napari image layers;
only the ephemeral split-boundary/bead overlays may touch the viewer.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

# Direct import to avoid the view package import chain (matplotlib/napari).
_view_path = Path(__file__).parent.parent / "view"
sys.path.insert(0, str(_view_path))
try:
    from MulticolorWidget import MulticolorWidget
finally:
    sys.path.pop(0)

from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors.multicolor_apply import MulticolorApplyResult
from imswitch.improcess.processors.multicolor_registration import (
    MulticolorRegistrationResult,
)


class MinimalResult(ProcessingResult):
    def save(self, path: Path, fmt: str):
        pass


class _Sig:
    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)

    def connect(self, fn):
        pass


class _FakeViewer:
    def __init__(self):
        self.layers = SimpleNamespace(selection=SimpleNamespace(active=None))
        self.added_images = []

    def add_image(self, *args, **kwargs):
        self.added_images.append((args, kwargs))

    def add_points(self, *args, **kwargs):
        pass

    def add_shapes(self, *args, **kwargs):
        pass


def _identity_alignment(x_bounds=None):
    return {
        "x_bounds": x_bounds or [0, 2, 4, 6],
        "reference_channel": 0,
        "mode": "maxproj",
        "roi_width": 2,
        "source_shape": (2, 3, 6),
        "transforms": [
            {"affine_2d": np.eye(3), "affine_3d": None, "z_shift": 0},
            {"affine_2d": np.eye(3), "affine_3d": None, "z_shift": 0},
            {"affine_2d": np.eye(3), "affine_3d": None, "z_shift": 0},
        ],
    }


def _synthetic_bead_volume():
    volume = np.zeros((12, 40, 30), dtype=np.float32)
    local = [(2, 5, 3), (9, 30, 8), (5, 12, 1), (3, 33, 6), (10, 8, 5)]
    for strip in range(3):
        for z, y, x in local:
            volume[z, y, strip * 10 + x] = 100.0
    return volume


def _spin(value):
    return SimpleNamespace(value=lambda: value)


def _widget(current_result=None):
    widget = MulticolorWidget.__new__(MulticolorWidget)
    widget._viewer = _FakeViewer()
    widget._alignment = None
    widget._bead_state = None
    widget._currentResult = current_result
    widget.sigResultProduced = _Sig()

    widget.slicesSpin = _spin(3)
    widget.axisCombo = SimpleNamespace(currentText=lambda: "X")
    widget.boundsEdit = SimpleNamespace(text=lambda: "")
    widget.timeSpin = _spin(0)
    widget.modeCombo = SimpleNamespace(currentText=lambda: "maxproj")
    widget.referenceSpin = _spin(0)
    widget.alignmentPathEdit = SimpleNamespace(text=lambda: "")
    widget.beadSigmaSpin = _spin(1.5)
    widget.beadMinDistSpin = _spin(6)
    widget.beadThresholdSpin = _spin(0.5)
    widget.matchMaxDistSpin = _spin(25.0)
    widget.ransacIterSpin = _spin(200)
    widget.ransacInlierSpin = _spin(3.0)
    widget.summaryLabel = SimpleNamespace(setText=lambda text: None)
    widget.resultsText = SimpleNamespace(setPlainText=lambda text: None)
    return widget


def test_register_publishes_registration_result_not_a_floating_layer():
    source = MinimalResult(
        name="beads",
        data=_synthetic_bead_volume(),
        axis_labels=["Z", "Y", "X"],
        axis_scales=[4.0, 2.0, 3.0],
        scale_unit="um",
    )
    widget = _widget(current_result=source)
    # descriptor_3d is the pure-scipy path (maxproj/volume need pystackreg);
    # bead params match _synthetic_bead_volume (see the processor tests).
    widget.modeCombo = SimpleNamespace(currentText=lambda: "descriptor_3d")
    widget.beadSigmaSpin = _spin(1.0)
    widget.beadMinDistSpin = _spin(2)
    widget.beadThresholdSpin = _spin(0.3)
    widget.matchMaxDistSpin = _spin(5.0)
    widget.ransacInlierSpin = _spin(2.0)

    widget.register()

    assert widget._viewer.added_images == []  # no floating layer
    assert len(widget.sigResultProduced.emitted) == 1
    result, name = widget.sigResultProduced.emitted[0]
    assert isinstance(result, MulticolorRegistrationResult)
    assert name == result.name == "beads (multicolor registration)"
    assert result.axis_labels == ["C", "Z", "Y", "X"]
    assert result.data.shape[0] == 3  # three channels
    # (C, Z, Y, X) scales: C=1.0, spatial carried from the source result.
    assert result.axis_scales == [1.0, 4.0, 2.0, 3.0]
    assert result.scale_unit == "um"
    # widget keeps the alignment for a subsequent in-memory Apply
    assert widget._alignment is not None


def test_apply_publishes_apply_result_from_current_result():
    volume = np.zeros((2, 3, 6), dtype=np.float32)
    volume[..., 0:2] = 1
    volume[..., 2:4] = 2
    volume[..., 4:6] = 3
    source = MinimalResult(
        name="stack",
        data=volume,
        axis_labels=["Z", "Y", "X"],
        axis_scales=[4.0, 2.0, 3.0],
        scale_unit="um",
    )
    widget = _widget(current_result=source)
    widget._alignment = _identity_alignment()

    widget.apply()

    assert widget._viewer.added_images == []
    assert len(widget.sigResultProduced.emitted) == 1
    result, _name = widget.sigResultProduced.emitted[0]
    assert isinstance(result, MulticolorApplyResult)
    assert result.axis_labels == ["C", "Z", "Y", "X"]
    assert result.data.shape == (3, 2, 3, 2)
    assert np.all(result.data[0] == 1) and np.all(result.data[2] == 3)
    assert result.axis_scales == [1.0, 4.0, 2.0, 3.0]


def test_apply_falls_back_to_active_layer_without_current_result():
    volume = np.zeros((2, 3, 6), dtype=np.float32)
    volume[..., 0:2] = 1
    volume[..., 2:4] = 2
    volume[..., 4:6] = 3
    widget = _widget(current_result=None)
    # The render path always writes axis_labels metadata on layers it creates;
    # the standalone fallback relies on it (a bare 3D guess is C/Y/X).
    layer = SimpleNamespace(
        data=volume, scale=(1.0, 1.0, 1.0), name="layer", visible=True,
        metadata={"axis_labels": ["Z", "Y", "X"]},
    )
    widget._viewer.layers = SimpleNamespace(
        selection=SimpleNamespace(active=layer), __iter__=lambda self: iter([layer])
    )
    widget._alignment = _identity_alignment()

    widget.apply()

    assert len(widget.sigResultProduced.emitted) == 1
    result, _name = widget.sigResultProduced.emitted[0]
    assert isinstance(result, MulticolorApplyResult)
    assert result.data.shape == (3, 2, 3, 2)


def test_source_prefers_current_result_over_active_layer():
    result_volume = _synthetic_bead_volume()
    layer_volume = np.ones((4, 4, 4), dtype=np.float32)
    source = MinimalResult(
        name="selected", data=result_volume, axis_labels=["Z", "Y", "X"]
    )
    widget = _widget(current_result=source)
    layer = SimpleNamespace(
        data=layer_volume, scale=(1.0,) * 3, name="other", visible=True, metadata={}
    )
    widget._viewer.layers = SimpleNamespace(
        selection=SimpleNamespace(active=layer), __iter__=lambda self: iter([layer])
    )

    resolved = widget._resolve_source()

    assert resolved.name == "selected"
    assert resolved.data is source.data


# --- controller bridge: producing panels forward into the pipeline -----------

class _BridgeSig:
    def __init__(self):
        self.slots = []
        self.emitted = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *args):
        self.emitted.append(args)
        for fn in list(self.slots):
            fn(*args)


def _bridge_controller(widget):
    from imswitch.improcess.controller.ImProcessMainController import (
        ImProcessMainController,
    )

    controller = ImProcessMainController.__new__(ImProcessMainController)
    controller._ImProcessMainController__mainView = SimpleNamespace(
        getRuntimeAnalysisWidget=lambda tool_id: widget
    )
    controller._ImProcessMainController__factory = SimpleNamespace(
        createController=lambda cls, w: SimpleNamespace(widget=w)
    )
    controller._ImProcessMainController__logger = SimpleNamespace(
        debug=lambda *a, **k: None, info=lambda *a, **k: None
    )
    controller._ImProcessMainController__commChannel = SimpleNamespace(
        sigResultProduced=_BridgeSig(),
        sigCurrentResultChanged=_BridgeSig(),
    )
    controller._resultProcessorControllers = {}
    controller._panelResultBridges = set()
    controller.mainViewController = SimpleNamespace(
        reconstructionController=SimpleNamespace(getActiveResult=lambda: None)
    )
    return controller


def test_producing_panel_bridge_forwards_results_once():
    """A panel exposing only sigResultProduced gets bridged to the comm
    channel — and wiring it under TWO tool ids (multicolor-registration and
    multicolor-apply share one widget) must not double-publish."""
    widget = SimpleNamespace(
        sigResultProduced=_BridgeSig(),
        setCurrentResult=lambda result: None,
    )
    controller = _bridge_controller(widget)

    controller._wire_runtime_result_processor("multicolor-registration")
    controller._wire_runtime_result_processor("multicolor-apply")

    fake_result = SimpleNamespace(name="r")
    widget.sigResultProduced.emit(fake_result, "r")

    comm = controller._ImProcessMainController__commChannel
    assert comm.sigResultProduced.emitted == [(fake_result, "r")]  # exactly once
    assert comm.sigCurrentResultChanged.emitted == [(fake_result,)]
