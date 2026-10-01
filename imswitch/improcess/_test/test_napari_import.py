"""Taking layers back from a plugin: typed results, fresh grids by default."""

import numpy as np
import pytest

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.labels_result import LabelsResult
from imswitch.improcess.model.napari_import import (
    LayerSnapshot,
    NotImportable,
    grid_decision,
    identity_transform,
    import_layer,
    snapshot_layer,
)
from imswitch.improcess.model.points_table_result import PointsTableResult
from imswitch.improcess.model.provenance import output_node


def _source(shape=(8, 8)):
    return ArrayProcessingResult(
        "recon", np.zeros(shape, dtype=np.float32), ["Y", "X"], axis_scales=[0.5, 0.5], scale_unit="um"
    )


def _snap(layer_type="labels", shape=(8, 8), **overrides):
    ndim = len(shape)
    data = (np.arange(int(np.prod(shape))) % 3).reshape(shape).astype(np.int32)
    if layer_type == "image":
        data = data.astype(np.float32)
    fields = dict(
        name="plugin output", layer_type=layer_type, data=data, ndim=ndim,
        scale=(0.5,) * ndim, translate=(0.0,) * ndim,
        rotate=np.eye(ndim), shear=np.zeros(ndim - 1 if ndim > 1 else 1), affine=np.eye(ndim + 1),
        metadata={"axis_labels": ["Y", "X"]},
    )
    fields.update(overrides)
    return LayerSnapshot(**fields)


class _FakeLayer:
    """The attributes snapshot_layer reads, without napari."""

    def __init__(self):
        self.name = "mask"
        self.data = np.ones((4, 4), dtype=np.int32)
        self.ndim = 2
        self.scale = np.array([1.0, 1.0])
        self.translate = np.array([0.0, 0.0])
        self.rotate = np.eye(2)
        self.shear = np.zeros(1)
        self.affine = type("A", (), {"affine_matrix": np.eye(3)})()
        self.metadata = {"axis_labels": ["Y", "X"]}
        self.properties = {}
        self.units = None


class Labels(_FakeLayer):
    pass


def test_snapshot_reads_the_transform_and_names_the_type_by_class():
    snap = snapshot_layer(Labels())
    assert snap.layer_type == "labels"
    assert snap.scale == (1.0, 1.0)
    assert identity_transform(snap) == (True, "")


# -- grid rules --------------------------------------------------------------------

def test_an_import_gets_a_fresh_grid_by_default():
    source = _source()
    imported = import_layer(_snap(), source, plugin_name="p", widget_name="w")
    assert imported.grid == "fresh"
    assert isinstance(imported.result, LabelsResult)
    assert imported.result.coordinate_space_uid != source.coordinate_space_uid
    assert imported.result.dataset_uid == source.dataset_uid          # still derived from it


def test_identity_transform_on_the_same_grid_may_inherit_when_the_adapter_says_so():
    source = _source()
    imported = import_layer(_snap(), source, plugin_name="p", preserves_grid=True)
    assert imported.grid == "inherit"
    assert imported.result.coordinate_space_uid == source.coordinate_space_uid


@pytest.mark.parametrize(
    "override, phrase",
    [
        ({"translate": (0.0, 2.0)}, "translated"),
        ({"rotate": np.array([[0.0, -1.0], [1.0, 0.0]])}, "rotated"),
        ({"shear": np.array([0.3])}, "sheared"),
        ({"affine": np.array([[1.0, 0.2, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])}, "affine"),
        ({"scale": (1.0, 1.0)}, "scale"),
        ({"metadata": {"axis_labels": ["X", "Y"]}}, "axis order"),
        ({"metadata": {"axis_labels": ["Y", "X"], "result_uid": "someone-else"}}, "different result"),
    ],
)
def test_any_non_identity_or_mismatch_forces_a_fresh_grid(override, phrase):
    grid, reason = grid_decision(_snap(**override), _source(), preserves_grid=True)
    assert grid == "fresh"
    assert phrase in reason


def test_shape_equality_alone_is_not_enough():
    """Same shape, non-zero translate: the classic false positive."""
    grid, _ = grid_decision(_snap(translate=(1.0, 0.0)), _source(), preserves_grid=True)
    assert grid == "fresh"


def test_a_different_shape_cannot_inherit():
    grid, reason = grid_decision(_snap(shape=(8, 6), scale=(0.5, 0.5)), _source(), preserves_grid=True)
    assert grid == "fresh" and "shape" in reason


# -- typed results -------------------------------------------------------------------

def test_an_image_layer_becomes_an_image_result_with_the_layer_scale():
    imported = import_layer(_snap("image"), _source(), plugin_name="p")
    assert isinstance(imported.result, ArrayProcessingResult)
    assert imported.result.axis_scales == [0.5, 0.5]
    assert imported.result.scale_unit == "um"


def test_points_become_a_table_never_localizations():
    snap = _snap(
        "points", data=np.array([[1.0, 2.0], [3.0, 4.0]]),
        metadata={}, scale=(1.0, 1.0), translate=(0.0, 0.0), shear=np.zeros(1),
    )
    snap = LayerSnapshot(**{**snap.__dict__, "properties": {"score": np.array([0.1, 0.9])}})
    imported = import_layer(snap, _source(), plugin_name="p")
    assert isinstance(imported.result, PointsTableResult)
    assert imported.result.kind == "table"
    assert imported.result.table_columns() == ["y", "x", "score"]
    assert imported.result.table_records()[1] == {"y": 3.0, "x": 4.0, "score": pytest.approx(0.9)}


def test_shapes_become_rois_not_a_result():
    snap = _snap(
        "shapes",
        data=[np.array([[1.0, 1.0], [1.0, 5.0], [4.0, 5.0], [4.0, 1.0]])],
        shape_types=("rectangle",), metadata={},
    )
    imported = import_layer(snap, _source(), plugin_name="p")
    assert imported.result is None
    assert len(imported.rois) == 1
    assert imported.rois[0].roi_type == "rectangle"
    assert imported.rois[0].source.startswith("napari:")


def test_unmappable_layer_types_are_refused():
    with pytest.raises(NotImportable):
        import_layer(_snap("surface"), _source(), plugin_name="p")


# -- provenance ---------------------------------------------------------------------------

def test_an_import_records_a_non_replayable_node_pointing_at_the_source():
    source = _source()
    imported = import_layer(_snap(), source, plugin_name="napari-skimage", widget_name="Automated Threshold")
    node = output_node(imported.result)
    assert node["op"] == "napari-import"
    assert node["replayable"] is False
    assert node["plugin_name"] == "napari-skimage"
    assert node["grid"] == "fresh"
    assert node["inputs"][0]["node"]                     # references the source's node


def test_labels_result_refuses_float_data():
    with pytest.raises(TypeError):
        LabelsResult("x", np.zeros((2, 2), dtype=np.float32))
