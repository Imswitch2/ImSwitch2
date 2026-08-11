"""P-R: running a processor over a region rather than a whole frame."""

import numpy as np
import pytest

from imswitch.imcommon.algorithms.roi import ROIRecord
from imswitch.imcommon.algorithms.roi_geometry import roi_from_mask, roi_from_points
from imswitch.improcess.analysis.roi_restriction import (
    ROI_MODES,
    ROI_PARAM,
    ROIRestriction,
    ROIRestrictionError,
    apply_provenance,
    restrict_array,
    restrict_result,
    union_bounds,
    union_mask,
)


def _rois():
    return (
        ROIRecord("a", "rectangle", (2, 6, 3, 7), uid="ua"),
        ROIRecord("b", "rectangle", (10, 14, 11, 15), uid="ub"),
    )


class _Result:
    """The parts of a ProcessingResult the restriction touches."""

    def __init__(self, data, name="src"):
        self.data = np.asarray(data)
        self.name = name
        self.axis_labels = ["Y", "X"] if self.data.ndim == 2 else ["Z", "Y", "X"]
        self.coordinate_space_uid = "space-1"
        self.roi_provenance = {}
        self.minted = 0

    def mint_coordinate_space(self):
        self.coordinate_space_uid = f"space-new-{self.minted}"
        self.minted += 1


# --------------------------------------------------------------------------
# the two modes
# --------------------------------------------------------------------------

def test_crop_narrows_to_the_union_of_the_regions():
    image = np.arange(256, dtype=float).reshape(16, 16)
    restriction = ROIRestriction(rois=_rois(), mode="crop")

    cropped, offset = restrict_array(image, ["Y", "X"], restriction)
    assert cropped.shape == (12, 12)      # rows 2..14, cols 3..15
    assert offset == (2, 3)
    assert cropped[0, 0] == image[2, 3]


def test_mask_keeps_the_frame_and_sets_the_rest_aside():
    image = np.ones((16, 16), dtype=float)
    restriction = ROIRestriction(rois=_rois(), mode="mask")

    masked, offset = restrict_array(image, ["Y", "X"], restriction)
    assert masked.shape == image.shape
    assert offset == (0, 0)
    assert masked[3, 4] == 1.0            # inside a region
    assert np.isnan(masked[0, 0])         # outside every region


def test_the_fill_is_nan_rather_than_zero():
    """Zero says "measured, and dark", which is a different and false claim."""
    assert np.isnan(ROIRestriction().fill)


def test_masking_an_integer_image_does_not_silently_cast_the_fill():
    image = np.ones((8, 8), dtype=np.uint16)
    masked, _offset = restrict_array(
        image, ["Y", "X"], ROIRestriction(rois=(ROIRecord("a", "rectangle", (0, 2, 0, 2)),), mode="mask")
    )
    assert masked.dtype.kind == "f"
    assert np.isnan(masked[5, 5])


def test_every_non_plane_axis_is_preserved():
    """A region on a Z-stack is the region on every slice, not one slice of it."""
    stack = np.zeros((5, 16, 16), dtype=float)
    cropped, _offset = restrict_array(
        stack, ["Z", "Y", "X"], ROIRestriction(rois=_rois(), mode="crop")
    )
    assert cropped.shape == (5, 12, 12)


def test_an_unknown_mode_is_refused_at_construction():
    with pytest.raises(ROIRestrictionError, match="unknown ROI mode"):
        ROIRestriction(mode="sideways")
    assert ROI_MODES == ("crop", "mask")


def test_regions_are_clipped_to_the_image_not_refused():
    """A region partly outside the image is still a region."""
    roi = ROIRecord("edge", "rectangle", (-5, 4, -5, 4))
    assert union_bounds([roi], (16, 16)) == (0, 4, 0, 4)


def test_regions_entirely_outside_the_image_are_refused():
    roi = ROIRecord("gone", "rectangle", (100, 120, 100, 120))
    with pytest.raises(ROIRestrictionError, match="entirely outside"):
        union_bounds([roi], (16, 16))


def test_the_mask_union_costs_the_regions_not_one_frame_per_roi():
    mask = np.zeros((8, 8), dtype=bool)
    mask[1:4, 1:4] = True
    rois = [
        roi_from_mask(mask, name="a", offset=(0, 0)),
        roi_from_mask(mask, name="b", offset=(10, 10)),
    ]
    union = union_mask(rois, (20, 20))
    assert union.sum() == 18
    assert union[2, 2] and union[12, 12]


def test_a_point_roi_is_skipped_rather_than_refusing_the_whole_mask():
    """A mixed set should still restrict by the regions that have an interior."""
    rois = [
        ROIRecord("area", "rectangle", (0, 4, 0, 4)),
        roi_from_points([[10.0, 10.0]], name="dot"),
    ]
    union = union_mask(rois, (16, 16))
    assert union.sum() == 16


def test_a_set_with_no_measurable_pixels_is_refused():
    with pytest.raises(ROIRestrictionError, match="no pixels"):
        union_mask([roi_from_points([[3.0, 3.0]], name="dot")], (16, 16))


# --------------------------------------------------------------------------
# restricting a result
# --------------------------------------------------------------------------

def test_restricting_leaves_the_source_untouched():
    """So a failed run leaves nothing behind, and one source can be
    restricted two ways in one session."""
    source = _Result(np.arange(256, dtype=float).reshape(16, 16))
    original = source.data.copy()

    narrowed, _applied = restrict_result(source, ROIRestriction(rois=_rois()))
    assert narrowed is not source
    assert np.array_equal(source.data, original)
    assert narrowed.data.shape == (12, 12)


def test_the_applied_restriction_carries_the_crop_offset():
    source = _Result(np.zeros((16, 16)))
    _narrowed, applied = restrict_result(source, ROIRestriction(rois=_rois()))
    assert applied.offset == (2, 3)


def test_an_inactive_restriction_returns_the_source_as_it_is():
    source = _Result(np.zeros((16, 16)))
    narrowed, applied = restrict_result(source, ROIRestriction())
    assert narrowed is source
    assert not applied.active


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------

def test_the_output_records_which_set_at_which_revision():
    restriction = ROIRestriction(
        rois=_rois(), mode="mask",
        set_uid="set-1", set_name="cells", set_revision=7,
    )
    output = _Result(np.zeros((4, 4)))
    apply_provenance([output], restriction)

    assert output.roi_provenance["roi_set_uid"] == "set-1"
    assert output.roi_provenance["roi_set_name"] == "cells"
    assert output.roi_provenance["roi_set_revision"] == 7
    assert output.roi_provenance["roi_mode"] == "mask"
    assert output.roi_provenance["roi_names"] == ["a", "b"]
    assert output.roi_provenance["roi_uids"] == ["ua", "ub"]


def test_a_cropped_output_gets_a_pixel_grid_of_its_own():
    """A crop moves every pixel index, so it cannot be on its source's grid."""
    output = _Result(np.zeros((4, 4)))
    before = output.coordinate_space_uid

    apply_provenance([output], ROIRestriction(rois=_rois(), mode="crop", offset=(2, 3)))
    assert output.coordinate_space_uid != before


def test_a_masked_output_keeps_the_grid_it_was_given():
    """Masking moves nothing, so an ROI still measures the same pixels."""
    output = _Result(np.zeros((16, 16)))
    before = output.coordinate_space_uid

    apply_provenance([output], ROIRestriction(rois=_rois(), mode="mask"))
    assert output.coordinate_space_uid == before


def test_an_unrestricted_run_records_nothing():
    """Absence means "the whole image", not "unknown"."""
    output = _Result(np.zeros((4, 4)))
    apply_provenance([output], ROIRestriction())
    assert output.roi_provenance == {}


# --------------------------------------------------------------------------
# the run path
# --------------------------------------------------------------------------

class _Processor:
    id = "fake"
    name = "Fake"
    accepts_roi = True
    preserves_grid = True

    def __init__(self):
        self.seen = []

    def apply(self, result, params):
        self.seen.append(np.asarray(result.data).shape)
        out = _Result(np.asarray(result.data) * 2, name="out")
        return out


def _normalized(output, source=None, processor=None):
    return (output,) if not isinstance(output, tuple) else output


def test_the_run_path_narrows_the_input_and_annotates_the_output(monkeypatch):
    import importlib

    # By module path: the package re-exports the *class* under the same name,
    # so a plain attribute lookup gets the class instead.
    module = importlib.import_module(
        "imswitch.improcess.controller.ResultProcessorController"
    )
    monkeypatch.setattr(module, "normalize_processor_output", _normalized)
    processor = _Processor()
    source = _Result(np.ones((16, 16)))
    restriction = ROIRestriction(rois=_rois(), mode="crop", set_uid="set-1")

    results = module._run_restricted(processor, source, {}, restriction)

    assert processor.seen == [(12, 12)]          # apply saw the region
    assert results[0].roi_provenance["roi_set_uid"] == "set-1"
    assert results[0].roi_provenance["roi_offset"] == [2, 3]


def test_a_processor_that_did_not_opt_in_is_never_handed_a_cropped_input():
    """The UI cannot make an ROI-unaware processor ROI-aware by guessing."""
    from imswitch.improcess.controller.ResultProcessorController import (
        _restriction_for,
    )

    class _Plain:
        accepts_roi = False

    restriction = ROIRestriction(rois=_rois())
    assert _restriction_for(_Plain(), {ROI_PARAM: restriction}) is None
    assert _restriction_for(_Processor(), {ROI_PARAM: restriction}) is restriction


def test_an_inactive_restriction_is_treated_as_none():
    from imswitch.improcess.controller.ResultProcessorController import (
        _restriction_for,
    )

    assert _restriction_for(_Processor(), {ROI_PARAM: ROIRestriction()}) is None
    assert _restriction_for(_Processor(), {}) is None


# --------------------------------------------------------------------------
# which processors opted in
# --------------------------------------------------------------------------

def test_the_local_processors_accept_a_region():
    import importlib

    for module_name, expected_first in (
        ("filters", "mask"),
        ("denoise", "mask"),
        ("background", "mask"),
        ("segmentation", "crop"),
        ("math_ops", "mask"),
    ):
        module = importlib.import_module(
            f"imswitch.improcess.processors.{module_name}.processor"
        )
        found = [
            obj for obj in vars(module).values()
            if isinstance(obj, type) and getattr(obj, "accepts_roi", False)
        ]
        assert found, module_name
        assert found[0].roi_modes[0] == expected_first, module_name


def test_psf_and_colocalization_read_rois_through_the_shared_rasteriser():
    """They are ROI-native already — per-ROI measurement, not input
    restriction — so a second path through the restriction would be a second
    thing to disagree with the first."""
    import ast
    import inspect

    from imswitch.improcess.analysis import colocalization, psf_resolution

    for module in (psf_resolution, colocalization):
        tree = ast.parse(inspect.getsource(module))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert "roi_mask_local" in imported, module.__name__


def test_a_drift_correction_does_not_offer_a_region():
    """Over a crop it is a different measurement, not a cheaper one."""
    from imswitch.improcess.processors.drift_correct import processor as module

    offered = [
        obj for obj in vars(module).values()
        if isinstance(obj, type) and getattr(obj, "accepts_roi", False)
    ]
    assert not offered
