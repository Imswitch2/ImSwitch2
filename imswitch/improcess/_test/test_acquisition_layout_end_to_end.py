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
