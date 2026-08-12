"""Semantic result kinds gate processor compatibility.

A metrics table and a microscope image can share the same 2D array shape, so
``applies_to`` (the shape/axis gate) alone cannot keep image processors away
from non-image values. ``Processor.accepts()`` checks the result's semantic
kind first. This matrix pins the behavior for every built-in processor
against representative results of every kind.
"""

from pathlib import Path

import numpy as np

from imswitch.improcess.model import (
    RESULT_KINDS,
    ArrayProcessingResult,
    LocalizationResult,
    ProcessingResult,
    localizations_from_columns,
    result_kind,
)
from imswitch.improcess.processors import _AVAILABLE_PROCESSOR_CLASSES
from imswitch.improcess.processors.colocalization import ColocalizationProcessor
from imswitch.improcess.processors.frc import FRCProcessor
from imswitch.improcess.processors.make_composite import MakeCompositeProcessor
from imswitch.improcess.processors.make_rgb import MakeRGBProcessor
from imswitch.improcess.processors.psf_resolution import PSFResolutionProcessor
from imswitch.improcess.processors.segmentation import SegmentationProcessor


class MinimalResult(ProcessingResult):
    def save(self, path: Path, fmt: str):
        pass


def _gaussian(shape=(24, 24), center=(10.0, 12.0), sigma=(1.5, 2.0)):
    yy, xx = np.mgrid[: shape[0], : shape[1]]
    return 5.0 + 80.0 * np.exp(
        -(
            ((yy - center[0]) ** 2) / (2.0 * sigma[0] ** 2)
            + ((xx - center[1]) ** 2) / (2.0 * sigma[1] ** 2)
        )
    )


def _image_2d():
    return ArrayProcessingResult(
        name="plane", data=_gaussian(), axis_labels=["Y", "X"]
    )


def _image_t_stack():
    data = np.stack([_gaussian(), _gaussian(center=(11.0, 12.5))], axis=0)
    return ArrayProcessingResult(name="stack", data=data, axis_labels=["T", "Y", "X"])


def _image_c_stack(channels=3):
    data = np.stack([_gaussian() * (i + 1) for i in range(channels)], axis=0)
    return ArrayProcessingResult(name="chans", data=data, axis_labels=["C", "Y", "X"])


def _sample_locs(count: int = 4) -> np.recarray:
    rng = np.random.default_rng(0)
    return localizations_from_columns(
        {
            "frame": np.arange(count),
            "x_nm": rng.uniform(0, 1000, count),
            "y_nm": rng.uniform(0, 1000, count),
            "sigma_x_nm": np.full(count, 120.0),
            "sigma_y_nm": np.full(count, 120.0),
            "photons": rng.uniform(500, 1500, count),
        }
    )


def _representative_results():
    """One real result per semantic kind, produced by the real processors."""
    return {
        "image": _image_2d(),
        "labels": SegmentationProcessor().apply(_image_2d(), {}),
        "table": PSFResolutionProcessor().apply(
            _image_2d(), {"pixel_size": 1.0, "unit": "px"}
        ),
        "curve": FRCProcessor().apply(
            _image_2d(), {"mode": "single-image", "pixel_size": 1.0}
        ),
        "localization": LocalizationResult(
            "locs", _sample_locs(), pixel_size_nm=100.0
        ),
        "rgb": MakeRGBProcessor().apply(_image_c_stack(3), {}),
        "composite": MakeCompositeProcessor().apply(_image_c_stack(2), {}),
    }


def _all_processors():
    return [cls() for cls in _AVAILABLE_PROCESSOR_CLASSES.values()]


def test_representative_results_declare_their_semantic_kind():
    for expected_kind, result in _representative_results().items():
        assert result_kind(result) == expected_kind


def test_every_processor_declares_known_kinds():
    for processor in _all_processors():
        assert processor.kinds, processor.id
        assert set(processor.kinds) <= set(RESULT_KINDS), processor.id


def test_duck_typed_results_default_to_image_kind():
    plain = MinimalResult(name="legacy", data=_gaussian(), axis_labels=["Y", "X"])
    assert result_kind(plain) == "image"
    assert result_kind(object()) == "image"


#: Processors that operate on localization tables (LocalizationResult).
LOCALIZATION_PROCESSOR_IDS = {"smlm-render", "smlm-filter", "smlm-drift", "smlm-group"}
#: Processors that operate on label masks (segmentation output).
#:
#: ``image-calculator`` is here because a label image is an array of numbers on
#: the same grid as the image it came from, and multiplying one into the other
#: is how a mask gets applied. The gate exists to keep a metrics *table* away
#: from an image processor, not to stop a mask reaching the image it was drawn
#: on. The other entries genuinely reinterpret the values as labels.
LABELS_PROCESSOR_IDS = {"label-morphology", "image-calculator"}


def test_non_image_results_are_never_offered_to_image_processors():
    """The load-bearing property: table/curve/rgb results match no processor;
    labels results match only the morphology post-processing and the
    calculator, and localization results match only the SMLM table/render
    processors."""
    results = _representative_results()
    for processor in _all_processors():
        for kind_name in ("table", "curve", "rgb"):
            assert not processor.accepts(results[kind_name]), (
                f"{processor.id} must not accept {kind_name} results"
            )
        accepts_labels = processor.accepts(results["labels"])
        assert accepts_labels == (
            processor.id in LABELS_PROCESSOR_IDS
        ), f"{processor.id} on labels"
        accepts_locs = processor.accepts(results["localization"])
        assert accepts_locs == (
            processor.id in LOCALIZATION_PROCESSOR_IDS
        ), processor.id


def test_kind_gate_is_transparent_for_image_results():
    """For plain image results, accepts() must equal the shape gate for every
    image-kind processor — the kind check may not change which processors
    apply to images. Non-image processors (labels/localization consumers) are
    exactly the ones the kind gate must hide from images."""
    for image in (_image_2d(), _image_t_stack(), _image_c_stack()):
        for processor in _all_processors():
            if "image" in processor.kinds:
                assert processor.accepts(image) == bool(processor.applies_to(image)), (
                    f"{processor.id} on {image.name}"
                )
            else:
                assert not processor.accepts(image), (
                    f"{processor.id} must not accept plain images"
                )


def test_composite_results_accepted_by_stack_and_channel_processors():
    composite = _representative_results()["composite"]
    accepted = {
        processor.id for processor in _all_processors() if processor.accepts(composite)
    }
    # Composite data is the source intensity stack (C, Y, X): axis/stack
    # operations and channel comparisons stay meaningful; drift correction
    # would need a T axis and this composite has none.
    assert accepted == {
        "stack-subset",
        "projection",
        "stack-split",
        "channel-split",
        "make-rgb",
        "colocalization",
    }


def test_component_inputs_inherit_layer_semantics():
    """A segmentation's labels component must not be offered to image
    processors; a composite's channel component remains an image."""
    results = _representative_results()

    labels_choices = results["labels"].processor_input_choices()
    component_kinds = {
        choice.id: result_kind(choice.result) for choice in labels_choices
    }
    assert component_kinds.pop("result") == "labels"
    assert set(component_kinds.values()) == {"labels"}

    composite_choices = results["composite"].processor_input_choices()
    channel_components = [
        choice for choice in composite_choices if choice.id != "result"
    ]
    assert channel_components
    assert all(
        result_kind(choice.result) == "image" for choice in channel_components
    )
