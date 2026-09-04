from dataclasses import replace
from types import SimpleNamespace

import pytest

from imswitch.imcommon.model import recorded_frame_coordinates
from imswitch.imcommon.model.acquisition_layout import (
    PAYLOAD_ASSEMBLED_IMAGE,
    RecordedEventSpan,
)
from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (
    build_advanced_scan_layouts,
    build_point_scan_layouts,
    build_triggerscope_raster_layouts,
    build_triggerscope_resolft_layouts,
    planned_frame_count,
    validate_detector_edge_counts,
    with_time_partition,
)
from imswitch.imcontrol.model.SetupInfo import SetupInfo
from imswitch.imcontrol.model.signaldesigners.BetaScanDesigner import (
    BetaScanDesigner,
)
from imswitch.imcontrol.model.signaldesigners.AdvancedScanTTLCycleDesigner import (
    AdvancedScanTTLCycleDesigner,
)
from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import (
    GalvoScanDesigner,
)


def _scan_info(x=3, y=2, z=None, *, conditions=1):
    dims = [x, y]
    axes = ["x", "y"]
    steps = [0.1, 0.2]
    if z is not None:
        dims.append(z)
        axes.append("z")
        steps.append(0.5)
    return {
        "img_dims": dims,
        "img_axes_phys": axes,
        "pixel_sizes": steps,
        "n_linesteps": conditions,
    }


def _coordinates(layout):
    count = planned_frame_count(layout)
    assert count is not None
    return [recorded_frame_coordinates(layout, index) for index in range(count)]


def _beta_scan_info():
    setup = SimpleNamespace(
        scan=SimpleNamespace(sampleRate=1000),
        positioners={
            axis: SimpleNamespace(
                forScanning=True,
                managerProperties={"conversionFactor": 1},
            )
            for axis in ("X", "Y", "Z")
        },
    )
    parameters = {
        "target_device": ["X", "Y", "Z"],
        "axis_length": [3, 2, 2],
        "axis_step_size": [1, 1, 1],
        "axis_startpos": [[0], [0], [0]],
        "axis_centerpos": [0, 0, 0],
        "return_time": 0.001,
        "sequence_time": 0.002,
        "n_linesteps": 2,
    }
    return BetaScanDesigner().make_signal(parameters, setup)[2]


def _galvo_setup():
    return SetupInfo.from_json(
        """
        {"positioners": {
          "X": {"analogChannel":0,"managerName":"NidaqPositionerManager",
                "managerProperties":{"conversionFactor":1,"minVolt":-10,"maxVolt":10,"vel_max":0.5,"acc_max":0.05},
                "axes":["X"],"forScanning":true,"forPositioning":true},
          "Y": {"analogChannel":1,"managerName":"NidaqPositionerManager",
                "managerProperties":{"conversionFactor":1,"minVolt":-10,"maxVolt":10,"vel_max":0.5,"acc_max":0.05},
                "axes":["Y"],"forScanning":true,"forPositioning":true},
          "Z": {"analogChannel":2,"managerName":"NidaqPositionerManager",
                "managerProperties":{"conversionFactor":1,"minVolt":-10,"maxVolt":10,"vel_max":0.5,"acc_max":0.05},
                "axes":["Z"],"forScanning":true,"forPositioning":true}},
         "scan": {"scanDesigner":"GalvoScanDesigner","scanDesignerParams":{},
                  "TTLCycleDesigner":"AdvancedScanTTLCycleDesigner",
                  "TTLCycleDesignerParams":{},"sampleRate":100000}}
        """,
        infer_missing=True,
    )


def _galvo_scan_info():
    parameters = {
        "target_device": ["X", "Y", "Z"],
        "axis_length": [3, 2, 2],
        "axis_step_size": [1, 1, 1],
        "axis_centerpos": [0, 0, 0],
        "axis_startpos": [[0], [0], [0]],
        "sequence_time": 0.001,
        "phase_delay": 0,
        "d3step_delay": 100,
        "n_linesteps": 2,
    }
    return GalvoScanDesigner().make_signal(parameters, _galvo_setup())[2]


def test_advanced_18_by_18_layout_keeps_conditions_inside_each_row():
    layouts = build_advanced_scan_layouts(
        _scan_info(18, 18, conditions=2),
        ("Both", "B"),
        scan_source="ScanControllerAdvanced",
        detector_masks={"Both": [True, True], "B": [False, True]},
        pulse_counts_by_condition={"Both": [1, 1], "B": [1, 1]},
    )

    both = layouts["Both"]
    assert [loop.kind for loop in both.event_loops] == [
        "scan_y", "condition", "scan_x"
    ]
    assert planned_frame_count(both) == 648
    assert recorded_frame_coordinates(both, 0) == {
        "scan_y": 0, "condition": 0, "scan_x": 0
    }
    assert recorded_frame_coordinates(both, 17)["condition"] == 0
    assert recorded_frame_coordinates(both, 18) == {
        "scan_y": 0, "condition": 1, "scan_x": 0
    }
    assert recorded_frame_coordinates(both, 36) == {
        "scan_y": 1, "condition": 0, "scan_x": 0
    }

    b_only = layouts["B"]
    assert b_only.recorded_event_spans == (
        RecordedEventSpan(
            start=18,
            count=18,
            period=36,
            repeats=18,
        ),
    )
    assert planned_frame_count(b_only) == 324
    assert {point["condition"] for point in _coordinates(b_only)} == {1}


@pytest.mark.parametrize("scan_info_factory", [_beta_scan_info, _galvo_scan_info])
def test_advanced_beta_and_galvo_contracts_establish_3_by_2_by_2_order(
    scan_info_factory,
):
    layout = build_advanced_scan_layouts(
        scan_info_factory(),
        ("Camera",),
        scan_source="ScanControllerAdvanced",
        detector_masks={"Camera": [True, True]},
        pulse_counts_by_condition={"Camera": [1, 1]},
    )["Camera"]

    assert [loop.kind for loop in layout.event_loops] == [
        "scan_z", "scan_y", "condition", "scan_x"
    ]
    assert [loop.count for loop in layout.event_loops] == [2, 2, 2, 3]
    assert all(rule.order == "forward" for rule in layout.traversal)
    assert planned_frame_count(layout) == 24


def test_advanced_expanded_line_masks_preserve_sparse_coordinates():
    layout = build_advanced_scan_layouts(
        _scan_info(3, 2, conditions=2),
        ("Camera",),
        scan_source="ScanControllerAdvanced",
        detector_masks={"Camera": [True, False, False, True]},
        pulse_counts_by_condition={"Camera": [1, 1]},
    )["Camera"]

    coordinates = _coordinates(layout)
    assert len(coordinates) == 6
    assert {
        (point["scan_y"], point["condition"])
        for point in coordinates
    } == {(0, 0), (1, 1)}


def test_advanced_multiple_pulses_use_an_inner_repeat_loop():
    layout = build_advanced_scan_layouts(
        _scan_info(3, 1, conditions=2),
        ("Camera",),
        scan_source="ScanControllerAdvanced",
        detector_masks={"Camera": [True, True]},
        pulse_counts_by_condition={"Camera": [1, 2]},
    )["Camera"]

    assert [loop.kind for loop in layout.event_loops] == [
        "scan_y", "condition", "scan_x", "repeat"
    ]
    assert planned_frame_count(layout) == 9
    assert {
        point["repeat"]
        for point in _coordinates(layout)
        if point["condition"] == 0
    } == {0}
    assert {
        point["repeat"]
        for point in _coordinates(layout)
        if point["condition"] == 1
    } == {0, 1}


def test_final_detector_edges_must_match_the_layout_frame_selection():
    layout = build_advanced_scan_layouts(
        _scan_info(3, 1, conditions=1),
        ("Camera",),
        scan_source="ScanControllerAdvanced",
        detector_masks={"Camera": [True]},
        pulse_counts_by_condition={"Camera": [1]},
    )["Camera"]

    validate_detector_edge_counts(
        {"Camera": layout},
        {"Camera": [True, False, True, False, True]},
    )
    with pytest.raises(ValueError, match="final TTL signal has 1 rising edge"):
        validate_detector_edge_counts(
            {"Camera": layout},
            {"Camera": [True, True, True]},
        )


def test_beta_final_ttl_edges_match_the_3d_advanced_layout():
    setup = SimpleNamespace(
        scan=SimpleNamespace(sampleRate=1000),
        positioners={
            axis: SimpleNamespace(
                forScanning=True,
                managerProperties={"conversionFactor": 1},
            )
            for axis in ("X", "Y", "Z")
        },
    )
    scan_info = _beta_scan_info()
    parameters = {
        "target_device": ["Camera"],
        "n_linesteps": 2,
        "linestep_enable": {"Camera": [True, True]},
        "pulse_starts_s": {"Camera": [[0], [0]]},
        "pulse_ends_s": {"Camera": [[0.001], [0.001]]},
        "sequence_time": 0.002,
        "advanced_mode": True,
    }
    signals, _ = AdvancedScanTTLCycleDesigner().make_signal(
        parameters, setup, scan_info
    )
    layout = build_advanced_scan_layouts(
        scan_info,
        ("Camera",),
        scan_source="ScanControllerAdvanced",
        detector_masks=parameters["linestep_enable"],
        pulse_counts_by_condition={"Camera": [1, 1]},
    )["Camera"]

    validate_detector_edge_counts({"Camera": layout}, signals)
    assert planned_frame_count(layout) == 24


def test_point_and_raster_builders_publish_actual_3d_and_x_fast_order():
    point = build_point_scan_layouts(
        _scan_info(3, 2, 2),
        ("Camera",),
        scan_source="ScanControllerPointScan",
        pulse_counts={"Camera": 2},
        directions={"scan_x": -1, "scan_y": 1, "scan_z": 1},
    )["Camera"]
    assert [loop.kind for loop in point.event_loops] == [
        "scan_z", "scan_y", "scan_x", "repeat"
    ]
    # A negative stage direction is a physical sign on the loop, never a
    # reverse traversal: the designer stepped X monotonically.
    assert point.event_loops[2].direction == -1
    assert [rule.order for rule in point.traversal] == ["forward"] * 4
    assert planned_frame_count(point) == 24

    raster = build_triggerscope_raster_layouts(
        ("Camera",),
        dimensions=(3, 2),
        step_sizes=(0.1, 0.2),
        scan_source="TriggerScopeRasterController",
    )["Camera"]
    assert [loop.kind for loop in raster.event_loops] == ["scan_y", "scan_x"]
    assert recorded_frame_coordinates(raster, 2)["scan_x"] == 2
    assert recorded_frame_coordinates(raster, 3) == {"scan_y": 1, "scan_x": 0}


def test_scan_driven_and_resolft_layouts_map_loops_to_assembled_axes():
    advanced = build_advanced_scan_layouts(
        _scan_info(3, 2, 2, conditions=2),
        ("PMT",),
        scan_source="ScanControllerAdvanced",
        detector_masks={},
        pulse_counts_by_condition={},
        scan_driven_detectors=("PMT",),
    )["PMT"]
    assert advanced.payload_kind == PAYLOAD_ASSEMBLED_IMAGE
    assert advanced.storage_axes == (
        "frame", "condition", "scan_z", "scan_y", "scan_x"
    )
    assert advanced.recorded_event_spans is None

    resolft = build_triggerscope_resolft_layouts(
        ("PMT",),
        scan_parameters={
            "timeLapsePoints": 2,
            "cycleSteps": 3,
            "roSteps": 4,
        },
        scan_source="TriggerScopeScanController",
        scan_driven_detectors=("PMT",),
    )["PMT"]
    assert [loop.kind for loop in resolft.event_loops] == [
        "time", "cycle", "plane"
    ]
    assert resolft.storage_axes == ("frame", "time", "cycle", "plane")


@pytest.mark.parametrize("single_file", [False, True])
def test_lapse_partitions_restart_with_only_the_index_varying(single_file):
    base = build_point_scan_layouts(
        _scan_info(),
        ("Camera",),
        scan_source="ScanControllerPointScan",
    )["Camera"]
    first = with_time_partition(
        base, index=0, planned_count=2, single_file=single_file
    )
    second = with_time_partition(
        base, index=1, planned_count=2, single_file=single_file
    )

    assert replace(second, partitions=first.partitions) == first
    assert first.partitions[0].index == 0
    assert second.partitions[0].index == 1
    assert recorded_frame_coordinates(first, 0) == recorded_frame_coordinates(
        second, 0
    )


# ----------------------------------------------------------------------
# Audit condition 2: the condition loop follows ScanInfo dimension 0, not the
# kind string 'scan_x' that dimension usually carries.
# ----------------------------------------------------------------------


def test_advanced_condition_loop_follows_dimension_0_under_kind_overrides():
    """A Y-first scan: dimension 0 is physically Y, so its loop is scan_y.

    The designers repeat every dimension-0 line per condition regardless of
    which stage drives it. Keyed on the kind string, the builder used to put
    the condition loop outside whichever loop happened to be called scan_x --
    here dimension 1 -- and every count still added up.
    """
    layouts = build_advanced_scan_layouts(
        _scan_info(3, 2, conditions=2),
        ("Both", "B"),
        scan_source="ScanControllerAdvanced",
        detector_masks={"Both": [True, True], "B": [False, True, True, False]},
        pulse_counts_by_condition={"Both": [1, 1], "B": [1, 1]},
        kind_overrides={"scan_x": "scan_y", "scan_y": "scan_x"},
    )

    both = layouts["Both"]
    assert [loop.kind for loop in both.event_loops] == ["scan_x", "condition", "scan_y"]
    assert [loop.count for loop in both.event_loops] == [2, 2, 3]
    assert planned_frame_count(both) == 12
    # Frames 0..2 are condition 0 of the first line, 3..5 condition 1 of the
    # SAME line, 6..8 condition 0 of the second line.
    assert recorded_frame_coordinates(both, 0) == {"scan_x": 0, "condition": 0, "scan_y": 0}
    assert recorded_frame_coordinates(both, 3) == {"scan_x": 0, "condition": 1, "scan_y": 0}
    assert recorded_frame_coordinates(both, 6) == {"scan_x": 1, "condition": 0, "scan_y": 0}

    # A per-expanded-line mask is indexed by dimension 1 (scan_x here), not by
    # whichever loop is called scan_y.
    b_only = layouts["B"]
    selected = _coordinates(b_only)
    assert [(p["scan_x"], p["condition"]) for p in selected[::3]] == [(0, 1), (1, 0)]
    assert planned_frame_count(b_only) == 6


def test_advanced_z_only_line_steps_put_the_condition_outside_the_only_axis():
    """A Z-piezo-only scan: dimension 0 is scan_z after the physical override."""
    scan_info = {
        "img_dims": [4],
        "img_axes_phys": ["x"],  # the designer's label for dimension 0
        "pixel_sizes": [0.5],
        "n_linesteps": 2,
    }
    layouts = build_advanced_scan_layouts(
        scan_info,
        ("Camera", "APD"),
        scan_source="ScanControllerAdvanced",
        detector_masks={"Camera": [True, True]},
        pulse_counts_by_condition={"Camera": [1, 1]},
        scan_driven_detectors=("APD",),
        kind_overrides={"scan_x": "scan_z"},
    )

    camera = layouts["Camera"]
    assert [loop.kind for loop in camera.event_loops] == ["condition", "scan_z"]
    assert planned_frame_count(camera) == 8
    assert recorded_frame_coordinates(camera, 3) == {"condition": 0, "scan_z": 3}
    assert recorded_frame_coordinates(camera, 4) == {"condition": 1, "scan_z": 0}

    apd = layouts["APD"]
    assert [loop.kind for loop in apd.event_loops] == ["condition", "scan_z"]
    assert list(apd.storage_axes) == ["frame", "condition", "scan_z"]


def _y_first_parameters(designer):
    """A scan whose fast axis is the Y stage, with X held per line pair."""
    common = {
        "target_device": ["Y", "X", "Z"],
        "axis_length": [3, 2, 2],
        "axis_step_size": [1, 1, 1],
        "axis_startpos": [[0], [0], [0]],
        "axis_centerpos": [0, 0, 0],
        "n_linesteps": 2,
    }
    if designer == "beta":
        return dict(common, return_time=0.001, sequence_time=0.002)
    return dict(common, sequence_time=0.001, phase_delay=0, d3step_delay=100)


@pytest.mark.parametrize("designer", ["beta", "galvo"])
def test_y_first_line_steps_follow_the_designers_own_signal(designer):
    """Y-first, two line steps, checked against the waveform, not the counts.

    The designer is asked for a scan whose dimension 0 is the Y stage. The
    analog values at every rising edge of the camera TTL say where each frame
    was taken; the layout built with the physical override (dimension 0 ->
    ``scan_y``, dimension 1 -> ``scan_x``) must put each frame at that position
    and condition. Totals cannot catch a misplaced condition loop; the
    sequence can: X must hold for two consecutive lines (both conditions of
    one line) and only then step.

    The Z-only variant of this check needs main's single-axis Galvo fix
    (9e64c5f7), which is not on this branch yet; the builder side of Z-only is
    covered synthetically above.
    """
    import numpy as np

    if designer == "beta":
        setup = SimpleNamespace(
            scan=SimpleNamespace(sampleRate=1000),
            positioners={
                axis: SimpleNamespace(
                    forScanning=True, managerProperties={"conversionFactor": 1}
                )
                for axis in ("X", "Y", "Z")
            },
        )
        sig_dict, _, scan_info = BetaScanDesigner().make_signal(
            _y_first_parameters(designer), setup
        )
        sequence_time, pulse_end = 0.002, 0.001
    else:
        setup = _galvo_setup()
        sig_dict, _, scan_info = GalvoScanDesigner().make_signal(
            _y_first_parameters(designer), setup
        )
        sequence_time, pulse_end = 0.001, 0.0005
    assert list(scan_info["img_dims"]) == [3, 2, 2]
    assert list(scan_info["img_axes_phys"]) == ["x", "y", "z"]  # designer labels

    ttl_parameters = {
        "target_device": ["Camera"],
        "n_linesteps": 2,
        "linestep_enable": {"Camera": [True, True]},
        "pulse_starts_s": {"Camera": [[0], [0]]},
        "pulse_ends_s": {"Camera": [[pulse_end], [pulse_end]]},
        "sequence_time": sequence_time,
        "advanced_mode": True,
    }
    signals, _ = AdvancedScanTTLCycleDesigner().make_signal(
        ttl_parameters, setup, scan_info
    )
    layout = build_advanced_scan_layouts(
        scan_info,
        ("Camera",),
        scan_source="ScanControllerAdvanced",
        detector_masks=ttl_parameters["linestep_enable"],
        pulse_counts_by_condition={"Camera": [1, 1]},
        kind_overrides={"scan_x": "scan_y", "scan_y": "scan_x"},
    )["Camera"]

    assert [loop.kind for loop in layout.event_loops] == [
        "scan_z", "scan_x", "condition", "scan_y"
    ]
    validate_detector_edge_counts({"Camera": layout}, signals)

    camera = np.asarray(signals["Camera"], dtype=bool)
    edges = np.flatnonzero(camera[1:] & ~camera[:-1]) + 1
    if camera[0]:
        edges = np.concatenate([[0], edges])
    assert edges.size == 24
    y_at_edges = np.asarray(sig_dict["Y"], dtype=float)[edges]
    x_at_edges = np.asarray(sig_dict["X"], dtype=float)[edges]

    coordinates = [recorded_frame_coordinates(layout, index) for index in range(24)]
    for line in range(8):
        frames = slice(line * 3, line * 3 + 3)
        # Along one line the Y stage climbs, and the layout counts scan_y 0..2.
        assert np.all(np.diff(y_at_edges[frames]) > 0), (line, y_at_edges[frames])
        assert [c["scan_y"] for c in coordinates[frames]] == [0, 1, 2]
        assert {c["condition"] for c in coordinates[frames]} == {line % 2}
    # X is one value per line (the Galvo designer's step lands just after the
    # first edge of a new line while the stage settles, so take the median
    # rather than the first sample) -- and the SAME value for the second line
    # of each pair, i.e. both conditions of one X position, stepping only
    # afterwards: that is the condition loop inside scan_x.
    x_per_line = np.array([np.median(x_at_edges[line * 3:line * 3 + 3]) for line in range(8)])
    assert np.allclose(x_per_line[0::2], x_per_line[1::2], atol=0.05), x_per_line
    assert [c["scan_x"] for c in coordinates[::3]] == [0, 0, 1, 1, 0, 0, 1, 1]
    assert [c["scan_z"] for c in coordinates[::3]] == [0, 0, 0, 0, 1, 1, 1, 1]
