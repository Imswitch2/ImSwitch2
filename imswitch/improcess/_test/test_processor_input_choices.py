import numpy as np
import pytest

from imswitch.improcess.model import DisplayLayerProcessingResult, ProcessingResult
from imswitch.improcess.model.result import DisplayLayerSpec
from imswitch.improcess.reconstructors.monalisa.result import MonalisaProcessingResult


class _LayeredResult(ProcessingResult):
    def __init__(self):
        super().__init__(
            name="layered",
            data=np.zeros((2, 3, 4), dtype=np.float32),
            axis_labels=["C", "Y", "X"],
        )

    def display_layers(self):
        return [
            DisplayLayerSpec(
                name="layered map",
                data=np.ones((3, 4), dtype=np.float32),
                axis_labels=["Y", "X"],
                display_levels=(0.0, 1.0),
                axis_scales=[2.0, 3.0],
                scale_unit="um",
                metadata={"component": "map", "source_result": self.name},
            )
        ]

    def save(self, path, fmt):
        pass


class _PlainResult(ProcessingResult):
    def __init__(self):
        super().__init__(
            name="plain",
            data=np.zeros((3, 4), dtype=np.float32),
            axis_labels=["Y", "X"],
        )

    def save(self, path, fmt):
        pass


def _scan_params():
    return {
        "dimensions": ["Right-Left", "Up-Down", "Back-Front", "Timepoints"],
        "directions": ["pos", "pos", "pos"],
        "steps": ["10", "10", "1", "1"],
        "step_sizes": ["35", "35", "100", "1"],
    }


def test_plain_result_offers_whole_result_only():
    result = _PlainResult()

    choices = result.processor_input_choices()

    assert [(choice.id, choice.label, choice.result) for choice in choices] == [
        ("result", "Whole result", result)
    ]


def test_display_layers_become_explicit_processor_inputs():
    result = _LayeredResult()

    choices = result.processor_input_choices()

    assert [choice.id for choice in choices] == ["result", "component:map"]
    component = choices[1].result
    assert isinstance(component, DisplayLayerProcessingResult)
    assert component.name == "layered_map"
    assert component.component == "map"
    assert component.source_result is result
    assert component.axis_labels == ["Y", "X"]
    assert component.axis_scales == [2.0, 3.0]
    assert component.scale_unit == "um"
    np.testing.assert_array_equal(component.data, np.ones((3, 4), dtype=np.float32))


def test_display_layer_processor_input_save_is_rejected(tmp_path):
    component = _LayeredResult().processor_input_choices()[1].result

    with pytest.raises(ValueError, match="derived components"):
        component.save(tmp_path / "component.tif", "tiff")


def test_monalisa_processor_choices_include_named_bases():
    data = np.zeros((1, 2, 1, 1, 3, 4), dtype=np.float32)
    data[:, 0] = 10.0
    data[:, 1] = 20.0
    result = MonalisaProcessingResult(
        name="ml",
        data=data,
        scan_params=_scan_params(),
    )

    choices = result.processor_input_choices()

    assert [choice.id for choice in choices] == [
        "result",
        "component:signal",
        "component:background",
    ]
    assert choices[1].result.axis_labels == ["Dataset", "T", "Z", "Y", "X"]
    assert choices[2].result.component == "background"
    np.testing.assert_array_equal(choices[1].result.data, data[:, 0])
