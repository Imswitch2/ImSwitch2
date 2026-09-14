"""The declared input-arity contract shared by every processor.

``min_inputs``/``max_inputs`` say how many results one run consumes, and
``check_inputs`` explains a refusal in words the UI can show. Multi-input
processors used to smuggle their extra inputs through an undeclared
``params["results"]`` convention, which no panel could discover.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.processors import _all_processor_classes
from imswitch.improcess.processors.base import Processor


def _result(name, shape=(4, 5), labels=("Y", "X"), scales=None, unit="px"):
    return ArrayProcessingResult(
        name=name,
        data=np.zeros(shape, dtype=np.float32),
        axis_labels=list(labels),
        axis_scales=list(scales) if scales else [1.0] * len(shape),
        scale_unit=unit,
    )


def _processor(processor_id):
    return _all_processor_classes()[processor_id]()


def test_processors_are_single_input_by_default():
    assert Processor.min_inputs == 1
    assert Processor.max_inputs == 1
    denoise = _processor("denoise")
    assert (denoise.min_inputs, denoise.max_inputs) == (1, 1)


@pytest.mark.parametrize(
    "processor_id, arity",
    [
        ("channel-merge", (2, None)),
        ("stack-combine", (2, None)),
        ("image-calculator", (2, 2)),
        ("frc", (1, 2)),
    ],
)
def test_multi_input_processors_declare_their_arity(processor_id, arity):
    processor = _processor(processor_id)
    assert (processor.min_inputs, processor.max_inputs) == arity


def test_too_few_inputs_is_refused_with_a_reason():
    ok, reason = _processor("channel-merge").check_inputs([_result("a")])

    assert ok is False
    assert "two" in reason


def test_too_many_inputs_is_refused_with_a_reason():
    inputs = [_result("a"), _result("b"), _result("c")]

    ok, reason = _processor("image-calculator").check_inputs(inputs)

    assert ok is False
    assert "at most 2" in reason


def test_single_input_processor_refuses_a_multi_input_run():
    """Batch scope loops one input at a time; handing a single-input
    processor several at once is a caller error, not a batch."""
    ok, reason = _processor("denoise").check_inputs([_result("a"), _result("b")])

    assert ok is False
    assert "at most 1" in reason


def test_inputs_the_processor_cannot_take_are_named():
    class _OnlyStacks(Processor):
        name = "Only stacks"
        id = "only-stacks"

        @property
        def applies_to(self):
            return lambda result: result.data.ndim >= 3

        def make_param_widget(self, parent):  # pragma: no cover - unused
            raise NotImplementedError

        def apply(self, result, params):  # pragma: no cover - unused
            raise NotImplementedError

    ok, reason = _OnlyStacks().check_inputs([_result("flat")])

    assert ok is False
    assert "flat" in reason


@pytest.mark.parametrize("processor_id", ["channel-merge", "stack-combine", "image-calculator"])
def test_metadata_mismatches_are_reported_not_just_refused(processor_id):
    """Every combine-shaped processor reports *why* it refused: the reason
    names the offending input and the property that disagrees. (The calculator
    uses the looser elementwise check, but owes the same explanation.)"""
    first = _result("recA", scales=[0.065, 0.065], unit="um")
    scaled = _result("recB", scales=[0.13, 0.13], unit="um")
    united = _result("recC", scales=[0.065, 0.065], unit="nm")
    shaped = _result("recD", shape=(6, 5))

    processor = _processor(processor_id)
    for other, expected in ((scaled, "scales"), (united, "unit"), (shaped, "shape")):
        ok, reason = processor.check_inputs([first, other])
        assert ok is False
        assert other.name in reason and expected in reason


def test_merge_refuses_inputs_that_already_have_a_channel_axis():
    """A second C axis would give the output two identically-labelled axes,
    and every label-driven axis lookup takes the first match."""
    first = _result("recA", shape=(2, 4, 5), labels=("C", "Y", "X"))
    second = _result("recB", shape=(2, 4, 5), labels=("C", "Y", "X"))

    ok, reason = _processor("channel-merge").check_inputs([first, second])

    assert ok is False
    assert "'C' already exists" in reason


def test_image_calculator_allows_the_same_result_twice():
    same = _result("recA")

    ok, _reason = _processor("image-calculator").check_inputs([same, same])

    assert ok is True


def test_check_inputs_does_not_materialize_lazy_data():
    class _Exploding:
        shape = (4, 5)
        ndim = 2

        def __array__(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("check_inputs materialized the array")

    lazy = _result("lazy")
    lazy.data = _Exploding()
    other = _result("other")

    assert _processor("channel-merge").check_inputs([lazy, other])[0] is True
