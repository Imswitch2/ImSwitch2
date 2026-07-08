"""Test SegmentationWidget conforms to result-processor widget contract."""

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


class _FakeViewer:
    def __init__(self):
        self.added_labels = []
        self.layers = []
    
    def add_labels(self, data, **kwargs):
        self.added_labels.append((np.asarray(data), kwargs))


class _Signal:
    def __init__(self):
        self.emitted = []
    
    def emit(self, *args):
        self.emitted.append(args)
    
    def connect(self, fn):
        pass


def _make_widget():
    """Build a SegmentationWidget with minimal fakes."""
    widget = SegmentationWidget.__new__(SegmentationWidget)
    widget._viewer = _FakeViewer()
    widget._roiManagerWidget = None
    widget._last_analysis = None
    widget._currentResult = None
    widget._preview_timer = SimpleNamespace(
        setSingleShot=lambda x: None,
        setInterval=lambda x: None,
        timeout=SimpleNamespace(connect=lambda fn: None),
    )
    widget._dims_connection = None
    widget._layer_selection_connection = None
    
    # Required UI widgets
    widget.runButton = SimpleNamespace(
        clicked=SimpleNamespace(connect=lambda fn: None),
        setEnabled=lambda x: None,
    )
    widget.summaryLabel = SimpleNamespace(setText=lambda x: None, text="")
    widget.methodCombo = SimpleNamespace(currentText=lambda: "otsu")
    widget.thresholdSpin = SimpleNamespace(value=lambda: 0.0)
    widget.minAreaSpin = SimpleNamespace(value=lambda: 10)
    widget.smoothSpin = SimpleNamespace(value=lambda: 0.0)
    widget.backgroundSpin = SimpleNamespace(value=lambda: 0.0)
    widget.morphologySpin = SimpleNamespace(value=lambda: 0)
    widget.fillHolesCheck = SimpleNamespace(isChecked=lambda: False)
    widget.clearBorderCheck = SimpleNamespace(isChecked=lambda: False)
    widget.localBlockSpin = SimpleNamespace(value=lambda: 51)
    widget.localOffsetSpin = SimpleNamespace(value=lambda: 0.0)
    widget.watershedDistanceSpin = SimpleNamespace(value=lambda: 5)
    
    # Inject the signal and processor as the real __init__ does
    widget.sigRunRequested = _Signal()
    from imswitch.improcess.processors import SegmentationProcessor
    widget.processor = SegmentationProcessor()
    
    return widget


def test_segmentation_widget_has_signal_and_processor():
    """SegmentationWidget should have sigRunRequested signal and processor attribute."""
    widget = _make_widget()
    
    assert hasattr(widget, "sigRunRequested")
    assert hasattr(widget, "processor")
    assert widget.processor.id == "segmentation"


def test_segmentation_widget_has_setCurrentResult():
    """SegmentationWidget should have setCurrentResult method."""
    widget = _make_widget()
    fake_result = SimpleNamespace(name="test", data=np.zeros((10, 10)))
    
    widget.setCurrentResult(fake_result)
    
    assert widget._currentResult is fake_result


def test_segmentation_widget_has_setStatusText():
    """SegmentationWidget should have setStatusText method."""
    widget = _make_widget()
    
    widget.setStatusText("test status")
    
    # Should forward to summaryLabel (mock doesn't capture, but method exists)
    assert hasattr(widget, "setStatusText")


def test_parameter_values_keys_match_processor_contract():
    """parameterValues() keys must be exactly what SegmentationProcessor.apply
    reads — otherwise the processor silently uses defaults and the panel's
    controls are ignored on commit (regression: agent emitted method/threshold/
    smooth/background/morphology instead of threshold_method/threshold_value/
    smooth_sigma/background_radius/morphology_radius)."""
    from imswitch.improcess.processors.segmentation import processor as seg_processor_mod

    widget = _make_widget()
    params = widget.parameterValues()

    expected = {
        "threshold_method", "threshold_value", "min_area", "smooth_sigma",
        "background_radius", "morphology_radius", "fill_holes", "clear_border",
        "local_block_size", "local_offset", "watershed_min_distance",
    }
    assert set(params) == expected

    # Every key the widget emits must be one the processor actually reads.
    source = Path(seg_processor_mod.__file__).read_text()
    for key in params:
        assert f'params.get("{key}"' in source, f"processor never reads {key!r}"


def test_manual_threshold_from_panel_actually_takes_effect():
    """End-to-end: params from the panel must change the processor's output
    (not silently fall back to otsu)."""
    from imswitch.improcess.model.array_result import ArrayProcessingResult

    widget = _make_widget()
    # manual threshold that isolates the bright square from a mid-gray block
    widget.methodCombo = SimpleNamespace(currentText=lambda: "manual")
    widget.thresholdSpin = SimpleNamespace(value=lambda: 50.0)
    widget.minAreaSpin = SimpleNamespace(value=lambda: 1)

    image = np.zeros((16, 16), dtype=np.float32)
    image[2:6, 2:6] = 100.0   # above manual threshold
    image[10:14, 10:14] = 20.0  # below manual threshold -> excluded
    result = ArrayProcessingResult(name="img", data=image, axis_labels=["Y", "X"])

    out = widget.processor.apply(result, widget.parameterValues())

    # manual threshold 50 keeps only the bright square (one region);
    # otsu (the wrong-keys fallback) would have found both.
    assert len(out.analysis.regions) == 1
    assert abs(out.analysis.threshold - 50.0) < 1e-6


def test_segmentation_widget_run_emits_sigRunRequested():
    """Clicking Segment button should emit sigRunRequested with current result and params."""
    widget = _make_widget()
    fake_result = SimpleNamespace(name="test", data=np.zeros((10, 10)))
    widget.setCurrentResult(fake_result)
    
    widget.run()
    
    assert len(widget.sigRunRequested.emitted) == 1
    emitted_result, emitted_params = widget.sigRunRequested.emitted[0]
    assert emitted_result is fake_result
    assert isinstance(emitted_params, dict)
    assert "threshold_method" in emitted_params


def test_segmentation_widget_run_guards_no_result():
    """Clicking Segment when no result is selected should show friendly message."""
    widget = _make_widget()
    
    widget.run()
    
    assert len(widget.sigRunRequested.emitted) == 0


def test_segmentation_widget_setCurrentResult_captures_analysis_from_segmentation_result():
    """setCurrentResult should capture analysis from SegmentationResult for ROI export."""
    from imswitch.improcess.processors.segmentation.result import SegmentationResult
    from imswitch.improcess.analysis.segmentation import SegmentationAnalysis
    
    widget = _make_widget()
    
    analysis = SegmentationAnalysis(
        labels=np.array([[1, 1], [2, 2]], dtype=np.int32),
        mask=np.array([[True, True], [True, True]]),
        threshold=0.5,
        regions=[],
        processed_image=np.zeros((2, 2)),
        metadata={},
    )
    seg_result = SegmentationResult(
        name="seg",
        analysis=analysis,
    )
    
    widget.setCurrentResult(seg_result)
    
    assert widget._last_analysis is analysis


def test_segmentation_widget_setCurrentResult_clears_analysis_for_non_segmentation():
    """setCurrentResult with non-SegmentationResult should clear cached analysis."""
    widget = _make_widget()
    
    # Set a cached analysis first
    from imswitch.improcess.analysis.segmentation import SegmentationAnalysis
    widget._last_analysis = SegmentationAnalysis(
        labels=np.array([[1]], dtype=np.int32),
        mask=np.array([[True]]),
        threshold=0.5,
        regions=[],
        processed_image=np.zeros((1, 1)),
        metadata={},
    )
    
    # Set a non-segmentation result
    fake_result = SimpleNamespace(name="not_seg", data=np.zeros((10, 10)))
    widget.setCurrentResult(fake_result)
    
    assert widget._last_analysis is None


def test_segmentation_widget_wiring_creates_result_processor_controller():
    """Segmentation widget should be wired via _wire_runtime_result_processor."""
    from imswitch.improcess.controller.ImProcessMainController import ImProcessMainController
    
    widget = _make_widget()
    view = SimpleNamespace(getRuntimeAnalysisWidget=lambda tool_id: widget if tool_id == "segmentation" else None)
    factory = SimpleNamespace(
        createController=lambda cls, w: SimpleNamespace(widget=w)
    )
    
    controller = ImProcessMainController.__new__(ImProcessMainController)
    controller._ImProcessMainController__mainView = view
    controller._ImProcessMainController__factory = factory
    controller._ImProcessMainController__logger = SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
    )
    controller._resultProcessorControllers = {}
    controller.mainViewController = SimpleNamespace(
        reconstructionController=SimpleNamespace(
            getActiveResult=lambda: SimpleNamespace(name="test", data=np.zeros((10, 10)))
        )
    )
    
    controller._wire_runtime_result_processor("segmentation")
    
    assert "segmentation" in controller._resultProcessorControllers
    assert controller._resultProcessorControllers["segmentation"].widget is widget
