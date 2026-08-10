"""P-F.2/.4/.6: the four identities on results, and how they propagate.

The point of keeping them apart is that unrelated results must never look
interchangeable, and derived results must say what they came from.
"""

import numpy as np

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.processors.stack_subset.processor import (
    normalize_subset_ranges,
    subset_result,
)


def _result(name="src", shape=(4, 8, 8), labels=("Z", "Y", "X")):
    return ArrayProcessingResult(
        name=name,
        data=np.ones(shape, dtype=float),
        axis_labels=list(labels),
    )


class _DataObj:
    """Minimal stand-in for the DataObj contract ViewOnlyReconstructor reads."""

    def __init__(self, data, name="loaded"):
        self.name = name
        self.data = data
        self.dataLoaded = True
        self.data_handle = None
        self.axis_labels = None
        self.axis_scales = None
        self.scale_unit = "px"

    def checkAndLoadData(self):
        pass

    def checkAndUnloadData(self):
        pass


# --------------------------------------------------------------------------
# P-F.2 — the four identities exist and are independent of the display name
# --------------------------------------------------------------------------

def test_every_result_has_the_four_identities():
    result = _result()

    assert result.result_uid
    assert result.dataset_uid
    assert result.coordinate_space_uid
    assert result.lineage == ()
    assert result.identity_kind == "minted"


def test_two_results_are_never_accidentally_the_same():
    """Same name, same shape, same values — still different results."""
    first, second = _result(), _result()

    assert first.result_uid != second.result_uid
    assert first.coordinate_space_uid != second.coordinate_space_uid


def test_renaming_changes_no_identity():
    result = _result()
    before = (result.result_uid, result.dataset_uid, result.coordinate_space_uid)

    result.name = "something else"

    assert (result.result_uid, result.dataset_uid, result.coordinate_space_uid) == before


# --------------------------------------------------------------------------
# P-F.4 — derived results inherit the dataset and record their lineage
# --------------------------------------------------------------------------

def test_same_grid_derivation_keeps_the_coordinate_space():
    source = _result()
    derived = _result(name="derived").adopt_identity_from(source, same_grid=True)

    assert derived.coordinate_space_uid == source.coordinate_space_uid
    assert derived.dataset_uid == source.dataset_uid
    assert derived.lineage == (source.result_uid,)


def test_regridding_derivation_gets_its_own_coordinate_space():
    source = _result()
    derived = _result(name="derived").adopt_identity_from(source, same_grid=False)

    assert derived.coordinate_space_uid != source.coordinate_space_uid
    assert derived.dataset_uid == source.dataset_uid
    assert derived.lineage == (source.result_uid,)


def test_lineage_accumulates_across_generations():
    source = _result()
    first = _result(name="a").adopt_identity_from(source, same_grid=True)
    second = _result(name="b").adopt_identity_from(first, same_grid=True)

    assert second.lineage == (source.result_uid, first.result_uid)


def test_trimming_a_stack_axis_keeps_the_grid():
    """Dropping Z planes moves no pixel, so ROIs still line up."""
    source = _result(shape=(6, 8, 8))

    ranges = normalize_subset_ranges(
        {"Z": (1, 4)}, labels=source.axis_labels, shape=source.data.shape
    )
    out = subset_result(source, ranges)

    assert out.coordinate_space_uid == source.coordinate_space_uid
    assert out.lineage == (source.result_uid,)


def test_cropping_in_xy_changes_the_grid():
    """A spatial crop moves the origin, so the grid is a different one."""
    source = _result(shape=(6, 8, 8))

    ranges = normalize_subset_ranges(
        {"X": (2, 6)}, labels=source.axis_labels, shape=source.data.shape
    )
    out = subset_result(source, ranges)

    assert out.coordinate_space_uid != source.coordinate_space_uid
    assert out.dataset_uid == source.dataset_uid


# --------------------------------------------------------------------------
# P-F.6 — data loaded from disk gets an inferred, clearly-marked identity
# --------------------------------------------------------------------------

def test_loaded_data_gets_a_deterministic_derived_identity():
    from imswitch.improcess.reconstructors.view_only.reconstructor import (
        ViewOnlyReconstructor,
    )

    data = np.arange(64, dtype=float).reshape(8, 8)
    reconstructor = ViewOnlyReconstructor()

    first = reconstructor.process(_DataObj(data.copy()), {})
    second = reconstructor.process(_DataObj(data.copy()), {})

    assert first.identity_kind == "derived"
    # The same content must yield the same ids, so an ROI set saved against
    # this file still matches it in a later session.
    assert first.dataset_uid == second.dataset_uid
    assert first.coordinate_space_uid == second.coordinate_space_uid


def test_different_loaded_data_gets_different_identity():
    from imswitch.improcess.reconstructors.view_only.reconstructor import (
        ViewOnlyReconstructor,
    )

    reconstructor = ViewOnlyReconstructor()
    first = reconstructor.process(_DataObj(np.zeros((8, 8))), {})
    second = reconstructor.process(_DataObj(np.ones((8, 8))), {})

    assert first.dataset_uid != second.dataset_uid
