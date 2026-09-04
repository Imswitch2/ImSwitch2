"""A file this branch cannot describe must still open.

The acquisition-layout work replaced inferred frame semantics with recorded
ones, and gave ImProcess a resolver that refuses rather than guesses. On files
recorded *before* the branch there is nothing recorded to read, so everything
runs through the legacy adapters -- and three ways of being wrong showed up
there, each of which made an ordinary old recording worse than it was before
the branch existed:

* An adapter claimed files from other modalities. Its signature was "has a
  sequence time, no line steps, no Nx", which is what every point-scan and
  MoNaLISA controller publishes for every recording on the rig, scan or not.
  A plain timelapse whose frame count happened to match the configured stage
  area was reported as a raster at *high* confidence, and a galvo point scan
  was attributed to TriggerScope.
* That adapter raised instead of declining, so a legacy file whose frame count
  does not match the configured area could not be resolved at all.
* Two positioners whose names guess the same axis produced duplicate loop ids
  and an invalid layout. That is not exotic: the repository's own example
  configurations have ``ND-PiezoZ`` beside ``PiezoZ`` and ``Mock Kinesis XY``
  beside ``Mock X``.

And in each case the exception escaped into the *display* path, so the user
opened a perfectly readable file and got no image. Naming axes is a decision
with a fallback; it must never cost the pixels.
"""

from __future__ import annotations

import h5py
import numpy as np
import pytest

from imswitch.improcess.model import DataObj
from imswitch.improcess.model.acquisition_layout_resolver import (
    resolve_acquisition_layout,
)
from imswitch.improcess.reconstructors.view_only.reconstructor import (
    ViewOnlyReconstructor,
)

FRAMES, HEIGHT, WIDTH = 6, 8, 8


def _attrs(**overrides):
    """What a pre-branch scan rig wrote onto every recording."""
    return {
        "ScanTTL:sequence_time": 0.001,
        "ScanStage:axis_length": [3.0, 2.0],
        "ScanStage:axis_step_size": [1.0, 1.0],
        "ScanStage:axis_startpos": [0.0, 0.0],
        "recording:detector_name": "Camera",
        **overrides,
    }


def _resolve(attrs, shape=(FRAMES, HEIGHT, WIDTH)):
    return resolve_acquisition_layout(attrs, shape=shape, detector="Camera")


def _legacy_file(tmp_path, attrs, shape=(FRAMES, HEIGHT, WIDTH)):
    path = tmp_path / "legacy_Camera.hdf5"
    with h5py.File(path, "w") as file:
        dataset = file.create_group("Camera").create_dataset(
            "data", data=np.zeros(shape, dtype=np.uint16)
        )
        dataset.attrs.update(attrs)
    return path


# ----------------------------------------------------------------------
# Only a file that says TriggerScope is read as TriggerScope
# ----------------------------------------------------------------------


def test_a_timelapse_on_a_scan_rig_is_not_reported_as_a_raster():
    """The configured area is what the rig *could* scan, not what this ran."""
    resolved = _resolve(_attrs(**{"recording:num_timepoints": FRAMES}))

    assert resolved.source == "generic-fallback"
    assert resolved.confidence == "low"
    assert [loop.kind for loop in resolved.layout.event_loops] == ["repeat"]


def test_a_point_scan_is_not_attributed_to_triggerscope():
    resolved = _resolve(_attrs())

    assert resolved.source == "scan-stage-legacy"
    assert resolved.layout.scan_source == "scan-stage-legacy"
    assert resolved.confidence == "medium"
    assert [loop.kind for loop in resolved.layout.event_loops] == [
        "scan_y", "scan_x"
    ]


def test_a_file_that_names_triggerscope_still_is_one():
    resolved = _resolve(
        _attrs(**{"recording:scan_source": "TriggerScopeRasterController"})
    )

    assert resolved.source == "triggerscope-raster-legacy"
    assert resolved.confidence == "high"


def test_an_assembled_raster_resolves_without_a_modality_claim():
    """A point detector's assembled image is a payload, not a firmware."""
    resolved = resolve_acquisition_layout(
        _attrs(**{"ScanStage:axis_length": [4.0, 3.0]}),
        shape=(3, 4),
        detector="APD",
    )

    assert resolved.layout.payload_kind == "assembled-image"
    assert resolved.layout.storage_axes == ("scan_y", "scan_x")
    assert resolved.source == "scan-stage-legacy-assembled"


# ----------------------------------------------------------------------
# Declining beats raising
# ----------------------------------------------------------------------


def test_a_frame_count_that_does_not_match_the_stage_area_declines():
    """A short or frame-dropping scan is not a broken file."""
    resolved = _resolve(_attrs(**{"ScanStage:axis_length": [4.0, 3.0]}))

    assert resolved.source == "generic-fallback"
    assert resolved.confidence == "low"


@pytest.mark.parametrize(
    "devices,lengths",
    [
        # example_sted.json: a Z piezo behind a named Z galvo.
        (["ND-GalvoX", "ND-GalvoY", "ND-PiezoZ", "PiezoZ"], [3.0, 2.0, 0.0, 0.0]),
        # example_no_hardware.json: an XY stage whose name contains 'X'.
        (["Mock X", "Mock Y", "Mock Kinesis XY"], [3.0, 2.0, 0.0]),
    ],
)
def test_a_spare_positioner_sharing_an_axis_name_is_left_out(devices, lengths):
    resolved = _resolve(
        _attrs(**{
            "ScanStage:target_device": devices,
            "ScanStage:axis_length": lengths,
            "ScanStage:axis_step_size": [1.0] * len(devices),
            "ScanStage:axis_startpos": [0.0] * len(devices),
        })
    )

    kinds = [loop.kind for loop in resolved.layout.event_loops]
    assert len(kinds) == len(set(kinds)), kinds
    assert {"scan_x", "scan_y"} <= set(kinds)
    assert "LEGACY_INACTIVE_AXIS_DROPPED" in {
        issue.code for issue in resolved.issues
    }


def test_two_axes_that_both_moved_and_share_a_name_are_not_guessed():
    """Dropping one would be a coin flip over which stage drove which axis."""
    from imswitch.improcess.model.acquisition_layout_resolver import (
        _scan_geometry_candidates,
        AcquisitionLayoutResolutionError,
    )

    with pytest.raises(AcquisitionLayoutResolutionError) as error:
        _scan_geometry_candidates(
            {
                "ScanStage:target_device": ["GalvoX", "PiezoX"],
                "ScanStage:axis_length": [3.0, 2.0],
                "ScanStage:axis_step_size": [1.0, 1.0],
            },
            observed_events=None,
            multiplier=1,
        )

    assert "AMBIGUOUS_LEGACY_AXIS_DEVICES" in {
        issue.code for issue in error.value.issues
    }


# ----------------------------------------------------------------------
# Naming axes must never cost the pixels
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "attrs",
    [
        _attrs(**{"ScanStage:axis_length": [4.0, 3.0]}),
        _attrs(**{
            "ScanStage:target_device": ["GalvoX", "PiezoX"],
            "ScanStage:axis_length": [3.0, 2.0],
            "ScanStage:axis_step_size": [1.0, 1.0],
            "ScanStage:axis_startpos": [0.0, 0.0],
        }),
    ],
    ids=["count-mismatch", "ambiguous-devices"],
)
def test_view_only_displays_a_file_whose_layout_cannot_be_resolved(tmp_path, attrs):
    path = _legacy_file(tmp_path, attrs)
    data_obj = DataObj(str(path), "Camera", path=str(path))

    result = ViewOnlyReconstructor().process(data_obj, {})

    assert result is not None
    assert result.data.shape == (FRAMES, HEIGHT, WIDTH)
    assert result.axis_labels == ["Frame", "Y", "X"]
