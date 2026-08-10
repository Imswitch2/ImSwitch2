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

    def __init__(self, data, name="loaded", path=None):
        self.name = name
        self.data = data
        self.dataPath = path
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

def test_reopening_the_same_file_yields_the_same_identity(tmp_path):
    """Identity comes from the source, so an ROI set saved against a file
    still lines up when that file is reopened."""
    from imswitch.improcess.reconstructors.view_only.reconstructor import (
        ViewOnlyReconstructor,
    )

    path = tmp_path / "stack.tif"
    path.write_bytes(b"not really a tiff, but a stable source")
    data = np.arange(64, dtype=float).reshape(8, 8)
    reconstructor = ViewOnlyReconstructor()

    first = reconstructor.process(_DataObj(data.copy(), path=str(path)), {})
    second = reconstructor.process(_DataObj(data.copy(), path=str(path)), {})

    assert first.identity_kind == "derived"
    assert first.dataset_uid == second.dataset_uid
    assert first.coordinate_space_uid == second.coordinate_space_uid


def test_data_from_different_files_never_shares_identity(tmp_path):
    from imswitch.improcess.reconstructors.view_only.reconstructor import (
        ViewOnlyReconstructor,
    )

    first_path = tmp_path / "a.tif"
    second_path = tmp_path / "b.tif"
    first_path.write_bytes(b"a")
    second_path.write_bytes(b"bb")
    reconstructor = ViewOnlyReconstructor()

    first = reconstructor.process(_DataObj(np.zeros((8, 8)), path=str(first_path)), {})
    second = reconstructor.process(_DataObj(np.zeros((8, 8)), path=str(second_path)), {})

    assert first.dataset_uid != second.dataset_uid


def test_pathless_data_gets_minted_not_inferred_identity():
    """Identical pixels are not evidence of being the same dataset.

    Two all-zero arrays would collide under any content fingerprint, so with
    no source to identify, the ids are minted and the two come out unrelated.
    """
    from imswitch.improcess.reconstructors.view_only.reconstructor import (
        ViewOnlyReconstructor,
    )

    reconstructor = ViewOnlyReconstructor()
    first = reconstructor.process(_DataObj(np.zeros((8, 8))), {})
    second = reconstructor.process(_DataObj(np.zeros((8, 8))), {})

    assert first.dataset_uid != second.dataset_uid
    assert first.coordinate_space_uid != second.coordinate_space_uid


# --------------------------------------------------------------------------
# provenance breadth (review round 6, P1)
# --------------------------------------------------------------------------

def test_processors_inherit_provenance_without_opting_in():
    """Declaring preserves_grid is all a processor has to do."""
    from imswitch.improcess.processors.base import attach_provenance

    class _SameGrid:
        preserves_grid = True

    class _Undeclared:
        pass

    source = _result()
    kept = attach_provenance([_result(name="a")], source, _SameGrid())[0]
    fresh = attach_provenance([_result(name="b")], source, _Undeclared())[0]

    assert kept.coordinate_space_uid == source.coordinate_space_uid
    assert kept.lineage == (source.result_uid,)
    # Undeclared must not claim a shared grid — the safe default.
    assert fresh.coordinate_space_uid != source.coordinate_space_uid
    assert fresh.lineage == (source.result_uid,)


def test_a_display_layer_component_inherits_its_parents_identity():
    """A component is a view of the parent, so an ROI drawn on the displayed
    result must not be judged unrelated to the component under it."""
    from imswitch.improcess.model.result import (
        DisplayLayerProcessingResult,
        DisplayLayerSpec,
    )

    source = _result(shape=(8, 8), labels=("Y", "X"))
    spec = DisplayLayerSpec(
        name="channel-0", data=np.ones((8, 8)), axis_labels=["Y", "X"]
    )

    component = DisplayLayerProcessingResult.from_spec(source, spec)

    assert component.coordinate_space_uid == source.coordinate_space_uid
    assert component.dataset_uid == source.dataset_uid
    assert component.lineage == (source.result_uid,)


def test_a_display_layer_can_declare_its_own_grid():
    from imswitch.improcess.model.result import (
        DisplayLayerProcessingResult,
        DisplayLayerSpec,
    )

    source = _result(shape=(8, 8), labels=("Y", "X"))
    spec = DisplayLayerSpec(
        name="overlay",
        data=np.ones((4, 4)),
        axis_labels=["Y", "X"],
        coordinate_space_uid="its-own-grid",
    )

    component = DisplayLayerProcessingResult.from_spec(source, spec)

    assert component.coordinate_space_uid == "its-own-grid"


def test_projecting_a_displayed_axis_does_not_claim_the_same_grid():
    """"Auto" on 2D data collapses X, which is not a shared grid."""
    from imswitch.improcess.processors.projection.processor import ProjectionProcessor

    source = _result(shape=(8, 8), labels=("Y", "X"))

    out = ProjectionProcessor().apply(source, {"axis": "Auto", "mode": "max"})

    assert out.coordinate_space_uid != source.coordinate_space_uid


def test_projecting_a_stack_axis_keeps_the_grid():
    from imswitch.improcess.processors.projection.processor import ProjectionProcessor

    source = _result(shape=(4, 8, 8), labels=("Z", "Y", "X"))

    out = ProjectionProcessor().apply(source, {"axis": "Z", "mode": "max"})

    assert out.coordinate_space_uid == source.coordinate_space_uid
