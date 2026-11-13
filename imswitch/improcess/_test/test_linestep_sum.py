"""Line-step sum: an Advanced Scan's line steps summed by condition.

The motivating case is two-colour STED with ten line steps: 488 + STED on
steps 1, 3, 5, 7, 9 and 561 + STED on steps 2, 4, 6, 8, 10 must come out as
two images, each the sum of its five planes. The end-to-end tests write the
recording with ImSwitch's own HDF5 storer, so the metadata arrives the way a
real scan stores it (JSON-encoded per-device maps under metadata/ScanTTL).
"""

from __future__ import annotations

import json
import os

import h5py
import numpy as np
import pytest
import tifffile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.imcommon.model.SharedAttributes import JSON_ATTR_PREFIX  # noqa: E402
from imswitch.imcommon.model.acquisition_layout import (  # noqa: E402
    ACQUISITION_LAYOUT_SCHEMA,
    encode_acquisition_layout,
)
from imswitch.imcontrol.controller.controllers._acquisition_layout_source import (  # noqa: E402
    build_advanced_scan_layouts,
)
from imswitch.imcontrol.model.managers.RecordingManager import HDF5Storer  # noqa: E402
from imswitch.improcess.model import DataObj  # noqa: E402
from imswitch.improcess.reconstructors.linestep_sum import (  # noqa: E402
    MATCH_ONOFF,
    MATCH_SETTINGS,
    LineStepSumError,
    LineStepSumReconstructor,
    group_line_steps,
    sum_dtype,
    sum_line_steps,
)

EXC_A, EXC_B, STED = "488 (EXC)", "561 (EXC)", "775 (STED)"


def _two_colour_sted(n_steps=10, **extra):
    """488 + STED on odd steps (counted from 1), 561 + STED on even ones."""
    attrs = {
        "ScanTTL:n_linesteps": n_steps,
        "ScanTTL:linestep_enable": {
            EXC_A: [step % 2 == 0 for step in range(n_steps)],
            EXC_B: [step % 2 == 1 for step in range(n_steps)],
            STED: [True] * n_steps,
        },
        "ScanTTL:linestep_power_percent": {
            EXC_A: [20.0] * n_steps, EXC_B: [30.0] * n_steps, STED: [80.0] * n_steps,
        },
        "ScanTTL:linestep_power_enabled": {EXC_A: True, EXC_B: True, STED: True},
        "ScanTTL:pulse_starts_s": {name: [[]] * n_steps for name in (EXC_A, EXC_B, STED)},
        "ScanTTL:pulse_ends_s": {name: [[]] * n_steps for name in (EXC_A, EXC_B, STED)},
        "ScanTTL:advanced_mode": False,
    }
    attrs.update(extra)
    return attrs


# ------------------------------------------------------------------- grouping
def test_two_colour_sted_gives_two_groups_of_five():
    plan = group_line_steps(_two_colour_sted(), 10)

    assert [group.label for group in plan.groups] == [
        f"{EXC_A} + {STED}", f"{EXC_B} + {STED}",
    ]
    assert [group.steps for group in plan.groups] == [(0, 2, 4, 6, 8), (1, 3, 5, 7, 9)]
    assert plan.groups[0].step_numbers == "1, 3, 5, 7, 9"
    assert plan.groups[1].devices == (EXC_B, STED)
    assert plan.groups[0].settings == f"{EXC_A} 20%, {STED} 80%"
    assert plan.skipped_steps == ()


def test_the_metadata_may_arrive_json_encoded_as_hdf5_stores_it():
    encoded = {
        key: JSON_ATTR_PREFIX + json.dumps(value) if isinstance(value, dict) else value
        for key, value in _two_colour_sted().items()
    }
    plan = group_line_steps(encoded, 10)
    assert [group.steps for group in plan.groups] == [(0, 2, 4, 6, 8), (1, 3, 5, 7, 9)]


def test_a_different_power_is_a_different_condition_unless_only_on_off_counts():
    attrs = _two_colour_sted(4)
    attrs["ScanTTL:linestep_enable"] = {EXC_A: [True] * 4, STED: [True] * 4}
    attrs["ScanTTL:linestep_power_percent"] = {EXC_A: [10.0, 50.0, 10.0, 50.0], STED: [80.0] * 4}

    by_settings = group_line_steps(attrs, 4, match=MATCH_SETTINGS)
    assert [group.steps for group in by_settings.groups] == [(0, 2), (1, 3)]
    assert [group.label for group in by_settings.groups] == [
        f"{EXC_A} 10% + {STED} 80%", f"{EXC_A} 50% + {STED} 80%",
    ]

    by_switching = group_line_steps(attrs, 4, match=MATCH_ONOFF)
    assert [group.steps for group in by_switching.groups] == [(0, 1, 2, 3)]


def test_power_of_a_device_without_power_modulation_does_not_split():
    attrs = _two_colour_sted(4)
    attrs["ScanTTL:linestep_power_percent"][STED] = [80.0, 10.0, 80.0, 10.0]
    attrs["ScanTTL:linestep_power_enabled"][STED] = False
    plan = group_line_steps(attrs, 4)
    assert [group.steps for group in plan.groups] == [(0, 2), (1, 3)]


def test_pulse_timing_splits_only_in_advanced_mode():
    attrs = _two_colour_sted(4)
    attrs["ScanTTL:linestep_enable"] = {EXC_A: [True] * 4}
    attrs["ScanTTL:pulse_starts_s"] = {EXC_A: [[0.0], [1e-6], [0.0], [1e-6]]}
    attrs["ScanTTL:pulse_ends_s"] = {EXC_A: [[1e-6], [2e-6], [1e-6], [2e-6]]}

    assert len(group_line_steps(attrs, 4).groups) == 1

    attrs["ScanTTL:advanced_mode"] = True
    plan = group_line_steps(attrs, 4)
    assert [group.steps for group in plan.groups] == [(0, 2), (1, 3)]
    # Same devices at the same power: told apart by their steps.
    assert [group.label for group in plan.groups] == [
        f"{EXC_A} 20% (steps 1, 3)", f"{EXC_A} 20% (steps 2, 4)",
    ]
    assert "custom pulse timing" in plan.groups[0].settings


def test_a_step_with_nothing_on_is_its_own_condition():
    attrs = _two_colour_sted(3)
    attrs["ScanTTL:linestep_enable"] = {EXC_A: [True, False, True]}
    plan = group_line_steps(attrs, 3)
    assert [(group.label, group.steps) for group in plan.groups] == [
        (EXC_A, (0, 2)), ("No TTL device on", (1,)),
    ]


def test_steps_the_detector_was_gated_off_are_left_out():
    attrs = _two_colour_sted(4)
    attrs["ScanTTL:linestep_enable"]["APD"] = [True, True, False, True]
    plan = group_line_steps(attrs, 4, detector="APD")

    assert plan.gate == "APD"
    assert plan.skipped_steps == (2,)
    assert [group.steps for group in plan.groups] == [(0,), (1, 3)]
    # The gate is not a condition of the image.
    assert all("APD" not in group.label for group in plan.groups)


@pytest.mark.parametrize(
    "attrs, n_steps, message",
    [
        (_two_colour_sted(1), 1, "single line step"),
        ({"ScanTTL:n_linesteps": 4}, 4, "which devices were on"),
        (_two_colour_sted(10), 8, "says 10 line steps"),
        (
            {"ScanTTL:linestep_enable": {EXC_A: [True, False, True]}},
            4,
            "3 entries, but the scan has 4 line steps",
        ),
    ],
    ids=["one-step", "no-masks", "count-mismatch", "short-mask"],
)
def test_metadata_that_cannot_be_grouped_is_refused_with_the_reason(attrs, n_steps, message):
    with pytest.raises(LineStepSumError, match=message):
        group_line_steps(attrs, n_steps)


# -------------------------------------------------------------------- summing
@pytest.mark.parametrize(
    "dtype, n_terms, expected",
    [
        (np.uint16, 5, np.uint32),
        (np.uint8, 2, np.uint32),
        (np.int16, 5, np.int32),
        (np.uint32, 5, np.uint64),
        (np.uint64, 5, np.float64),
        (np.float32, 5, np.float32),
        (np.float16, 5, np.float32),
        (np.float64, 5, np.float64),
        (np.bool_, 5, np.uint32),
    ],
)
def test_the_sum_type_cannot_overflow(dtype, n_terms, expected):
    assert sum_dtype(dtype, n_terms) == np.dtype(expected)


def test_full_scale_uint16_planes_sum_without_wrapping():
    data = np.full((1, 4, 2, 3), 65535, dtype=np.uint16)
    plan = group_line_steps(_two_colour_sted(4), 4)
    summed = sum_line_steps(data, 1, plan.groups)
    assert summed.shape == (1, 2, 2, 3)
    assert summed.dtype == np.uint32
    assert np.all(summed == 2 * 65535)


# ---------------------------------------------------------------- end to end
class _Detector:
    dtype = np.dtype(np.uint16)
    pixelSizeUm = [1.0, 0.05, 0.05]


class _Detectors:
    def __getitem__(self, name):
        return _Detector()


def _scan_info(nx, ny, n_steps, nz=None):
    dims, axes, sizes = [nx, ny], ["x", "y"], [0.05, 0.05]
    if nz is not None:
        dims, axes, sizes = dims + [nz], axes + ["z"], sizes + [0.2]
    return {"img_dims": dims, "img_axes_phys": axes, "pixel_sizes": sizes,
            "n_linesteps": n_steps}


def _write_recording(tmp_path, data, attrs, *, nz=None, recorded_layout=True, detector="APD"):
    """Store ``data`` (frame, line step, [Z,] Y, X) as a scan-driven detector does."""
    n_steps, ny, nx = data.shape[1], data.shape[-2], data.shape[-1]
    attrs = dict(attrs)
    attrs.update({
        "ScanTTL:Nx": nx,
        "ScanTTL:Ny": ny,
        "ScanStage:target_device": ["X", "Y", "Z"],
        "ScanStage:scan_dim_target_device": ["X", "Y", "Z" if nz else "None"],
        "ScanStage:axis_length": [nx * 0.05, ny * 0.05, (nz or 1) * 0.2],
        "ScanStage:axis_step_size": [0.05, 0.05, 0.2],
    })
    if recorded_layout:
        layout = build_advanced_scan_layouts(
            _scan_info(nx, ny, n_steps, nz), (detector,),
            scan_source="ScanControllerAdvanced", detector_masks={},
            pulse_counts_by_condition={}, scan_driven_detectors=(detector,),
        )[detector]
        attrs["AcquisitionLayout:schema"] = ACQUISITION_LAYOUT_SCHEMA
        attrs["AcquisitionLayout:json"] = encode_acquisition_layout(layout)
    storer = HDF5Storer(str(tmp_path / "rec"), _Detectors())
    storer.snap({detector: data}, {detector: attrs})
    return DataObj("rec", detector, path=str(tmp_path / f"rec_{detector}.h5"))


def _planes(n_steps, ny=6, nx=5, nz=None):
    """Every line step a distinct, recognisable plane: value = 100 * step + pixel."""
    spatial = (nz, ny, nx) if nz else (ny, nx)
    pixel = np.arange(int(np.prod(spatial)), dtype=np.uint16).reshape(spatial)
    planes = [100 * step + pixel for step in range(n_steps)]
    return np.stack(planes)[np.newaxis].astype(np.uint16)


@pytest.mark.parametrize("recorded_layout", [True, False], ids=["recorded", "legacy"])
def test_two_colour_sted_recording_sums_into_two_images(tmp_path, recorded_layout):
    data = _planes(10)
    data_obj = _write_recording(tmp_path, data, _two_colour_sted(), recorded_layout=recorded_layout)

    result = LineStepSumReconstructor().process(data_obj, {})

    assert result.axis_labels == ["C", "Y", "X"]
    assert result.data.shape == (2, 6, 5)
    assert result.data.dtype == np.uint32
    np.testing.assert_array_equal(result.data[0], data[0, 0::2].sum(axis=0))
    np.testing.assert_array_equal(result.data[1], data[0, 1::2].sum(axis=0))
    assert result.axis_scales == pytest.approx([1.0, 0.05, 0.05])
    assert result.scale_unit == "um"
    assert result.kind == type(LineStepSumReconstructor()).output_kind

    layers = result.display_layers()
    assert [layer.name for layer in layers] == [f"{EXC_A} + {STED}", f"{EXC_B} + {STED}"]
    assert [layer.colormap for layer in layers] == ["green", "magenta"]
    assert all(layer.axis_labels == ["Y", "X"] for layer in layers)
    np.testing.assert_array_equal(layers[1].data, result.data[1])
    # A processor can take one colour as its input, offered by name.
    assert [choice.label for choice in result.processor_input_choices()] == [
        "Whole result", f"{EXC_A} + {STED}", f"{EXC_B} + {STED}",
    ]

    assert result.table_records() == [
        {"recording": "rec", "image": f"{EXC_A} + {STED}", "line steps": "1, 3, 5, 7, 9",
         "steps summed": 5, "settings": f"{EXC_A} 20%, {STED} 80%"},
        {"recording": "rec", "image": f"{EXC_B} + {STED}", "line steps": "2, 4, 6, 8, 10",
         "steps summed": 5, "settings": f"{EXC_B} 30%, {STED} 80%"},
    ]


def test_a_z_stack_keeps_its_z_axis(tmp_path):
    data = _planes(4, ny=3, nx=4, nz=2)
    data_obj = _write_recording(tmp_path, data, _two_colour_sted(4), nz=2)

    result = LineStepSumReconstructor().process(data_obj, {})

    assert result.axis_labels == ["C", "Z", "Y", "X"]
    assert result.data.shape == (2, 2, 3, 4)
    np.testing.assert_array_equal(result.data[1], data[0, 1] + data[0, 3])


def test_saving_names_each_channel_after_its_condition(tmp_path):
    data_obj = _write_recording(tmp_path, _planes(4), _two_colour_sted(4))
    result = LineStepSumReconstructor().process(data_obj, {})

    receipt = result.save(tmp_path / "sums.tif", "tiff")
    with tifffile.TiffFile(receipt.primary) as tif:
        stored = tif.asarray()
        description = tif.ome_metadata or ""
    np.testing.assert_array_equal(stored, result.data)
    assert f"{EXC_A} + {STED}" in description
    assert f"{EXC_B} + {STED}" in description

    receipt = result.save(tmp_path / "sums.h5", "hdf5")
    with h5py.File(receipt.primary, "r") as handle:
        found = []

        def collect(_name, obj):
            if "channel_names" in obj.attrs:
                found.append(json.loads(obj.attrs["channel_names"]))

        collect("/", handle)
        handle.visititems(collect)
    assert found and found[0] == [f"{EXC_A} + {STED}", f"{EXC_B} + {STED}"]


def test_the_source_inspection_says_how_the_steps_group(tmp_path):
    data_obj = _write_recording(tmp_path, _planes(10), _two_colour_sted())
    inspection = LineStepSumReconstructor().inspect_source(data_obj)

    assert inspection.warning is None
    assert inspection.metadata["line_steps"] == 10
    assert inspection.metadata["line_step_images"] == 2
    assert inspection.metadata["line_step_groups"] == (
        f"{EXC_A} + {STED}: steps 1, 3, 5, 7, 9; {EXC_B} + {STED}: steps 2, 4, 6, 8, 10"
    )


def test_a_camera_that_recorded_steps_as_frames_is_refused_with_the_reason(tmp_path):
    layout = build_advanced_scan_layouts(
        _scan_info(3, 2, 2), ("CAM",), scan_source="ScanControllerAdvanced",
        detector_masks={"CAM": [True, True]}, pulse_counts_by_condition={"CAM": [1, 1]},
    )["CAM"]
    attrs = _two_colour_sted(2)
    attrs["AcquisitionLayout:schema"] = ACQUISITION_LAYOUT_SCHEMA
    attrs["AcquisitionLayout:json"] = encode_acquisition_layout(layout)
    storer = HDF5Storer(str(tmp_path / "rec"), _Detectors())
    storer.snap({"CAM": np.zeros((12, 4, 4), np.uint16)}, {"CAM": attrs})
    data_obj = DataObj("rec", "CAM", path=str(tmp_path / "rec_CAM.h5"))

    reconstructor = LineStepSumReconstructor()
    with pytest.raises(LineStepSumError, match="separate frames"):
        reconstructor.process(data_obj, {})
    assert "separate frames" in reconstructor.inspect_source(data_obj).warning
