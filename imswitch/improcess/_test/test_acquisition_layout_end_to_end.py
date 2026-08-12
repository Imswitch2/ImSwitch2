"""The imcontrol/improcess seam, exercised whole.

Every other test covers one side of the boundary: producers build layouts,
transport round-trips them, reconstructors consume them. Nothing walked the
whole path, which is how a producer that silently dropped the line-step
dimension survived seven pull requests and a code review -- the gap only
appears when the same layout is built by a producer, accepted by the recording
gate, written to a file, resolved by the reader, and finally placed by a
reconstructor.

These tests are deliberately end to end for that reason. They use the real
builders, the real recording gate, the real storer and the real resolver.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
import zarr

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    encode_acquisition_layout,
)
from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
    build_advanced_scan_layouts,
    build_point_scan_layouts,
    build_triggerscope_raster_layouts,
)
from imswitch.imcontrol.model.managers.RecordingManager import ZarrStorer
from imswitch.improcess.model import DataObj
from imswitch.improcess.reconstructors.beadrec import BeadRecReconstructor
from imswitch.improcess.reconstructors.monalisa.coeffs_to_image import (
    coeffs_to_image_from_placement,
    placement_from_layout,
)


def _advanced_scan_info(x=18, y=18, linesteps=2):
    return {
        "img_dims": [x, y],
        "img_axes_phys": ["x", "y"],
        "pixel_sizes": [0.05, 0.05],
        "n_linesteps": linesteps,
    }


def _record(tmp_path, layout, frames, detector="CAM"):
    """Write a detector array the way RecordingManager's Zarr storer does."""
    path = tmp_path / "recording.zarr"
    root = zarr.group(store=ZarrStorer._make_store(str(path)), overwrite=True)
    array = ZarrStorer._create_array(root, detector, data=frames, chunks=(1, *frames.shape[1:]))
    array.attrs["AcquisitionLayout:schema"] = ACQUISITION_LAYOUT_SCHEMA
    array.attrs["AcquisitionLayout:json"] = encode_acquisition_layout(layout)
    array.attrs["writing"] = False
    array.attrs["recording:completion_outcome"] = "complete"
    return DataObj(path.name, detector, path=str(path))


def _ramp(frames, height=4, width=4):
    """One frame per event, uniformly valued with its own index."""
    return np.arange(frames, dtype=np.float32)[:, None, None] * np.ones(
        (frames, height, width), dtype=np.float32
    )


class _StubDetectors:
    """The only thing the layout gate asks a detector is whether it assembles.

    A real DetectorsManager instantiates camera mocks and their threads, and
    doing that inside the ImProcess suite deadlocks -- the same reason the two
    suites are run in separate pytest processes.
    """

    def __init__(self, names, scan_driven=()):
        self._detectors = {
            name: SimpleNamespace(isScanDriven=name in scan_driven) for name in names
        }

    def __getitem__(self, name):
        return self._detectors[name]


def _gate(layout, *, detector="CAM", recFrames, numCamTTL):
    """Run RecordingManager's pre-open validation, nothing else."""
    from imswitch.imcontrol.model import RecMode, RecordingManager, SaveMode

    manager = RecordingManager(_StubDetectors([detector]))
    opened: list[bool] = []
    manager._RecordingManager__prepareRecordingThread = lambda: opened.append(True)
    manager.startRecording(
        detectorNames=[detector],
        recMode=RecMode.ScanOnce,
        savename="seam-probe",
        saveMode=SaveMode.RAM,
        attrs={detector: {}},
        recFrames=recFrames,
        numCamTTL={detector: numCamTTL},
        acquisitionLayouts={detector: layout},
    )
    return opened


def test_advanced_line_step_scan_survives_the_whole_seam(tmp_path):
    """The motivating case, from producer to reconstructed image.

    frame = ((scan_y * 2 + condition) * 18) + scan_x. Every step has to agree
    for the A/B interleave to come out right at the end.
    """
    rows = cols = 18
    layout = build_advanced_scan_layouts(
        _advanced_scan_info(cols, rows, 2),
        ("CAM",),
        scan_source="ScanControllerAdvanced",
        detector_masks={"CAM": [True, True]},
        pulse_counts_by_condition={"CAM": [1, 1]},
    )["CAM"]
    assert [loop.kind for loop in layout.event_loops] == ["scan_y", "condition", "scan_x"]

    # The recording gate accepts it: 324 scan positions, 2 camera pulses each.
    assert _gate(layout, recFrames=rows * cols, numCamTTL=2) == [True]

    data_obj = _record(tmp_path, layout, _ramp(rows * cols * 2))
    resolved = data_obj.acquisition_layout
    assert resolved.is_authoritative
    assert resolved.confidence == "certain"

    placement = placement_from_layout(resolved.layout)
    image = coeffs_to_image_from_placement(
        np.arange(rows * cols * 2, dtype=np.float32).reshape(-1, 1, 1), placement
    )

    assert image.shape == (2, 1, rows, cols)
    y, x = np.mgrid[0:rows, 0:cols]
    for condition in (0, 1):
        np.testing.assert_allclose(image[condition, 0], (y * 2 + condition) * cols + x)


def test_advanced_scan_refuses_a_detector_it_cannot_describe(tmp_path):
    """A detector outside the line-step program has no describable order.

    The builder used to fall back to a condition-free layout, which describes
    a 648-frame scan as 324 events. The recording was then refused with a
    message about pulse counts, blaming the wrong thing.
    """
    with pytest.raises(ValueError, match="line steps"):
        build_advanced_scan_layouts(
            _advanced_scan_info(18, 18, 2),
            ("CAM",),
            scan_source="ScanControllerAdvanced",
            detector_masks={},
            pulse_counts_by_condition={},
        )


def test_a_single_line_step_scan_still_needs_no_mask(tmp_path):
    """With one line step there is no condition dimension to lose."""
    layout = build_advanced_scan_layouts(
        _advanced_scan_info(4, 3, 1),
        ("CAM",),
        scan_source="ScanControllerAdvanced",
        detector_masks={},
        pulse_counts_by_condition={},
    )["CAM"]

    assert [loop.kind for loop in layout.event_loops] == ["scan_y", "scan_x"]
    assert _gate(layout, recFrames=12, numCamTTL=1) == [True]


def test_gated_detector_survives_the_whole_seam(tmp_path):
    """A detector recorded on one line step reconstructs that condition."""
    rows = cols = 6
    layout = build_advanced_scan_layouts(
        _advanced_scan_info(cols, rows, 2),
        ("CAM",),
        scan_source="ScanControllerAdvanced",
        detector_masks={"CAM": [False, True]},
        pulse_counts_by_condition={"CAM": [0, 1]},
    )["CAM"]
    assert layout.recorded_event_spans is not None

    assert _gate(layout, recFrames=rows * cols, numCamTTL=1) == [True]

    data_obj = _record(tmp_path, layout, _ramp(rows * cols))
    placement = placement_from_layout(data_obj.acquisition_layout.layout)

    # Every stored frame belongs to condition B, and none is dropped.
    assert len(placement.slots) == rows * cols
    assert {slot[0] for slot in placement.slots} == {1}


def test_point_scan_survives_the_whole_seam(tmp_path):
    """A plain raster reaches BeadRec with its geometry and calibration."""
    rows, cols = 3, 4
    layout = build_point_scan_layouts(
        {
            "img_dims": [cols, rows],
            "img_axes_phys": ["x", "y"],
            "pixel_sizes": [0.1, 0.1],
        },
        ("CAM",),
        scan_source="ScanControllerPointScan",
    )["CAM"]

    assert _gate(layout, recFrames=rows * cols, numCamTTL=1) == [True]

    data_obj = _record(tmp_path, layout, _ramp(rows * cols))
    result = BeadRecReconstructor().process(data_obj, {"fit_model": "none"})

    assert result.metadata["geometry_source"] == "layout"
    assert result.metadata["scan_dims"] == (cols, rows)
    np.testing.assert_allclose(
        result.data, np.arange(rows * cols, dtype=np.float32).reshape(rows, cols)
    )


def test_triggerscope_raster_survives_the_whole_seam(tmp_path):
    """The raster producer's X-fast/Y-slow order reaches reconstruction."""
    rows, cols = 3, 4
    layout = build_triggerscope_raster_layouts(
        ("CAM",),
        dimensions=(cols, rows),
        step_sizes=(0.1, 0.2),
        scan_source="TriggerScopeRasterController",
    )["CAM"]

    data_obj = _record(tmp_path, layout, _ramp(rows * cols))
    resolved = data_obj.acquisition_layout

    assert resolved.is_authoritative
    kinds = [loop.kind for loop in resolved.layout.event_loops]
    assert kinds.index("scan_y") < kinds.index("scan_x"), "X must be the fast axis"


def test_a_layout_the_gate_rejects_never_reaches_a_file(tmp_path):
    """The gate is the last point where a wrong layout is cheap to catch."""
    from imswitch.imcommon.model.acquisition_layout import AcquisitionLayoutError

    layout = build_advanced_scan_layouts(
        _advanced_scan_info(18, 18, 2),
        ("CAM",),
        scan_source="ScanControllerAdvanced",
        detector_masks={"CAM": [True, True]},
        pulse_counts_by_condition={"CAM": [1, 1]},
    )["CAM"]

    # 324 scan positions is right, but the layout selects two frames each.
    with pytest.raises(AcquisitionLayoutError) as error:
        _gate(layout, recFrames=18 * 18, numCamTTL=1)

    assert "DETECTOR_PULSE_COUNT_MISMATCH" in {
        issue.code for issue in error.value.issues
    }


def test_live_geometry_uses_the_producers_rounding_convention():
    """The live reader must count scan positions the way the scan does.

    ImControl unified every axis-count computation onto round(); the live
    source kept ceil(). A 0.52 um axis at 0.05 um is 10 positions to the
    producer and was 11 to the reader, so a 10x10 = 100-frame stack was read
    as needing 121 frames and never completed.
    """
    from imswitch.imcontrol.model.scan_parameters import pixels_for_length_step
    from imswitch.improcess.live.sources import _derive_scan_frames_per_stack

    for length in (0.50, 0.52, 0.55, 0.58, 0.61):
        attrs = {
            "ScanStage:axis_length": [length, length, 1.0],
            "ScanStage:axis_step_size": [0.05, 0.05, 1.0],
        }
        expected = pixels_for_length_step(length, 0.05) ** 2
        assert _derive_scan_frames_per_stack(attrs) == expected, length


def test_live_stack_size_comes_from_the_resolved_layout(tmp_path):
    """One interpretation: the layout that describes the stack also sizes it."""
    from imswitch.improcess.live.sources import ZarrLiveSource

    rows = cols = 6
    layout = build_point_scan_layouts(
        {
            "img_dims": [cols, rows],
            "img_axes_phys": ["x", "y"],
            "pixel_sizes": [0.05, 0.05],
        },
        ("CAM",),
        scan_source="ScanControllerPointScan",
    )["CAM"]

    path = tmp_path / "live.zarr"
    root = zarr.group(store=ZarrStorer._make_store(str(path)), overwrite=True)
    array = ZarrStorer._create_array(root, "CAM", data=_ramp(rows * cols), chunks=(1, 4, 4))
    array.attrs["AcquisitionLayout:schema"] = ACQUISITION_LAYOUT_SCHEMA
    array.attrs["AcquisitionLayout:json"] = encode_acquisition_layout(layout)
    array.attrs["writing"] = False
    # Deliberately contradictory: the legacy attribute says something else.
    array.attrs["recording:frames_per_stack"] = 999

    info = ZarrLiveSource(detector_name="CAM").open(str(path))

    assert info.frames_per_stack == rows * cols
    assert info.acquisition_layout.is_authoritative


def test_stage_geometry_alone_is_interpreted_by_the_resolver():
    """The resolver must understand what the modules it replaces understood.

    Confining interpretation to the resolver is only safe if it is at least as
    capable. Before the ScanStage-only adapter, a file carrying stage geometry
    but no ScanTTL counts resolved to a bare frame stream, so routing the live
    sources through the resolver would have lost geometry they could read.
    """
    from imswitch.improcess.model.acquisition_layout_resolver import (
        resolve_acquisition_layout,
    )

    attrs = {
        "ScanStage:axis_length": [0.5, 0.5, 1.0],
        "ScanStage:axis_step_size": [0.05, 0.05, 1.0],
        "ScanStage:axis_startpos": [0.0, 0.0, 0.0],
    }

    resolved = resolve_acquisition_layout(attrs, shape=(100, 8, 8), detector="CAM")

    assert resolved.source == "scan-stage-legacy"
    assert resolved.is_usable
    # It describes the scan, but it is inference: it may not refuse anything.
    assert not resolved.is_authoritative
    counts = {loop.kind: loop.count for loop in resolved.layout.event_loops}
    assert counts["scan_x"] == 10 and counts["scan_y"] == 10


def test_stage_geometry_that_cannot_explain_the_frames_is_declined():
    """Nothing claims this is a scan, so an unexplained count is not an error."""
    from imswitch.improcess.model.acquisition_layout_resolver import (
        resolve_acquisition_layout,
    )

    attrs = {
        "ScanStage:axis_length": [0.5, 0.5, 1.0],
        "ScanStage:axis_step_size": [0.05, 0.05, 1.0],
        "ScanStage:axis_startpos": [0.0, 0.0, 0.0],
    }

    resolved = resolve_acquisition_layout(attrs, shape=(37, 8, 8), detector="CAM")

    assert resolved.source != "scan-stage-legacy"
    assert not resolved.is_usable


def test_live_session_geometry_comes_from_an_inferred_layout_too():
    """The resolver decides, even when its answer is an inference.

    The session kept its own ladder over the same ScanStage attributes. Both
    now agree because only one of them reads the file.
    """
    from imswitch.improcess.model.acquisition_layout_resolver import (
        resolve_acquisition_layout,
    )
    from imswitch.improcess.reconstructors.base import StackInfo
    from imswitch.improcess.reconstructors.monalisa.live_session import (
        MonalisaLiveSession,
    )

    attrs = {
        "ScanStage:axis_length": [0.5, 0.5, 1.0],
        "ScanStage:axis_step_size": [0.05, 0.05, 1.0],
        "ScanStage:axis_startpos": [0.0, 0.0, 0.0],
    }
    stack_info = StackInfo(
        frame_shape=(8, 8),
        dtype=np.dtype(np.uint16),
        acquisition_layout=resolve_acquisition_layout(
            attrs, shape=(100, 8, 8), detector="CAM"
        ),
    )

    assert MonalisaLiveSession._geometry_from_recorded_layout(stack_info) == (10, 10, 1)


def test_an_inferred_shape_the_fast_path_cannot_hold_is_declined_not_refused():
    """A guess must never fail a file; only a declaration may."""
    from imswitch.imcommon.model.acquisition_layout import (
        ACQUISITION_LAYOUT_SCHEMA,
        PAYLOAD_DETECTOR_FRAME_STREAM,
        AcquisitionLayout,
        AcquisitionLoop,
    )
    from imswitch.improcess.model.acquisition_layout_resolver import (
        ResolvedAcquisitionLayout,
    )
    from imswitch.improcess.reconstructors.base import StackInfo
    from imswitch.improcess.reconstructors.monalisa.live_session import (
        MonalisaLiveSession,
    )

    # A Z stack: the contiguous X/Y fast path cannot represent it.
    loops = (
        AcquisitionLoop("scan_z", "scan_z", 3),
        AcquisitionLoop("scan_y", "scan_y", 4),
        AcquisitionLoop("scan_x", "scan_x", 5),
    )
    body = dict(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="CAM",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=loops,
    )

    inferred = StackInfo(
        frame_shape=(8, 8),
        dtype=np.dtype(np.uint16),
        acquisition_layout=ResolvedAcquisitionLayout(
            layout=AcquisitionLayout(provenance="legacy-adapter", **body),
            source="scan-stage-legacy",
            confidence="high",
        ),
    )
    declared = StackInfo(
        frame_shape=(8, 8),
        dtype=np.dtype(np.uint16),
        acquisition_layout=ResolvedAcquisitionLayout(
            layout=AcquisitionLayout(provenance="recorded", **body),
            source="explicit-metadata",
            confidence="certain",
        ),
    )

    assert MonalisaLiveSession._geometry_from_recorded_layout(inferred) is None
    with pytest.raises(ValueError, match="Z slices"):
        MonalisaLiveSession._geometry_from_recorded_layout(declared)
