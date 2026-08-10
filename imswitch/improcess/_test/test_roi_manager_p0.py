"""P-0 defect fixes for the ROI manager (see docs/design/plans/improcess-roi-manager-2-0.md).

Covers D-01 (inert Visible checkbox), D-03 (one bad ROI blanking the batch),
D-05 (rows keyed by model index) and D-12/D-13 (lossy mutations, unsafe name
identity). Model-level tests are Qt-free; the widget tests use a fake viewer.
"""

import dataclasses

import numpy as np
import pytest

from imswitch.imcommon.algorithms.roi import ROIRecord
from imswitch.improcess.analysis.roi_manager import ROIManagerModel, replaced


# --------------------------------------------------------------------------
# D-12 — every mutation must preserve every field
# --------------------------------------------------------------------------

def _fully_populated_roi() -> ROIRecord:
    """An ROI with every field set to a non-default value.

    Built from the dataclass fields rather than a hand-written literal so a
    field added later is covered automatically instead of silently escaping
    the preservation test.
    """
    return ROIRecord(
        name="cell",
        roi_type="mask",
        bounds=(1, 4, 2, 5),
        visible=True,
        source="segmentation",
        pixels=((1, 2), (2, 3)),
    )


def _collide(model):
    """Add a second ROI under a taken name; returns (stored, expected source).

    The uniquifying branch of ``add`` must preserve the *incoming* record's
    fields, not the ones already in the model — so this case carries its own
    reference record.
    """
    incoming = ROIRecord(
        name="cell",
        roi_type="polygon",
        bounds=(0, 3, 0, 3),
        visible=False,
        source="manual",
        pixels=((0, 0),),
    )
    return model.add(incoming), incoming


@pytest.mark.parametrize(
    "mutate, changed",
    [
        # Renaming touches nothing that affects measurement, so revision holds.
        (lambda m: (m.rename("cell", "nucleus"), m.get("nucleus")), {"name"}),
        # Visibility does affect what is measured, so revision advances.
        (lambda m: (m.set_visible("cell", False), None), {"visible", "revision"}),
        # A copy is a different ROI, however identical its geometry.
        (lambda m: (m.duplicate("cell"), None), {"name", "uid"}),
        (_collide, {"name", "uid"}),
    ],
    ids=["rename", "set_visible", "duplicate", "add-collision"],
)
def test_every_mutation_preserves_all_record_fields(mutate, changed):
    """Mutations must copy the fields they are not changing.

    Rebuilding the record field by field passes today only because the list
    happens to be complete; this pins it so adding a field cannot silently
    drop it on rename/duplicate/visibility/collision paths.
    """
    model = ROIManagerModel([_fully_populated_roi()])
    stored = model.get("cell")

    result, expected = mutate(model)
    expected = expected if expected is not None else stored

    for field in dataclasses.fields(ROIRecord):
        if field.name in changed:
            continue
        assert getattr(result, field.name) == getattr(expected, field.name), (
            f"{field.name} was not preserved across the mutation"
        )


def test_identity_survives_a_rename_and_a_copy_gets_its_own():
    model = ROIManagerModel([_fully_populated_roi()])
    original_uid = model.get("cell").uid

    renamed = model.rename("cell", "nucleus")
    copy = model.duplicate("nucleus")

    assert original_uid, "the model must assign an identity on ingest"
    assert renamed.uid == original_uid, "renaming must not change identity"
    assert copy.uid != original_uid, "a duplicate is a different ROI"


def test_revision_advances_only_for_measurement_affecting_changes():
    """Renaming 200 ROIs must not invalidate 200 cached measurements."""
    model = ROIManagerModel([_fully_populated_roi()])
    start = model.get("cell").revision

    renamed = model.rename("cell", "nucleus")
    assert renamed.revision == start

    hidden = model.set_visible("nucleus", False)
    assert hidden.revision == start + 1


def test_replaced_is_the_single_mutation_helper():
    original = _fully_populated_roi()

    updated = replaced(original, name="other")

    assert updated.name == "other"
    assert updated.pixels == original.pixels
    assert original.name == "cell"  # frozen: the input is untouched


# --------------------------------------------------------------------------
# D-13 — names are the row key, so they must be unique on every path in
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "build",
    [
        lambda payload: ROIManagerModel.from_dicts(payload),
        lambda payload: ROIManagerModel([ROIRecord.from_dict(item) for item in payload]),
    ],
    ids=["from_dicts", "constructor"],
)
def test_duplicate_names_are_rejected_on_every_ingestion_path(build):
    payload = [
        ROIRecord("cell", "rectangle", (0, 2, 0, 2)).to_dict(),
        ROIRecord("cell", "rectangle", (2, 4, 2, 4)).to_dict(),
    ]

    model = build(payload)

    names = [roi.name for roi in model.rois]
    assert len(names) == 2
    assert len(set(names)) == 2, f"duplicate names survived ingestion: {names}"


def test_removing_one_of_two_similarly_named_rois_keeps_the_other():
    model = ROIManagerModel.from_dicts(
        [
            ROIRecord("cell", "rectangle", (0, 2, 0, 2)).to_dict(),
            ROIRecord("cell", "rectangle", (2, 4, 2, 4)).to_dict(),
        ]
    )

    model.remove("cell")

    assert [roi.name for roi in model.rois] == ["cell_1"]


# --------------------------------------------------------------------------
# D-01 — visibility actually controls measurement
# --------------------------------------------------------------------------

def test_hidden_rois_are_not_measured_but_are_still_listed():
    image = np.ones((6, 6), dtype=float)
    model = ROIManagerModel(
        [
            ROIRecord("shown", "rectangle", (0, 2, 0, 2), visible=True),
            ROIRecord("hidden", "rectangle", (2, 4, 2, 4), visible=False),
        ]
    )

    records = model.compute_stats(image)

    assert [record.roi.name for record in records] == ["shown", "hidden"]
    assert records[0].measured is True
    assert records[1].measured is False
    assert np.isnan(records[1].stats.mean)
    assert records[1].note == "hidden"


def test_measure_hidden_opt_in_still_works():
    image = np.ones((6, 6), dtype=float)
    model = ROIManagerModel([ROIRecord("hidden", "rectangle", (0, 2, 0, 2), visible=False)])

    records = model.compute_stats(image, measure_hidden=True)

    assert records[0].measured is True
    assert records[0].stats.mean == pytest.approx(1.0)


# --------------------------------------------------------------------------
# D-03 — one bad ROI must not blank the batch
# --------------------------------------------------------------------------

def test_out_of_bounds_roi_reports_error_without_blanking_the_batch():
    image = np.ones((8, 8), dtype=float)
    model = ROIManagerModel(
        [
            ROIRecord("good", "rectangle", (0, 4, 0, 4)),
            ROIRecord("outside", "rectangle", (100, 120, 100, 120)),
            ROIRecord("also_good", "rectangle", (4, 8, 4, 8)),
        ]
    )

    records = model.compute_stats(image)

    assert len(records) == 3
    assert records[0].stats.mean == pytest.approx(1.0)
    assert records[2].stats.mean == pytest.approx(1.0)
    assert records[1].error, "the out-of-bounds ROI should carry its own error"
    assert np.isnan(records[1].stats.mean)


def test_empty_mask_roi_yields_nan_measurements_and_a_note():
    image = np.ones((4, 4), dtype=float)
    model = ROIManagerModel(
        [
            ROIRecord("empty", "mask", (0, 2, 0, 2), pixels=()),
            ROIRecord("fine", "rectangle", (0, 2, 0, 2)),
        ]
    )

    records = model.compute_stats(image)

    assert records[0].error
    assert records[0].note == records[0].error
    assert np.isnan(records[0].stats.mean)
    assert records[1].stats.mean == pytest.approx(1.0)


def test_error_rows_survive_serialisation():
    image = np.ones((4, 4), dtype=float)
    model = ROIManagerModel([ROIRecord("outside", "rectangle", (50, 60, 50, 60))])

    row = model.compute_stats(image)[0].to_row()

    assert row["name"] == "outside"
    assert row["measured"] is False
    assert row["note"]
