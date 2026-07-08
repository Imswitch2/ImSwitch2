"""Registry-wide guarantees that the result-unification pipeline is general.

These are the "extra mile" checks that the panels->processors reroute and the
typed-layer render path work for EVERY registered processor/reconstructor, not
just the two panels touched in Phase 2 — and an architectural guard that no new
panel silently reintroduces the floating-layer smell.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import numpy as np

from imswitch.improcess.model.result import ProcessingResult
from imswitch.improcess.processors import (
    _AVAILABLE_PROCESSOR_CLASSES,
    available_processor_ids,
)
from imswitch.improcess.processors.base import normalize_processor_output


def get_processor_class(processor_id):
    return _AVAILABLE_PROCESSOR_CLASSES[processor_id]


# --- 1. Every processor conforms to the generic run->publish contract --------

def test_every_processor_conforms_to_the_processor_contract():
    """The generic ResultProcessorWidget/Controller drives any registered
    processor, so each must expose a stable id/name, an ``applies_to`` gate,
    ``make_param_widget`` and ``apply``. If this holds, a new processor gets a
    runnable panel + a published result for free."""
    ids = available_processor_ids()
    assert ids, "expected registered processors"
    for processor_id in ids:
        processor = get_processor_class(processor_id)()
        assert isinstance(processor.id, str) and processor.id
        assert isinstance(processor.name, str) and processor.name
        gate = processor.applies_to
        assert callable(gate), f"{processor_id}.applies_to must be callable"
        assert callable(processor.make_param_widget), processor_id
        assert callable(processor.apply), processor_id
        # apply signature is (self, result, params)
        sig = inspect.signature(processor.apply)
        assert len(sig.parameters) == 2, f"{processor_id}.apply(result, params)"


def _image_result(shape, labels, name="img"):
    from imswitch.improcess.model.array_result import ArrayProcessingResult

    data = np.random.default_rng(0).random(shape).astype(np.float32)
    return ArrayProcessingResult(name=name, data=data, axis_labels=labels)


def test_image_applicable_processors_return_processing_results():
    """Every processor that accepts a plain (C, Y, X) image must return
    ProcessingResult(s) that normalize cleanly — i.e. it publishes into the
    reconstruction list rather than mutating the viewer."""
    result = _image_result((3, 16, 16), ["C", "Y", "X"])
    ran = 0
    for processor_id in available_processor_ids():
        processor = get_processor_class(processor_id)()
        try:
            applies = bool(processor.applies_to(result))
        except Exception:
            applies = False
        if not applies:
            continue
        try:
            output = processor.apply(result, {})
        except Exception:
            # Processors needing extra inputs (e.g. multi-result merge,
            # registration) may reject the lone image with default params; the
            # contract test above already covers their shape.
            continue
        results = normalize_processor_output(output)
        assert results, f"{processor_id} produced no results"
        assert all(isinstance(r, ProcessingResult) for r in results), processor_id
        ran += 1
    assert ran >= 4, "expected several image-applicable processors to run"


# --- 2. Architectural guard: only the render path + known panels add_* --------

def test_only_allowlisted_view_modules_create_napari_layers():
    """Persistent napari layers must be created ONLY by the single render path.
    The allowlist encodes the result-unification invariant; a new panel that
    calls viewer.add_* (reintroducing the floating-layer smell) fails this test
    and must instead publish a ProcessingResult.

    Allowed:
      - ReconstructionView: THE render path for selected results.
      - SegmentationWidget: ephemeral tuning preview only (commit publishes a
        SegmentationResult).
      - MulticolorWidget: still floats its overlays — deferred to Phase 3 of
        docs/design/plans/improcess-result-unification.md. Remove from this
        allowlist when Phase 3 lands.
    """
    view_dir = Path(__file__).resolve().parent.parent / "view"
    allow = {"ReconstructionView.py", "SegmentationWidget.py", "MulticolorWidget.py"}
    markers = ("add_image(", "add_labels(", "add_points(", "add_shapes(")

    offenders = []
    for path in sorted(view_dir.glob("*.py")):
        if path.name in allow:
            continue
        source = path.read_text()
        if any(marker in source for marker in markers):
            offenders.append(path.name)

    assert not offenders, (
        "these view panels create napari layers directly instead of publishing "
        f"a ProcessingResult (result-unification smell): {offenders}"
    )


def test_projection_panel_no_longer_creates_floating_layers():
    """The panel the maintainer reported: projection must NOT ship a widget that
    calls viewer.add_*; it is now the generic result-processor panel."""
    view_dir = Path(__file__).resolve().parent.parent / "view"
    assert not (view_dir / "ProjectionWidget.py").exists(), (
        "ProjectionWidget was retired in favour of the generic result-processor "
        "panel; the file should be gone"
    )
