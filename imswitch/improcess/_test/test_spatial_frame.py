"""P-F.1: the spatial frame and the compatibility decision tree.

The decision tree's *order* is the contract (F-29): unrelated data must be
rejected on the coordinate-space check before any shape comparison, so two
unrelated images that happen to share a shape can never be called comparable.
"""

import pytest

from imswitch.imcommon.algorithms.spatial_frame import (
    AxisDescriptor,
    InMemoryTransformRegistry,
    SpatialFrame,
    TransformEdge,
    compatibility,
    content_digest_uid,
    derive_frame_uid,
    is_auto_measurable,
    mint_uid,
)


def _frame(
    *,
    space="space-a",
    result="result-a",
    dataset="data-a",
    plane=("Y", "X"),
    shape=(64, 64),
    affine=None,
    unit="px",
    axes=None,
    component=None,
    view_mode=None,
    identity_kind="minted",
):
    if axes is None:
        axes = (
            AxisDescriptor("Z", 10),
            AxisDescriptor("Y", shape[0]),
            AxisDescriptor("X", shape[1]),
        )
    kwargs = dict(
        coordinate_space_uid=space,
        result_uid=result,
        dataset_uid=dataset,
        plane_axes=plane,
        axes=axes,
        shape=shape,
        unit=unit,
        component=component,
        view_mode=view_mode,
        identity_kind=identity_kind,
    )
    if affine is not None:
        kwargs["affine"] = affine
    return SpatialFrame(**kwargs)


# --------------------------------------------------------------------------
# frame identity (A-27)
# --------------------------------------------------------------------------

def test_frame_uid_is_derived_not_minted():
    """Identical planes hash alike, so a frame in a saved ROI set still
    matches the same plane next session with nothing written to the image."""
    assert _frame().frame_uid == _frame().frame_uid
    assert _frame().frame_uid.startswith("frame-")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"space": "space-b"},
        {"plane": ("Z", "X")},
        {"shape": (64, 65)},
        {"unit": "um"},
        {"component": "channel-1"},
        {"view_mode": "XZ"},
        {"affine": (0.5, 0, 0, 0, 0.5, 0, 0, 0, 1)},
    ],
    ids=["space", "plane", "shape", "unit", "component", "view_mode", "affine"],
)
def test_frame_uid_changes_with_every_defining_property(kwargs):
    assert _frame(**kwargs).frame_uid != _frame().frame_uid


def test_frame_uid_ignores_provenance_only_fields():
    """result/dataset identity describe where a plane came from, not which
    plane it is; two views of one grid must not get different frame uids."""
    assert _frame(result="other", dataset="other").frame_uid == _frame().frame_uid


def test_minted_uids_are_unique_and_digest_uids_are_stable():
    assert mint_uid("result") != mint_uid("result")
    assert content_digest_uid("data", "abc", 3) == content_digest_uid("data", "abc", 3)
    assert content_digest_uid("data", "abc", 3) != content_digest_uid("data", "abd", 3)


# --------------------------------------------------------------------------
# the decision tree, in order (F-29)
# --------------------------------------------------------------------------

def test_unrelated_data_of_the_same_shape_is_incompatible():
    """The regression the ordering exists for: identical shape and scale on
    unrelated grids must not read as comparable."""
    verdict = compatibility(_frame(space="space-a"), _frame(space="space-b"))

    assert verdict == "incompatible"


def test_different_plane_axes_are_incompatible():
    """An XY region says nothing about an XZ slice."""
    source = _frame(plane=("Y", "X"))
    target = _frame(plane=("Z", "X"))

    assert compatibility(source, target) == "incompatible"


def test_plane_axis_check_precedes_everything_else():
    """Even on the same grid with the same shape, a different plane loses."""
    assert compatibility(_frame(plane=("Y", "X")), _frame(plane=("Z", "X"))) == "incompatible"


def test_position_outside_the_target_extent_is_incompatible():
    target = _frame(axes=(AxisDescriptor("Z", 5), AxisDescriptor("Y", 64), AxisDescriptor("X", 64)))

    assert compatibility(_frame(), target, positions=(("Z", 12),)) == "incompatible"
    assert compatibility(_frame(), target, positions=(("Z", 4),)) == "exact"


def test_position_on_a_missing_axis_is_incompatible():
    target = _frame(axes=(AxisDescriptor("Y", 64), AxisDescriptor("X", 64)))

    assert compatibility(_frame(), target, positions=(("T", 0),)) == "incompatible"


def test_same_frame_is_exact():
    assert compatibility(_frame(), _frame()) == "exact"


def test_same_grid_different_calibration_is_pixel_compatible():
    source = _frame(affine=(1, 0, 0, 0, 1, 0, 0, 0, 1))
    target = _frame(affine=(0.1, 0, 0, 0, 0.1, 0, 0, 0, 1))

    assert compatibility(source, target) == "pixel-compatible"


def test_same_grid_different_extent_is_clippable():
    assert compatibility(_frame(shape=(512, 512)), _frame(shape=(256, 256))) == "clippable"


def test_transform_edge_reports_registered_but_only_when_a_registry_is_given():
    source = _frame(space="space-a")
    target = _frame(space="space-b")
    registry = InMemoryTransformRegistry(
        [TransformEdge("space-a", "space-b", (1, 0, 0, 0, 1, 0, 0, 0, 1))]
    )

    assert compatibility(source, target, transforms=registry) == "registered"
    # Without the registry there is no way to know, so it stays incompatible.
    assert compatibility(source, target) == "incompatible"


def test_registered_is_never_auto_measurable():
    """Measuring through a transform is reprojection, so it is not automatic."""
    assert not is_auto_measurable("registered")
    assert not is_auto_measurable("clippable")
    assert not is_auto_measurable("incompatible")
    assert is_auto_measurable("exact")
    assert is_auto_measurable("pixel-compatible")


# --------------------------------------------------------------------------
# derived identity may never claim certainty (A-27)
# --------------------------------------------------------------------------

def test_derived_identity_never_reports_exact():
    source = _frame(identity_kind="derived")
    target = _frame(identity_kind="minted")

    assert compatibility(source, target) == "pixel-compatible"


def test_derived_identity_does_not_upgrade_a_weaker_verdict():
    source = _frame(shape=(512, 512), identity_kind="derived")
    target = _frame(shape=(256, 256))

    assert compatibility(source, target) == "clippable"


def test_derived_identity_still_rejects_unrelated_grids():
    source = _frame(space="space-a", identity_kind="derived")
    target = _frame(space="space-b", identity_kind="derived")

    assert compatibility(source, target) == "incompatible"


# --------------------------------------------------------------------------
# layering (A-26 / F-11)
# --------------------------------------------------------------------------

def test_spatial_frame_needs_no_app_packages():
    """It lives in imcommon, so it must not reach into the app packages.

    Checked on the imports rather than the source text: the docstring names
    napari precisely to say it is *not* needed.
    """
    import ast
    import inspect

    from imswitch.imcommon.algorithms import spatial_frame

    tree = ast.parse(inspect.getsource(spatial_frame))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    forbidden = ("imswitch.improcess", "imswitch.imcontrol", "napari", "qtpy")
    offenders = [name for name in imported if name.startswith(forbidden)]
    assert not offenders, offenders


def test_derive_frame_uid_is_callable_on_a_frame():
    frame = _frame()

    assert derive_frame_uid(frame) == frame.frame_uid


# --------------------------------------------------------------------------
# P-G.5 — reading a frame off a layer, atomically (A-12)
# --------------------------------------------------------------------------

class _Layer:
    def __init__(self, data, metadata=None, scale=None, translate=None):
        self.name = "Reconstruction"
        self.data = data
        self.scale = scale or tuple(1.0 for _ in data.shape)
        self.translate = translate or tuple(0.0 for _ in data.shape)
        self.affine = None
        self.metadata = dict(metadata or {})


def test_frame_from_layer_reads_scale_and_unit_from_one_layer():
    import numpy as np

    from imswitch.improcess.analysis.roi_frame_adapter import frame_from_layer

    layer = _Layer(
        np.zeros((4, 8, 8)),
        metadata={
            "axis_labels": ["Z", "Y", "X"],
            "scale_unit": "um",
            "coordinate_space_uid": "space-1",
            "result_uid": "result-1",
            "dataset_uid": "data-1",
            "plane_axes": ("Y", "X"),
            "identity_kind": "minted",
        },
        scale=(1.0, 0.1, 0.1),
    )

    frame = frame_from_layer(layer)

    assert frame.unit == "um"
    assert frame.shape == (8, 8)
    assert frame.plane_axes == ("Y", "X")
    assert frame.coordinate_space_uid == "space-1"
    assert frame.identity_kind == "minted"
    assert [axis.label for axis in frame.axes] == ["Z", "Y", "X"]
    assert frame.axis("Z").size == 4


def test_layer_without_provenance_yields_a_derived_frame():
    """Measuring still works; it just cannot claim an exact match."""
    import numpy as np

    from imswitch.improcess.analysis.roi_frame_adapter import frame_from_layer

    frame = frame_from_layer(_Layer(np.zeros((8, 8))))

    assert frame is not None
    assert frame.identity_kind == "derived"
    assert compatibility(frame, frame) != "exact"


def test_plane_position_is_axis_labelled_and_skips_displayed_axes():
    import numpy as np
    from types import SimpleNamespace

    from imswitch.improcess.analysis.roi_frame_adapter import frame_from_layer, plane_position

    layer = _Layer(
        np.zeros((4, 3, 8, 8)),
        metadata={"axis_labels": ["T", "Z", "Y", "X"], "plane_axes": ("Y", "X")},
    )
    viewer = SimpleNamespace(dims=SimpleNamespace(current_step=(2, 1, 0, 0)))

    position = plane_position(viewer, frame_from_layer(layer))

    assert position == (("T", 2), ("Z", 1))
