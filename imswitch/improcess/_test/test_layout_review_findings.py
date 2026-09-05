"""Regressions from an external review of the acquisition-layout branch.

Eight findings, all reproduced before being fixed. They are collected here
rather than scattered because they share a subject: each is a place where the
contract's own rule -- a *declared* geometry may refuse, an *inferred* one must
decline -- was applied to something it does not cover, or where a number was
carried further than the thing that produced it justified.
"""

from __future__ import annotations

import pathlib
import tempfile
import tracemalloc
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLoop,
    RecordedEventSpan,
    TraversalRule,
    recorded_frames_per_time_point,
    validate_acquisition_layout,
)
from imswitch.imcommon.model.acquisition_metadata import (
    RecordingLifecycleMarkers,
    normalize_recording_lifecycle,
)
from imswitch.improcess.model.acquisition_layout_resolver import (
    resolve_acquisition_layout,
)


def _stream(loops, traversal=None, spans=None, provenance="recorded"):
    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="CAM",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=loops,
        traversal=traversal or tuple(TraversalRule(l.id, "forward") for l in loops),
        recorded_event_spans=spans,
        provenance=provenance,
    )


# ----------------------------------------------------------------------
# 1. A legacy assembled scan may have more than two spatial axes
# ----------------------------------------------------------------------

LEGACY_3D = {
    "ScanTTL:n_linesteps": 2,
    "ScanTTL:sequence_time": 0.001,
    "ScanStage:target_device": ["X", "Y", "Z"],
    "ScanStage:axis_length": [3.0, 2.0, 2.0],
    "ScanStage:axis_step_size": [1.0, 1.0, 1.0],
    "ScanStage:axis_startpos": [0.0, 0.0, 0.0],
}


@pytest.mark.parametrize(
    "shape,expected",
    [
        ((2, 2, 2, 3), ("condition", "scan_z", "scan_y", "scan_x")),
        ((1, 2, 2, 2, 3), ("frame", "condition", "scan_z", "scan_y", "scan_x")),
    ],
)
def test_a_legacy_assembled_z_scan_resolves(shape, expected):
    """Naming only Y and X described a 3D scan with one axis too few.

    An explicit layout that disagrees with its container is refused outright,
    by design, so this made a valid point-detector recording unreadable rather
    than merely under-described.
    """
    resolved = resolve_acquisition_layout(LEGACY_3D, shape=shape, detector="APD")

    assert resolved.layout.storage_axes == expected
    assert resolved.layout.payload_kind == "assembled-image"


def test_a_two_dimensional_assembled_scan_is_unchanged():
    attrs = dict(LEGACY_3D, **{
        "ScanStage:target_device": ["X", "Y"],
        "ScanStage:axis_length": [3.0, 2.0],
        "ScanStage:axis_step_size": [1.0, 1.0],
        "ScanStage:axis_startpos": [0.0, 0.0],
    })

    resolved = resolve_acquisition_layout(attrs, shape=(2, 2, 3), detector="APD")

    assert resolved.layout.storage_axes == ("condition", "scan_y", "scan_x")


# ----------------------------------------------------------------------
# 2. An inferred layout may not refuse what the user typed
# ----------------------------------------------------------------------


def test_an_inferred_raster_does_not_veto_a_manual_one():
    from imswitch.imcommon.algorithms.bead_fits import FIT_MODELS
    from imswitch.improcess.reconstructors.beadrec.reconstructor import (
        BeadRecReconstructor,
    )

    inferred = _stream(
        (AcquisitionLoop("scan_y", "scan_y", 3), AcquisitionLoop("scan_x", "scan_x", 4)),
        provenance="legacy-adapter",
    )

    class _Source:
        acquisition_layout = SimpleNamespace(
            layout=inferred, is_usable=True, is_authoritative=False
        )
        data = np.zeros((12, 8, 8), np.float32)
        name = "legacy"
        axis_labels = ["T", "Y", "X"]
        axis_scales = None
        scale_unit = "um"

    result = BeadRecReconstructor().process(
        _Source(),
        {"scan_x": 6, "scan_y": 2, "fit_model": list(FIT_MODELS)[0]},
    )

    assert result is not None


def test_a_declared_raster_still_refuses_a_conflicting_manual_one():
    from imswitch.imcommon.algorithms.bead_fits import FIT_MODELS
    from imswitch.improcess.reconstructors.beadrec.reconstructor import (
        BeadRecReconstructor,
    )

    declared = _stream(
        (AcquisitionLoop("scan_y", "scan_y", 3), AcquisitionLoop("scan_x", "scan_x", 4))
    )

    class _Source:
        acquisition_layout = SimpleNamespace(
            layout=declared, is_usable=True, is_authoritative=True
        )
        data = np.zeros((12, 8, 8), np.float32)
        name = "recorded"
        axis_labels = ["T", "Y", "X"]
        axis_scales = None
        scale_unit = "um"

    with pytest.raises(ValueError, match="declares"):
        BeadRecReconstructor().process(
            _Source(),
            {"scan_x": 6, "scan_y": 2, "fit_model": list(FIT_MODELS)[0]},
        )


# ----------------------------------------------------------------------
# 3. Frames the writer threw away must reach the reader
# ----------------------------------------------------------------------


def test_a_recording_that_discarded_surplus_frames_does_not_read_as_clean():
    """The plan was met exactly, and frames were still thrown away.

    A camera that ran free or was pulsed more often than the scan declared
    produces a surplus the writer drops. The outcome vocabulary has no word
    for it, so the file said ``complete`` and no reader consulted the count --
    the one fact the field was added to carry.
    """
    lifecycle = normalize_recording_lifecycle(
        {
            "recording:planned_frames": 6,
            "recording:actual_frames": 6,
            "recording:discarded_frames": 12,
            "recording:completion_outcome": "complete",
            "writing": False,
        },
        RecordingLifecycleMarkers(writing=False),
    )

    assert lifecycle.discarded_frames == 12
    assert "DISCARDED_SURPLUS_FRAMES" in {issue.code for issue in lifecycle.issues}


def test_a_clean_recording_reports_nothing():
    lifecycle = normalize_recording_lifecycle(
        {
            "recording:planned_frames": 6,
            "recording:actual_frames": 6,
            "recording:discarded_frames": 0,
            "recording:completion_outcome": "complete",
            "writing": False,
        },
        RecordingLifecycleMarkers(writing=False),
    )

    assert lifecycle.discarded_frames == 0
    assert lifecycle.issues == ()


def test_an_ome_tiff_carries_the_discarded_count():
    from imswitch.imcontrol.model.managers.RecordingManager import Storer

    kept = Storer._ome_annotation_attrs({
        "recording:discarded_frames": 3,
        "recording:completion_outcome": "complete",
        "irrelevant": "dropped",
    })

    assert kept["recording:discarded_frames"] == 3
    assert "irrelevant" not in kept


# ----------------------------------------------------------------------
# 4. A few hundred bytes of metadata may not allocate hundreds of megabytes
# ----------------------------------------------------------------------


def test_an_out_of_range_span_is_rejected_without_expanding_it():
    layout = _stream(
        (AcquisitionLoop("scan_x", "scan_x", 6),),
        spans=(
            RecordedEventSpan(start=0, count=1, stride=1, period=2, repeats=300_000),
            RecordedEventSpan(start=1, count=1, stride=1, period=2, repeats=300_000),
        ),
    )

    tracemalloc.start()
    try:
        issues = validate_acquisition_layout(layout)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert "SPAN_OUT_OF_RANGE" in {issue.code for issue in issues}
    # The overlap sweep builds one run per repetition. Running it on spans that
    # cannot be valid cost ~70 MB for a layout of a few hundred bytes.
    assert peak < 5_000_000, f"expanded the spans anyway: {peak / 1e6:.1f} MB"


def test_overlapping_spans_that_fit_are_still_caught():
    layout = _stream(
        (AcquisitionLoop("scan_x", "scan_x", 12),),
        spans=(
            RecordedEventSpan(start=0, count=2, stride=1, period=4, repeats=3),
            RecordedEventSpan(start=1, count=2, stride=1, period=4, repeats=3),
        ),
    )

    assert "DUPLICATE_PRODUCER_EVENT" in {
        issue.code for issue in validate_acquisition_layout(layout)
    }


# ----------------------------------------------------------------------
# 5. Time is not necessarily outermost, nor forward
# ----------------------------------------------------------------------

_INNER_TIME = (
    AcquisitionLoop("scan_y", "scan_y", 2),
    AcquisitionLoop("time", "time", 3),
    AcquisitionLoop("scan_x", "scan_x", 4),
)


def test_a_nested_time_loop_has_no_single_stack_size():
    """Counting the frames right is not the same as being able to slice them.

    Counting until the time coordinate changed returned four of the eight, and
    counting properly returns eight -- but a reader takes the *first eight*
    frames, and with time nested inside a spatial axis those hold two time
    points, not one. The rest of time 0 is at indices 12-15. No number
    describes that stream, so none is returned.
    """
    with pytest.raises(ValueError, match="not consecutive"):
        recorded_frames_per_time_point(_stream(_INNER_TIME))


def test_a_nested_time_loop_is_declined_by_the_live_reader():
    from imswitch.improcess.live.sources import _frames_per_stack_from_layout

    resolved = SimpleNamespace(layout=_stream(_INNER_TIME), is_usable=True)

    assert _frames_per_stack_from_layout(resolved) is None


def test_an_outermost_time_loop_still_gives_a_stack_size():
    outermost = _stream((
        AcquisitionLoop("time", "time", 3),
        AcquisitionLoop("scan_y", "scan_y", 2),
        AcquisitionLoop("scan_x", "scan_x", 4),
    ))

    assert recorded_frames_per_time_point(outermost) == 8


def test_a_reversed_outermost_time_loop_gives_the_same_stack_size():
    """Which time point comes first does not change how big one is."""
    loops = (
        AcquisitionLoop("time", "time", 3),
        AcquisitionLoop("scan_y", "scan_y", 2),
        AcquisitionLoop("scan_x", "scan_x", 4),
    )
    reversed_time = _stream(
        loops,
        traversal=(
            TraversalRule("time", "reverse"),
            TraversalRule("scan_y", "forward"),
            TraversalRule("scan_x", "forward"),
        ),
    )

    assert recorded_frames_per_time_point(reversed_time) == 8


def test_a_stack_size_is_not_derived_by_walking_every_frame():
    """Two million frames took nearly three seconds to state a stack size."""
    import time

    big = _stream((
        AcquisitionLoop("time", "time", 1000),
        AcquisitionLoop("scan_y", "scan_y", 50),
        AcquisitionLoop("scan_x", "scan_x", 40),
    ))

    started = time.monotonic()
    assert recorded_frames_per_time_point(big) == 2000
    assert time.monotonic() - started < 0.5


def test_uneven_time_points_have_no_single_stack_size():
    """Returning one of them would be a number the reader waits forever for."""
    # Three frames kept from the first time point, one from the second.
    layout = _stream(
        (AcquisitionLoop("time", "time", 2), AcquisitionLoop("scan_x", "scan_x", 4)),
        spans=(
            RecordedEventSpan(start=0, count=3, stride=1),
            RecordedEventSpan(start=4, count=1, stride=1),
        ),
    )

    with pytest.raises(ValueError, match="same number"):
        recorded_frames_per_time_point(layout)


# ----------------------------------------------------------------------
# 6. A persisted override must be found again
# ----------------------------------------------------------------------


def test_a_persisted_override_survives_reopening_a_tiff(tmp_path):
    """The sidecar was keyed on the layout's detector, not the file's.

    A TIFF series is identified as ``Image0`` while the layout declares
    ``Camera``, so the override was rejected as targeting a different detector
    the moment the file was reopened -- silently restoring the metadata it was
    written to correct.
    """
    import tifffile

    from imswitch.improcess.model import DataObj

    path = tmp_path / "override_Camera.ome.tiff"
    tifffile.imwrite(str(path), np.zeros((6, 8, 8), np.uint16))
    override = replace(
        _stream(
            (AcquisitionLoop("scan_y", "scan_y", 2), AcquisitionLoop("scan_x", "scan_x", 3))
        ),
        detector="Camera",
        provenance="user-override",
    )

    name = DataObj.getDatasetNames(str(path))[0]
    assert name != override.detector, "the premise: the file names its data differently"
    DataObj(str(path), name, path=str(path)).setAcquisitionLayoutOverride(
        override, persist=True
    )

    reopened = DataObj(str(path), name, path=str(path)).acquisition_layout

    assert reopened.layout.provenance == "user-override"
    assert "OVERRIDE_DATASET_MISMATCH" not in {i.code for i in reopened.issues}
    assert [loop.count for loop in reopened.layout.event_loops] == [2, 3]


# ----------------------------------------------------------------------
# 7. Two stacks correspond only if their whole event structure does
# ----------------------------------------------------------------------


def test_polarization_stacks_of_different_shape_are_not_paired():
    from imswitch.improcess.reconstructors.widefield_starss.reconstructor import (
        _pairing_signature,
    )

    def layout(timepoints, conditions):
        return _stream((
            AcquisitionLoop("time", "time", timepoints),
            AcquisitionLoop("condition", "condition", conditions),
        ))

    # Same kinds, same frame count, different acquisitions.
    assert _pairing_signature(layout(2, 6)) != _pairing_signature(layout(3, 4))
    assert _pairing_signature(layout(2, 6)) == _pairing_signature(layout(2, 6))


def test_polarization_stacks_traversed_differently_are_not_paired():
    from imswitch.improcess.reconstructors.widefield_starss.reconstructor import (
        _pairing_signature,
    )

    loops = (
        AcquisitionLoop("time", "time", 2),
        AcquisitionLoop("condition", "condition", 6),
    )
    forward = _stream(loops)
    serpentine = _stream(
        loops,
        traversal=(
            TraversalRule("time", "forward"),
            TraversalRule("condition", "serpentine", parity_loops=("time",)),
        ),
    )

    assert _pairing_signature(forward) != _pairing_signature(serpentine)


# ----------------------------------------------------------------------
# 8. One pixel pitch may not stand for two
# ----------------------------------------------------------------------


def _calibrated(y_um, x_um):
    return SimpleNamespace(
        axis_labels=["T", "Y", "X"],
        axis_scales=[1.0, y_um, x_um],
        scale_unit="um",
    )


def test_an_anisotropic_calibration_is_refused_rather_than_halved():
    from imswitch.improcess.reconstructors.smlm.localizer import (
        AnisotropicPixelSize,
        source_pixel_size_nm,
    )

    with pytest.raises(AnisotropicPixelSize) as error:
        source_pixel_size_nm(_calibrated(0.065, 0.100))

    message = str(error.value)
    assert "65" in message and "100" in message


def test_a_square_calibration_is_used():
    from imswitch.improcess.reconstructors.smlm.localizer import source_pixel_size_nm

    assert source_pixel_size_nm(_calibrated(0.100, 0.100)) == 100.0


def test_a_manual_pixel_size_settles_an_anisotropic_source():
    from imswitch.improcess.reconstructors.smlm.localizer import SmlmLocalizer

    value, calibration = SmlmLocalizer._pixel_size_nm(
        _calibrated(0.065, 0.100), {"pixel_size_nm": 80}
    )

    assert value == 80.0
    assert calibration["pixel_size_source"] == "manual-anisotropic-source"


def test_without_a_manual_pixel_size_an_anisotropic_source_refuses():
    from imswitch.improcess.reconstructors.smlm.localizer import (
        AnisotropicPixelSize,
        SmlmLocalizer,
    )

    with pytest.raises(AnisotropicPixelSize):
        SmlmLocalizer._pixel_size_nm(_calibrated(0.065, 0.100), {})


# ----------------------------------------------------------------------
# Second review: what the first round's fixes did not reach
# ----------------------------------------------------------------------


def test_a_valid_layout_is_validated_without_expanding_its_spans():
    """Range-checking first only helped spans that were already invalid.

    Two spans of three frames per position over a hundred-thousand-position
    scan are an ordinary gated acquisition, entirely in range, and comparing
    them built one run per repetition: six hundred thousand of them, seventy
    megabytes, a second and a half. Each span is now cut along its shorter
    edge, so the work follows ``min(repeats, count)``.
    """
    import time

    layout = _stream(
        (AcquisitionLoop("scan_x", "scan_x", 3_000_000),),
        spans=(
            RecordedEventSpan(start=0, count=3, stride=1, period=10, repeats=300_000),
            RecordedEventSpan(start=5, count=3, stride=1, period=10, repeats=300_000),
        ),
    )

    tracemalloc.start()
    started = time.monotonic()
    try:
        issues = validate_acquisition_layout(layout)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert [issue.code for issue in issues] == []
    assert peak < 5_000_000, f"expanded the spans: {peak / 1e6:.1f} MB"
    assert time.monotonic() - started < 1.0


def test_overlaps_are_still_caught_when_spans_are_cut_the_short_way():
    layout = _stream(
        (AcquisitionLoop("scan_x", "scan_x", 10_000),),
        spans=(
            RecordedEventSpan(start=0, count=3, stride=1, period=10, repeats=1000),
            RecordedEventSpan(start=2, count=3, stride=1, period=10, repeats=1000),
        ),
    )

    assert "DUPLICATE_PRODUCER_EVENT" in {
        issue.code for issue in validate_acquisition_layout(layout)
    }


def test_condition_labels_in_the_other_order_are_not_the_same_pairing():
    """Same shape, opposite meaning: signal paired with background."""
    from imswitch.improcess.reconstructors.widefield_starss.reconstructor import (
        _pairing_signature,
    )

    def labelled(order):
        return _stream((
            AcquisitionLoop("time", "time", 2),
            AcquisitionLoop("condition", "condition", 2, labels=order),
        ))

    assert _pairing_signature(labelled(("signal", "background"))) != _pairing_signature(
        labelled(("background", "signal"))
    )
    assert _pairing_signature(labelled(("signal", "background"))) == _pairing_signature(
        labelled(("signal", "background"))
    )


@pytest.mark.parametrize("value", [1.9, -3, "twelve"])
def test_a_malformed_discarded_count_is_reported_not_dropped(value):
    """A corrupt marker used to be indistinguishable from an absent one.

    It went through a lenient parser of its own -- ``1.9`` became ``1``, a
    negative became ``None`` in silence -- while every other count in the same
    record went through one strict path that says so.
    """
    lifecycle = normalize_recording_lifecycle(
        {
            "recording:planned_frames": 6,
            "recording:actual_frames": 6,
            "recording:discarded_frames": value,
            "recording:completion_outcome": "complete",
            "writing": False,
        },
        RecordingLifecycleMarkers(writing=False),
    )

    assert lifecycle.discarded_frames is None
    assert "INVALID_RECORDING_COUNT" in {issue.code for issue in lifecycle.issues}


def test_an_ome_tiff_reads_the_discarded_count_back_as_a_number(tmp_path):
    """Left out of the numeric set it came back as text, which the strict
    parser then rejected -- the field reporting thrown-away frames became a
    report that the metadata was invalid."""
    import numpy as np
    import tifffile

    from imswitch.imcontrol.model.managers.recording_metadata import (
        MODE_TIMELAPSE,
        build_ome_image_meta,
    )
    from imswitch.imcommon.model import ome_metadata as _ome
    from imswitch.improcess.model.image_sources import _ome_map_annotation_attrs

    meta = build_ome_image_meta(
        "Cam", MODE_TIMELAPSE, 2, pixel_size_yx_um=(0.1, 0.1), dtype=np.uint16
    )
    meta.annotations.update({
        "recording:discarded_frames": 7,
        "recording:actual_frames": 2,
    })
    path = tmp_path / "annotated_Cam.ome.tiff"
    tifffile.imwrite(str(path), np.zeros((2, 4, 4), np.uint16))
    tifffile.tiffcomment(str(path), _ome.build_ome_xml(meta, (2, 4, 4)))

    with tifffile.TiffFile(str(path)) as handle:
        annotations = _ome_map_annotation_attrs(handle, 0)

    assert annotations["recording:discarded_frames"] == 7
