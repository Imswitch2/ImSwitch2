from __future__ import annotations

from types import SimpleNamespace

import h5py
import numpy as np
import pytest

from imswitch.imcommon.model.acquisition_layout import (
    ACQUISITION_LAYOUT_SCHEMA,
    PAYLOAD_DETECTOR_FRAME_STREAM,
    AcquisitionLayout,
    AcquisitionLoop,
    RecordedEventSpan,
    encode_acquisition_layout,
    recorded_frame_coordinates,
)
from imswitch.improcess.model.acquisition_layout_resolver import (
    AcquisitionLayoutResolutionError,
    adapt_tiling_manifest,
    persist_layout_override,
    resolve_acquisition_layout,
)
from imswitch.improcess.model.DataObj import DataObj
from imswitch.improcess.reconstructors.base import (
    AcquisitionPreflightError,
    AcquisitionRequirements,
    Reconstructor,
    SourceInspection,
    preflight_acquisition_layout,
)


def _recorded_layout(*, provenance: str = "recorded") -> AcquisitionLayout:
    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="Cam",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(AcquisitionLoop("time", "time", 6),),
        provenance=provenance,
    )


def _advanced_attrs(*, detector_mask=None):
    attrs = {
        "ScanStage:target_device": ["X", "Y"],
        "ScanStage:axis_length": [18, 18],
        "ScanStage:axis_step_size": [1, 1],
        "ScanStage:positive_direction": [True, True],
        "ScanTTL:n_linesteps": 2,
    }
    if detector_mask is not None:
        attrs["ScanTTL:linestep_enable"] = {"Cam": detector_mask}
    return attrs


def _codes(result_or_error) -> set[str]:
    return {issue.code for issue in result_or_error.issues}


def test_explicit_layout_wins_and_reports_lossy_ome_time_projection():
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="Cam",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(AcquisitionLoop("condition", "condition", 6),),
    )

    resolved = resolve_acquisition_layout(
        {
            "AcquisitionLayout:schema": ACQUISITION_LAYOUT_SCHEMA,
            "AcquisitionLayout:json": encode_acquisition_layout(layout),
            "ScanTTL:n_linesteps": 2,
        },
        shape=(6, 2, 3),
        detector="Cam",
        axis_labels=("T", "Y", "X"),
        axis_metadata_explicit=True,
    )

    assert resolved.layout == layout
    assert resolved.source == "explicit-metadata"
    assert resolved.confidence == "certain"
    assert "LOSSY_OME_TIME_PROJECTION" in _codes(resolved)


def test_invalid_explicit_layout_never_falls_through_to_legacy_metadata():
    with pytest.raises(AcquisitionLayoutResolutionError) as exc_info:
        resolve_acquisition_layout(
            {
                "AcquisitionLayout:schema": ACQUISITION_LAYOUT_SCHEMA,
                "AcquisitionLayout:json": "{not json",
                **_advanced_attrs(),
            },
            shape=(648, 2, 3),
            detector="Cam",
        )

    assert "INVALID_EXPLICIT_LAYOUT" in _codes(exc_info.value)


def test_valid_user_override_recovers_invalid_explicit_metadata():
    override = _recorded_layout(provenance="user-override")

    resolved = resolve_acquisition_layout(
        {
            "AcquisitionLayout:schema": ACQUISITION_LAYOUT_SCHEMA,
            "AcquisitionLayout:json": "{not json",
        },
        shape=(6, 2, 3),
        detector="Cam",
        user_override=override,
    )

    assert resolved.layout == override
    assert resolved.source == "user-override"
    assert resolved.confidence == "certain"
    assert "INVALID_EXPLICIT_LAYOUT" in _codes(resolved)
    assert "USER_OVERRIDE_RECOVERS_INVALID_LAYOUT" in _codes(resolved)
    assert all(issue.severity != "error" for issue in resolved.issues)


def test_advanced_18_by_18_by_2_uses_line_steps_not_timepoints():
    resolved = resolve_acquisition_layout(
        _advanced_attrs(),
        shape=(648, 2, 3),
        detector="Cam",
    )

    assert [loop.kind for loop in resolved.layout.event_loops] == [
        "scan_y",
        "condition",
        "scan_x",
    ]
    assert recorded_frame_coordinates(resolved.layout, 0) == {
        "scan_y": 0,
        "condition": 0,
        "scan_x": 0,
    }
    assert recorded_frame_coordinates(resolved.layout, 17)["condition"] == 0
    assert recorded_frame_coordinates(resolved.layout, 18)["condition"] == 1
    assert recorded_frame_coordinates(resolved.layout, 36) == {
        "scan_y": 1,
        "condition": 0,
        "scan_x": 0,
    }


def test_advanced_detector_gate_canonicalizes_to_periodic_b_span():
    resolved = resolve_acquisition_layout(
        _advanced_attrs(detector_mask=[False, True]),
        shape=(324, 2, 3),
        detector="Cam",
    )

    assert resolved.layout.recorded_event_spans == (
        RecordedEventSpan(start=18, count=18, period=36, repeats=18),
    )
    assert recorded_frame_coordinates(resolved.layout, 0)["condition"] == 1
    assert recorded_frame_coordinates(resolved.layout, 18) == {
        "scan_y": 1,
        "condition": 1,
        "scan_x": 0,
    }


def test_advanced_per_line_detector_gate_preserves_producer_coordinates():
    mask = [
        enabled
        for row in range(18)
        for enabled in (row % 2 == 1, row % 2 == 0)
    ]
    resolved = resolve_acquisition_layout(
        _advanced_attrs(detector_mask=mask),
        shape=(324, 2, 3),
        detector="Cam",
    )

    assert recorded_frame_coordinates(resolved.layout, 0) == {
        "scan_y": 0,
        "condition": 1,
        "scan_x": 0,
    }
    assert recorded_frame_coordinates(resolved.layout, 18) == {
        "scan_y": 1,
        "condition": 0,
        "scan_x": 0,
    }


def test_advanced_assembled_condition_image_maps_loops_to_storage_axes():
    resolved = resolve_acquisition_layout(
        _advanced_attrs(),
        shape=(2, 18, 18),
        detector="APD",
    )

    assert resolved.layout.payload_kind == "assembled-image"
    assert resolved.layout.storage_axes == ("condition", "scan_y", "scan_x")
    assert [loop.storage_axis for loop in resolved.layout.event_loops] == [
        "condition",
        "scan_y",
        "scan_x",
    ]


def test_triggerscope_assembled_raster_keeps_scan_axes():
    resolved = resolve_acquisition_layout(
        {
            "ScanStage:target_device": ["X", "Y"],
            "ScanStage:axis_length": [4, 3],
            "ScanStage:axis_step_size": [1, 1],
            "ScanTTL:sequence_time": 0.001,
        },
        shape=(3, 4),
        detector="APD",
    )

    assert resolved.layout.payload_kind == "assembled-image"
    assert resolved.layout.storage_axes == ("scan_y", "scan_x")
    assert [loop.kind for loop in resolved.layout.event_loops] == ["scan_y", "scan_x"]


def test_ambiguous_legacy_geometry_blocks_instead_of_guessing_time():
    attrs = _advanced_attrs()
    attrs["ScanStage:axis_startpos"] = [0, 0]

    with pytest.raises(AcquisitionLayoutResolutionError) as exc_info:
        resolve_acquisition_layout(
            attrs,
            shape=(650, 2, 3),
            detector="Cam",
        )

    assert "AMBIGUOUS_LEGACY_SCAN_GEOMETRY" in _codes(exc_info.value)


def test_legacy_axis_activated_only_to_fit_the_frame_count_is_reported():
    """A trailing axis chosen to make the arithmetic work is a guess.

    The size convention (length/step) gives the third stage axis one position;
    the endpoint convention ((length-start)/step + 1) gives two, and only the
    latter explains 200 frames. Two Z steps and two timepoints are not
    interchangeable, so the adapter has to say that it chose.
    """
    attrs = {
        "ScanStage:axis_startpos": [0.0, 0.0, 0.0],
        "ScanStage:axis_length": [0.55, 0.55, 1.0],
        "ScanStage:axis_step_size": [0.05, 0.05, 1.0],
        "ScanTTL:Nx": 10,
        "ScanTTL:Ny": 10,
    }

    resolved = resolve_acquisition_layout(attrs, shape=(200, 100, 100), detector="Cam")

    assert "AMBIGUOUS_LEGACY_TRAILING_AXIS" in _codes(resolved)
    # High confidence would claim the recording states this; it does not.
    assert resolved.confidence == "medium"
    assert [(loop.kind, loop.count) for loop in resolved.layout.event_loops] == [
        ("scan_z", 2),
        ("scan_y", 10),
        ("scan_x", 10),
    ]


def test_a_genuine_legacy_z_stack_keeps_full_adapter_confidence():
    """Both conventions agree this axis moved, so nothing was guessed."""
    attrs = {
        "ScanStage:axis_startpos": [0.0, 0.0, 0.0],
        "ScanStage:axis_length": [0.55, 0.55, 4.0],
        "ScanStage:axis_step_size": [0.05, 0.05, 1.0],
        "ScanTTL:Nx": 10,
        "ScanTTL:Ny": 10,
    }

    resolved = resolve_acquisition_layout(attrs, shape=(400, 100, 100), detector="Cam")

    assert "AMBIGUOUS_LEGACY_TRAILING_AXIS" not in _codes(resolved)
    assert resolved.confidence == "high"


def test_snouty_time_is_a_loop_only_for_a_combined_array():
    attrs = {
        "MS-RESOLFT_Scan:cycleSteps": 3,
        "MS-RESOLFT_Scan:roSteps": 5,
        "recording:num_timepoints": 2,
    }

    combined = resolve_acquisition_layout(attrs, shape=(30, 2, 3), detector="Cam")
    partitioned = resolve_acquisition_layout(attrs, shape=(15, 2, 3), detector="Cam")

    assert [loop.kind for loop in combined.layout.event_loops] == ["time", "cycle", "plane"]
    assert [loop.kind for loop in partitioned.layout.event_loops] == ["cycle", "plane"]
    assert partitioned.layout.partitions[0].kind == "time"
    assert partitioned.layout.partitions[0].index == 0


def test_container_time_axis_requires_explicit_axis_metadata():
    generic = resolve_acquisition_layout(
        {},
        shape=(6, 2, 3),
        detector="Cam",
        axis_labels=("T", "Y", "X"),
        axis_metadata_explicit=False,
    )
    ome = resolve_acquisition_layout(
        {},
        shape=(6, 2, 3),
        detector="Cam",
        axis_labels=("T", "Y", "X"),
        axis_metadata_explicit=True,
    )

    assert generic.layout.event_loops[0].kind == "repeat"
    assert generic.layout.storage_axes[0] == "frame"
    assert generic.confidence == "low"
    assert ome.layout.event_loops[0].kind == "time"
    assert ome.source == "ome-ngff"


def test_matching_sidecar_reopens_and_fingerprint_mismatch_is_ignored(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"original source")
    before = source.read_bytes()
    override = _recorded_layout(provenance="user-override")

    sidecar = persist_layout_override(
        source,
        override,
        detector="Cam",
        dataset_path="Cam/data",
    )

    assert sidecar.is_file()
    assert source.read_bytes() == before
    reopened = resolve_acquisition_layout(
        {},
        shape=(6, 2, 3),
        detector="Cam",
        source_path=source,
        dataset_path="Cam/data",
    )
    assert reopened.layout == override
    assert reopened.source == "user-override-sidecar"

    source.write_bytes(b"changed source")
    mismatched = resolve_acquisition_layout(
        {},
        shape=(6, 2, 3),
        detector="Cam",
        source_path=source,
        dataset_path="Cam/data",
    )
    assert mismatched.source == "generic-fallback"
    assert "OVERRIDE_FINGERPRINT_MISMATCH" in _codes(mismatched)


def test_tiling_manifest_is_authoritative():
    index = SimpleNamespace(
        tiles=(object(), object(), object()),
        alignment_detector="AlignCam",
    )

    resolved = adapt_tiling_manifest(index)

    assert resolved.layout.detector == "AlignCam"
    assert resolved.layout.event_loops[0].kind == "tile"
    assert resolved.layout.event_loops[0].count == 3
    assert resolved.layout.partitions[0].storage == "one-file-per-item"


def test_data_obj_exposes_resolution_without_materializing_pixels(tmp_path):
    path = tmp_path / "layout.h5"
    layout = _recorded_layout()
    with h5py.File(path, "w") as handle:
        group = handle.create_group("Cam")
        data = group.create_dataset("data", shape=(6, 2, 3), dtype=np.uint16)
        data.attrs["detector_name"] = "Cam"
        data.attrs["writing"] = False
        data.attrs["AcquisitionLayout:schema"] = ACQUISITION_LAYOUT_SCHEMA
        data.attrs["AcquisitionLayout:json"] = encode_acquisition_layout(layout)

    data_obj = DataObj(path.name, "Cam", path=path)
    resolved = data_obj.acquisition_layout

    assert resolved.layout == layout
    assert resolved.source == "explicit-metadata"
    assert not data_obj.dataMaterialized
    assert data_obj.recording_lifecycle.writer_state == "finalized"
    data_obj.checkAndUnloadData()


def test_data_obj_sidecar_can_recover_invalid_file_metadata(tmp_path):
    path = tmp_path / "invalid.h5"
    with h5py.File(path, "w") as handle:
        group = handle.create_group("Cam")
        data = group.create_dataset("data", shape=(6, 2, 3), dtype=np.uint16)
        data.attrs["detector_name"] = "Cam"
        data.attrs["writing"] = False
        data.attrs["AcquisitionLayout:schema"] = ACQUISITION_LAYOUT_SCHEMA
        data.attrs["AcquisitionLayout:json"] = "{not json"

    persist_layout_override(
        path,
        _recorded_layout(provenance="user-override"),
        detector="Cam",
        dataset_path="Cam/data",
    )
    data_obj = DataObj(path.name, "Cam", path=path)

    resolved = data_obj.acquisition_layout

    assert resolved.source == "user-override-sidecar"
    assert "USER_OVERRIDE_RECOVERS_INVALID_LAYOUT" in _codes(resolved)
    data_obj.checkAndUnloadData()


class _StrictReconstructor(Reconstructor):
    name = "strict test"
    id = "strict-test"
    acquisition_requirements = AcquisitionRequirements(
        payload_kinds=frozenset({PAYLOAD_DETECTOR_FRAME_STREAM}),
        required_loop_kinds=frozenset({"time"}),
    )

    def make_param_widget(self, parent):
        return None

    def make_metadata_dialog(self, parent):
        return None

    def process(self, data_obj, params, context=None):
        return None


class _LegacyReconstructor(_StrictReconstructor):
    acquisition_requirements = None


def test_preflight_is_opt_in_and_structured_issues_project_to_warning():
    generic = resolve_acquisition_layout({}, shape=(6, 2, 3), detector="Cam")
    strict_data = SimpleNamespace(sourceKind="image", acquisition_layout=generic)

    inspection = _StrictReconstructor().inspect_source(strict_data)

    assert isinstance(inspection, SourceInspection)
    assert inspection.warning
    assert "AMBIGUOUS_ACQUISITION_LAYOUT" in _codes(inspection)
    with pytest.raises(AcquisitionPreflightError):
        _StrictReconstructor().validate_source(strict_data)

    inaccessible = SimpleNamespace(sourceKind="image")
    assert _LegacyReconstructor().inspect_source(inaccessible) is None
    assert _LegacyReconstructor().validate_source(inaccessible) is None


def test_preflight_reports_missing_calibration_and_extra_loops():
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="Cam",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(
            AcquisitionLoop("time", "time", 3),
            AcquisitionLoop("condition", "condition", 2),
        ),
    )
    resolved = resolve_acquisition_layout(
        {
            "AcquisitionLayout:schema": ACQUISITION_LAYOUT_SCHEMA,
            "AcquisitionLayout:json": encode_acquisition_layout(layout),
        },
        shape=(6, 2, 3),
        detector="Cam",
    )
    requirements = AcquisitionRequirements(
        payload_kinds=frozenset({PAYLOAD_DETECTOR_FRAME_STREAM}),
        required_loop_kinds=frozenset({"time"}),
        requires_calibrated_loops=frozenset({"time"}),
    )
    reconstructor = _StrictReconstructor()
    reconstructor.acquisition_requirements = requirements
    inspection = reconstructor.inspect_source(
        SimpleNamespace(sourceKind="image", acquisition_layout=resolved)
    )

    assert "UNSUPPORTED_EXTRA_ACQUISITION_LOOP" in _codes(inspection)
    assert "UNCALIBRATED_ACQUISITION_LOOP" in _codes(inspection)


def test_strict_preflight_rejects_stopped_early_layout():
    resolved = resolve_acquisition_layout(
        {
            "AcquisitionLayout:schema": ACQUISITION_LAYOUT_SCHEMA,
            "AcquisitionLayout:json": encode_acquisition_layout(_recorded_layout()),
            "recording:completion_outcome": "stopped_early",
        },
        shape=(4, 2, 3),
        detector="Cam",
    )

    inspection = _StrictReconstructor().inspect_source(
        SimpleNamespace(sourceKind="image", acquisition_layout=resolved)
    )

    assert "FRAME_COUNT_MISMATCH" in _codes(inspection)
    assert "INCOMPLETE_ACQUISITION_LAYOUT" in _codes(inspection)
    assert sum(issue.code == "FRAME_COUNT_MISMATCH" for issue in resolved.issues) == 1


def test_strict_preflight_rejects_payload_without_registered_validator():
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind="event-stream",
        detector="TCSPC",
        storage_axes=("event",),
        event_loops=(),
    )
    resolved = resolve_acquisition_layout(
        {
            "AcquisitionLayout:schema": ACQUISITION_LAYOUT_SCHEMA,
            "AcquisitionLayout:json": encode_acquisition_layout(layout),
        },
        shape=(10,),
        detector="TCSPC",
    )
    requirements = AcquisitionRequirements(payload_kinds=frozenset({"event-stream"}))

    issues = {
        issue.code for issue in preflight_acquisition_layout(resolved, requirements)
    }

    assert "UNVALIDATED_ACQUISITION_PAYLOAD" in issues


def test_snouty_adapter_puts_each_pitch_on_the_same_loop_as_the_producer():
    """cycleStepSizeUm belongs to the cycle loop, roStepSizeUm to the plane loop."""
    from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
        build_triggerscope_resolft_layouts,
    )

    params = {
        "timeLapsePoints": 1, "cycleSteps": 3, "roSteps": 5,
        "cycleStepSizeUm": 0.21, "roStepSizeUm": 2.0,
    }
    produced = build_triggerscope_resolft_layouts(
        ("Cam",), scan_parameters=params,
        scan_source="TriggerScopeScanController", pulse_counts={"Cam": 1},
    )["Cam"]
    adapted = resolve_acquisition_layout(
        {f"MS-RESOLFT_Scan:{key}": value for key, value in params.items()},
        shape=(15, 2, 3), detector="Cam",
    )
    assert adapted.source == "snouty-legacy"

    def pitches(layout):
        return {loop.kind: loop.step for loop in layout.event_loops if loop.kind != "time"}

    assert pitches(produced) == pitches(adapted.layout) == {"cycle": 0.21, "plane": 2.0}
