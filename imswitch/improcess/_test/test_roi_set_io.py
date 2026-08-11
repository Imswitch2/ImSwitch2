"""P-6.2: the versioned envelope, its refusals, and semantic round-tripping.

Round-tripping is asserted *semantically* — the ROIs mean the same thing after
a save and load — rather than byte-identically. A byte comparison passes for a
writer that is consistently wrong, and fails for one that merely reorders a
key.
"""

import json

import numpy as np
import pytest

from imswitch.imcommon.algorithms.roi import ROIRecord
from imswitch.imcommon.algorithms.roi_geometry import roi_from_mask, roi_mask
from imswitch.imcommon.algorithms.roi_set import MeasurementConfig, ROISet
from imswitch.imcommon.algorithms.roi_set_io import (
    COORDINATE_CONVENTION,
    FORMAT,
    SCHEMA_VERSION,
    ROISetFormatError,
    dumps,
    loads,
    read_set,
    set_to_json,
    validate,
    write_set,
)
from imswitch.imcommon.algorithms.roi_style import ROIStyle
from imswitch.imcommon.algorithms.spatial_frame import AxisDescriptor, SpatialFrame

SHAPE = (32, 32)


def _frame(scale=0.5):
    return SpatialFrame(
        coordinate_space_uid="space-1",
        result_uid="result-1",
        dataset_uid="data-1",
        plane_axes=("Y", "X"),
        axes=(
            AxisDescriptor("Z", 8, 2.0, "um"),
            AxisDescriptor("Y", 32, scale, "um"),
            AxisDescriptor("X", 32, scale, "um"),
        ),
        shape=SHAPE,
        unit="um",
        affine=(scale, 0.0, 1.0, 0.0, scale, 2.0, 0.0, 0.0, 1.0),
        lineage=("result-0",),
    )


def _full_set():
    """A set using every part of the schema, so nothing round-trips by omission."""
    frame = _frame()
    mask = np.zeros((8, 8), dtype=bool)
    mask[2:6, 2:6] = True
    return ROISet(
        name="Everything",
        rois=(
            ROIRecord(
                "rect", "rectangle", (1, 5, 2, 6), uid="u1", revision=3,
                frame_uid=frame.frame_uid, position=(("Z", 2),),
                style=ROIStyle(stroke_color="#ff0000", stroke_width=2.0),
                group=7, properties=(("stain", "DAPI"),),
            ),
            ROIRecord(
                "poly", "polygon", (0, 10, 0, 10), uid="u2",
                vertices=((0.5, 0.5), (9.5, 0.5), (4.0, 9.5)),
                frame_uid=frame.frame_uid,
            ),
            roi_from_mask(
                mask, name="blob", offset=(10, 10), frame_uid=frame.frame_uid
            ),
        ),
        frames=(frame,),
        default_style=ROIStyle(stroke_color="#00ff00"),
        measurement_config=MeasurementConfig(
            selected=("mean", "area_px"), decimals=5, threshold=(1.0, 9.0),
            line_width=3, display_label=True,
        ),
        dataset_uid="data-1",
    )


# --------------------------------------------------------------------------
# the envelope
# --------------------------------------------------------------------------

def test_the_envelope_says_what_the_file_is_and_how_to_read_it():
    payload = set_to_json(ROISet())
    assert payload["format"] == FORMAT
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["coordinate_convention"] == COORDINATE_CONVENTION
    assert payload["created_with"].startswith("ImSwitch ")
    assert "roi_set" in payload


def test_a_file_from_a_newer_imswitch_is_refused_not_guessed():
    payload = set_to_json(ROISet())
    payload["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(ROISetFormatError, match="newer ImSwitch"):
        loads(json.dumps(payload))


def test_a_file_that_is_not_ours_is_refused_by_name():
    with pytest.raises(ROISetFormatError, match="not an ImSwitch ROI set"):
        loads(json.dumps({"format": "something-else", "schema_version": 1}))


def test_invalid_json_is_refused_with_the_parser_s_reason():
    with pytest.raises(ROISetFormatError, match="not valid JSON"):
        loads("{not json")


def test_a_nonsense_version_is_refused():
    payload = set_to_json(ROISet())
    payload["schema_version"] = "banana"
    with pytest.raises(ROISetFormatError, match="not a number"):
        loads(json.dumps(payload))


def test_a_file_with_no_set_in_it_is_refused():
    with pytest.raises(ROISetFormatError, match="no ROI set"):
        loads(json.dumps({"format": FORMAT, "schema_version": 1}))


# --------------------------------------------------------------------------
# semantic round-trip
# --------------------------------------------------------------------------

def test_every_field_survives_a_round_trip():
    original = _full_set()
    restored = loads(dumps(original))

    assert restored.name == original.name
    assert restored.dataset_uid == original.dataset_uid
    assert restored.default_style == original.default_style
    assert restored.measurement_config == original.measurement_config
    assert [roi.uid for roi in restored.rois] == [roi.uid for roi in original.rois]

    rect = restored.rois[0]
    assert rect.revision == 3
    assert rect.position == (("Z", 2),)
    assert rect.style == ROIStyle(stroke_color="#ff0000", stroke_width=2.0)
    assert rect.group == 7
    assert rect.properties == (("stain", "DAPI"),)


def test_geometry_means_the_same_thing_after_a_round_trip():
    """Semantic, not byte-identical: the pixels are what has to survive."""
    original = _full_set()
    restored = loads(dumps(original))

    for before, after in zip(original.rois, restored.rois):
        assert np.array_equal(roi_mask(before, SHAPE), roi_mask(after, SHAPE))


def test_a_frame_is_restored_with_its_calibration_and_its_identity():
    original = _full_set()
    restored = loads(dumps(original))

    frame = restored.frames[0]
    assert frame.frame_uid == original.frames[0].frame_uid
    assert frame.unit == "um"
    assert frame.axis("Z").scale == 2.0
    assert frame.affine == original.frames[0].affine
    assert frame.lineage == ("result-0",)


def test_the_frame_uid_is_derived_on_load_not_trusted_from_the_file():
    """A stored uid disagreeing with its content would be the wrong answer.

    It would also be the more trustworthy-looking one, which is why it is
    recomputed rather than read.
    """
    payload = set_to_json(_full_set())
    payload["roi_set"]["frames"][0]["frame_uid"] = "frame-tampered"
    payload["roi_set"]["rois"] = []       # so validation does not fire first

    restored = loads(json.dumps(payload))
    assert restored.frames[0].frame_uid != "frame-tampered"
    assert restored.frames[0].frame_uid == _full_set().frames[0].frame_uid


def test_a_10_export_without_any_20_fields_still_loads():
    """Tolerating omissions is the migration path."""
    legacy = {
        "format": FORMAT,
        "schema_version": 1,
        "roi_set": {
            "name": "old",
            "rois": [
                {"name": "a", "roi_type": "rectangle", "bounds": [0, 4, 0, 4]}
            ],
        },
    }
    restored = loads(json.dumps(legacy))
    assert restored.rois[0].name == "a"
    assert restored.rois[0].uid == ""
    assert restored.measurement_config == MeasurementConfig()


# --------------------------------------------------------------------------
# semantic validation
# --------------------------------------------------------------------------

def test_two_rois_sharing_an_identity_are_refused():
    """One of them would be unreachable through every uid-keyed path."""
    broken = ROISet(
        rois=(
            ROIRecord("a", "rectangle", (0, 4, 0, 4), uid="same"),
            ROIRecord("b", "rectangle", (5, 9, 5, 9), uid="same"),
        )
    )
    with pytest.raises(ROISetFormatError, match="unreachable"):
        validate(broken)


def test_an_roi_pointing_at_a_frame_the_file_lacks_is_refused():
    broken = ROISet(
        rois=(ROIRecord("a", "rectangle", (0, 4, 0, 4), frame_uid="frame-missing"),)
    )
    with pytest.raises(ROISetFormatError, match="does not contain"):
        validate(broken)


def test_a_valid_set_passes_validation_silently():
    validate(_full_set())


def test_an_unreadable_roi_names_which_one():
    payload = set_to_json(ROISet(rois=(ROIRecord("a", "rectangle", (0, 4, 0, 4)),)))
    payload["roi_set"]["rois"][0] = "not a record"
    with pytest.raises(ROISetFormatError, match="ROI 0"):
        loads(json.dumps(payload))


# --------------------------------------------------------------------------
# atomic writing
# --------------------------------------------------------------------------

def test_writing_and_reading_a_file_round_trips(tmp_path):
    target = tmp_path / "sets" / "one.json"
    original = _full_set()
    write_set(target, original)

    assert target.exists()
    assert read_set(target).name == original.name


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path, monkeypatch):
    """A truncated file where a valid one used to be is the failure to avoid."""
    target = tmp_path / "one.json"
    write_set(target, ROISet(name="good"))

    def explode(_roi_set, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(
        "imswitch.imcommon.algorithms.roi_set_io.dumps", explode
    )
    with pytest.raises(RuntimeError):
        write_set(target, ROISet(name="bad"))

    assert read_set(target).name == "good"
    # ...and nothing was left behind.
    assert [p.name for p in tmp_path.iterdir()] == ["one.json"]
