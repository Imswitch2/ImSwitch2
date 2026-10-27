"""The Fiji conversion layer: axis mapping, units and reported loss, without Java."""

from __future__ import annotations

import json

import numpy as np
import pytest

from imswitch.improcess.fiji.convert import (
    IMAGEJ_AXES,
    PROP_RESULT_UID,
    FijiImageDescription,
    NotImportableFromFiji,
    NotSendable,
    SentRecord,
    plan_send,
    import_fiji_image,
    to_snapshot,
)
from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.labels_result import LabelsResult
from imswitch.improcess.model.provenance import output_node


def _result(shape, labels, *, scales=None, unit="um", dtype=np.uint16, name="src"):
    n = int(np.prod(shape))
    data = (np.arange(n) % 60000).astype(dtype).reshape(shape)
    return ArrayProcessingResult(name, data, list(labels), axis_scales=scales, scale_unit=unit)


def _desc(plan_or_shape, *, uid="", **over):
    """What the host would report for an ImagePlus built from the plan."""
    t, z, c, y, x = plan_or_shape
    base = dict(
        image_id=-7, title="img", dims_xyczt=(x, y, c, z, t), bit_depth=16,
        pixel_width=1.0, pixel_height=1.0, pixel_depth=1.0,
        x_unit="pixel", y_unit="pixel", z_unit="pixel",
        x_origin=0.0, y_origin=0.0, z_origin=0.0,
        frame_interval=0.0, time_unit="sec",
        properties={PROP_RESULT_UID: uid} if uid else {},
        channel_labels=(), changes=False,
    )
    base.update(over)
    return FijiImageDescription(**base)


def _roundtrip_desc(plan, result, **over):
    cal = plan.calibration
    shape = plan.data.shape
    base = dict(
        pixel_width=cal["pixel_width"], pixel_height=cal["pixel_height"],
        pixel_depth=cal["pixel_depth"] or 1.0,
        x_unit="µm", y_unit="µm", z_unit="µm",
        frame_interval=cal["frame_interval_s"] or 0.0, time_unit="sec",
        title=plan.title, properties=dict(plan.properties),
    )
    base.update(over)
    return _desc(shape, **base)


# -- plan_send ----------------------------------------------------------------

@pytest.mark.parametrize("labels,shape", [
    ("YX", (4, 5)), ("ZYX", (2, 4, 5)), ("CYX", (3, 4, 5)), ("TYX", (3, 4, 5)),
    ("TZCYX", (2, 3, 2, 4, 5)), ("XZCY", (5, 3, 2, 4)),
])
def test_plan_send_gives_canonical_axes_with_identical_pixels(labels, shape):
    result = _result(shape, labels)
    plan = plan_send(result)
    assert plan.data.ndim == 5
    present = [a for a in IMAGEJ_AXES if a in labels]
    expected = np.transpose(result.data, [list(labels).index(a) for a in present])
    assert np.array_equal(np.squeeze(plan.data, axis=tuple(i for i, a in enumerate(IMAGEJ_AXES) if a not in labels)), expected)
    for i, a in enumerate(IMAGEJ_AXES):
        if a not in labels:
            assert plan.data.shape[i] == 1
    assert np.shares_memory(plan.data, result.data)
    assert plan.source_axes == tuple(labels)


def test_calibration_units_and_missing_axes():
    plan = plan_send(_result((4, 5), "YX", scales=[0.2, 0.1]))
    assert plan.calibration["unit"] == "µm"
    assert plan.calibration["pixel_width"] == 0.1 and plan.calibration["pixel_height"] == 0.2
    assert plan.calibration["pixel_depth"] is None
    assert plan.calibration["frame_interval_s"] is None
    assert plan_send(_result((4, 5), "YX", unit="px")).calibration["unit"] == "pixel"
    assert plan_send(_result((2, 4, 5), "ZYX", scales=[0.5, 0.2, 0.1])).calibration["pixel_depth"] == 0.5
    assert plan_send(_result((3, 4, 5), "TYX", scales=[2.0, 1.0, 1.0])).calibration["frame_interval_s"] == 2.0


def test_channel_names_come_from_metadata_and_are_noted_when_absent():
    result = _result((2, 4, 5), "CYX")
    result.metadata["channel_names"] = ["a", "b"]
    assert plan_send(result).channel_names == ["a", "b"]
    plan = plan_send(_result((2, 4, 5), "CYX"))
    assert plan.channel_names == [] and any("channel names" in n for n in plan.notes)


def test_properties_name_the_result_and_axes():
    result = _result((3, 4, 5), "TYX")
    plan = plan_send(result, title="mine")
    assert plan.title == "mine"
    assert plan.properties == {PROP_RESULT_UID: result.result_uid, "imswitch.axes": "T,Y,X"}


@pytest.mark.parametrize("labels,shape,dtype,match", [
    ("BYX", (2, 4, 5), np.uint16, "Base|BYX|\\['B"),
    ("YYX", (2, 4, 5), np.uint16, "more than once"),
    ("ZY", (2, 4), np.uint16, "Y and X"),
    ("YX", (4, 5), np.float64, "float64"),
])
def test_plan_send_refusals(labels, shape, dtype, match):
    with pytest.raises(NotSendable, match=match):
        plan_send(_result(shape, labels, dtype=dtype))


def test_an_extra_axis_is_named_in_the_refusal():
    result = ArrayProcessingResult("r", np.zeros((2, 4, 5), np.uint16), ["Base", "Y", "X"])
    with pytest.raises(NotSendable, match="Base"):
        plan_send(result)


def test_label_results_are_refused_by_kind():
    labels = LabelsResult("l", np.zeros((4, 5), np.int32), ["Y", "X"])
    with pytest.raises(NotSendable, match="labels"):
        plan_send(labels)


def test_lazy_data_without_transpose_is_materialised_and_noted():
    class Lazy:
        ndim, dtype, shape = 2, np.dtype("uint8"), (4, 5)

        def __array__(self, dtype=None, copy=None):
            return np.zeros((4, 5), np.uint8)

    result = _result((4, 5), "YX", dtype=np.uint8)
    result.data = Lazy()
    plan = plan_send(result)
    assert isinstance(plan.data, np.ndarray) and any("materialised" in n for n in plan.notes)


# -- description ----------------------------------------------------------------

def test_description_round_trips_through_json():
    desc = _desc((2, 3, 2, 4, 5), uid="u", channel_labels=("a", None))
    again = FijiImageDescription.from_dict(json.loads(json.dumps(desc.to_dict())))
    assert again == desc


# -- import ---------------------------------------------------------------------

def test_round_trip_restores_data_axes_scales_and_unit():
    source = _result((3, 4, 5), "TYX", scales=[2.0, 0.2, 0.1])
    plan = plan_send(source)
    desc = _roundtrip_desc(plan, source)
    imported = import_fiji_image(desc, plan.data, source, sent=SentRecord(-7, source.result_uid, plan.source_axes, plan.data.shape))
    result = imported.result
    assert np.array_equal(result.data, source.data)
    assert list(result.axis_labels) == ["T", "Y", "X"]
    assert np.allclose(result.axis_scales, source.axis_scales)
    assert result.scale_unit == "um"


def test_grid_is_fresh_by_default_and_inherited_when_declared():
    source = _result((4, 5), "YX", scales=[0.2, 0.1])
    plan = plan_send(source)
    desc = _roundtrip_desc(plan, source)
    sent = SentRecord(-7, source.result_uid, plan.source_axes, plan.data.shape)
    assert import_fiji_image(desc, plan.data, source, sent=sent).grid == "fresh"
    assert import_fiji_image(desc, plan.data, source, sent=sent, preserves_grid=True).grid == "inherit"


def test_a_crop_with_the_same_uid_hint_is_fresh():
    source = _result((8, 10), "YX", scales=[0.2, 0.1])
    plan = plan_send(source)
    cropped = np.ascontiguousarray(plan.data[:, :, :, :4, :5])
    desc = _roundtrip_desc(plan, source, dims_xyczt=(5, 4, 1, 1, 1))
    sent = SentRecord(-7, source.result_uid, plan.source_axes, plan.data.shape)
    imported = import_fiji_image(desc, cropped, source, sent=sent, preserves_grid=True)
    assert imported.grid == "fresh" and "shape" in imported.reason


def test_a_mismatching_uid_hint_is_fresh():
    source = _result((4, 5), "YX", scales=[0.2, 0.1])
    plan = plan_send(source)
    props = {PROP_RESULT_UID: "result-other"}
    desc = _roundtrip_desc(plan, source, properties=props)
    sent = SentRecord(-7, source.result_uid, plan.source_axes, plan.data.shape)
    assert import_fiji_image(desc, plan.data, source, sent=sent, preserves_grid=True).grid == "fresh"


def test_mixed_length_units_are_converted_onto_the_x_unit():
    data = np.zeros((1, 3, 1, 4, 5), np.uint16)
    desc = _desc((1, 3, 1, 4, 5), pixel_width=0.1, pixel_height=0.1, pixel_depth=500.0,
                 x_unit="µm", y_unit="µm", z_unit="nm")
    snap, _ = to_snapshot(desc, data, sent=None)
    assert snap.metadata["scale_unit"] == "um"
    assert dict(zip(snap.metadata["axis_labels"], snap.scale))["Z"] == pytest.approx(0.5)


def test_pixels_and_lengths_cannot_be_mixed():
    desc = _desc((1, 3, 1, 4, 5), x_unit="µm", y_unit="µm", z_unit="pixel")
    with pytest.raises(NotImportableFromFiji):
        to_snapshot(desc, np.zeros((1, 3, 1, 4, 5), np.uint16), sent=None)


@pytest.mark.parametrize("unit,factor", [("sec", 1.0), ("min", 60.0), ("ms", 1e-3)])
def test_frame_interval_is_converted_to_seconds(unit, factor):
    desc = _desc((3, 1, 1, 4, 5), frame_interval=2.0, time_unit=unit)
    snap, notes = to_snapshot(desc, np.zeros((3, 1, 1, 4, 5), np.uint16), sent=None)
    assert snap.scale[snap.metadata["axis_labels"].index("T")] == pytest.approx(2.0 * factor)
    assert not any("frame interval" in n for n in notes)


def test_unknown_frame_interval_is_the_frame_index_and_noted():
    data = np.zeros((3, 1, 1, 4, 5), np.uint16)
    snap, notes = to_snapshot(_desc((3, 1, 1, 4, 5), frame_interval=0.0), data, sent=None)
    assert snap.scale[0] == 1.0 and any("frame interval unknown" in n for n in notes)
    snap, notes = to_snapshot(_desc((3, 1, 1, 4, 5), frame_interval=2.0, time_unit="fortnight"), data, sent=None)
    assert snap.scale[0] == 1.0 and any("fortnight" in n for n in notes)


def test_a_non_zero_origin_is_reported():
    desc = _desc((1, 1, 1, 4, 5), x_origin=3.0)
    _, notes = to_snapshot(desc, np.zeros((1, 1, 1, 4, 5), np.uint16), sent=None)
    assert any("origin" in n for n in notes)


def test_canonical_restoration_without_a_send_record_is_noted():
    desc = _desc((2, 1, 1, 4, 5))
    snap, notes = to_snapshot(desc, np.zeros((2, 1, 1, 4, 5), np.uint16), sent=None)
    assert snap.metadata["axis_labels"] == ["T", "Y", "X"]
    assert any("canonically" in n for n in notes)


def test_wrong_array_shape_is_refused():
    with pytest.raises(NotImportableFromFiji):
        to_snapshot(_desc((1, 1, 1, 4, 5)), np.zeros((4, 5), np.uint16), sent=None)


def test_provenance_and_metadata_carry_the_notes_and_channel_names():
    source = _result((2, 4, 5), "CYX", scales=[1.0, 0.2, 0.1])
    plan = plan_send(source)
    desc = _roundtrip_desc(plan, source, x_origin=2.0, channel_labels=("DAPI", "GFP"))
    imported = import_fiji_image(desc, plan.data, source, sent=None, details={"op": "blur"})
    node = output_node(imported.result)
    assert node["op"] == "external-import" and node["tool"] == "fiji"
    assert node["details"]["image_id"] == -7 and node["details"]["op"] == "blur"
    assert imported.result.metadata["fiji"]["dropped"] == node["details"]["notes"]
    assert any("origin" in n for n in imported.result.metadata["fiji"]["dropped"])
    assert imported.result.metadata["channel_names"] == ["DAPI", "GFP"]
    json.dumps(node["details"])


def test_a_sent_record_without_notes_adds_nothing_and_one_with_notes_leads_the_import_notes():
    source = _result((2, 4, 5), "CYX", scales=[1.0, 0.2, 0.1])
    plan = plan_send(source)
    assert plan.notes and SentRecord(-7, "u", plan.source_axes, plan.data.shape).notes == ()
    desc = _roundtrip_desc(plan, source, x_origin=2.0, channel_labels=("DAPI", "GFP"))

    plain = import_fiji_image(desc, plan.data, source, sent=SentRecord(-7, "u", plan.source_axes, plan.data.shape))
    assert not any(n.startswith("on send: ") for n in plain.result.metadata["fiji"]["dropped"])

    record = SentRecord(-7, "u", plan.source_axes, plan.data.shape, notes=tuple(plan.notes))
    imported = import_fiji_image(desc, plan.data, source, sent=record)
    dropped = imported.result.metadata["fiji"]["dropped"]
    on_send = [f"on send: {n}" for n in plan.notes]
    assert dropped[: len(on_send)] == on_send
    assert any("origin" in n for n in dropped[len(on_send):])          # the way back's own notes follow
    assert dropped == output_node(imported.result)["details"]["notes"]
    json.dumps(output_node(imported.result)["details"])


def test_uint16_values_above_int16_survive():
    source = _result((4, 5), "YX")
    source.data[0, 0] = 65535
    plan = plan_send(source)
    desc = _roundtrip_desc(plan, source)
    out = import_fiji_image(desc, plan.data, source, sent=None).result
    assert out.data.dtype == np.uint16 and out.data[0, 0] == 65535


# -- results whose writer lays them out differently --------------------------------

class _ViewResult(ArrayProcessingResult):
    """In-memory axes ImageJ cannot take; the writer's layout it can."""

    def __init__(self, view_labels=("C", "Y", "X"), names=("a", "b")):
        data = (np.arange(2 * 2 * 4 * 5) % 250).astype(np.uint16).reshape(2, 2, 4, 5)
        super().__init__("multi", data, ["Dataset", "Base", "Y", "X"], axis_scales=[1, 1, 0.2, 0.1], scale_unit="um")
        self._view_labels, self._names = list(view_labels), list(names)

    def serialization_view(self):
        from imswitch.improcess.model.result import SerializationView

        folded = np.asarray(self.data).reshape(4, 4, 5) if len(self._view_labels) == 3 else np.asarray(self.data)
        return SerializationView(
            data=folded, axis_labels=self._view_labels, axis_scales=[1.0, 200.0, 100.0][-len(self._view_labels):],
            scale_unit="nm", channel_names=[f"{n}{i}" for n in self._names for i in range(2)],
        )


def test_a_result_whose_axes_do_not_map_is_sent_in_its_serialization_layout():
    result = _ViewResult()
    plan = plan_send(result)
    assert plan.source_axes == ("C", "Y", "X")             # what was sent, for restoring an import
    assert plan.data.shape == (1, 1, 4, 4, 5)
    assert plan.channel_names == ["a0", "a1", "b0", "b1"]
    assert plan.calibration["unit"] == "nm" and plan.calibration["pixel_width"] == 100.0
    assert plan.properties[PROP_RESULT_UID] == result.result_uid
    assert any(
        "sent in the layout the result is saved in (C, Y, X), not its in-memory axes (Dataset, Base, Y, X)" in n
        for n in plan.notes
    )


def test_axes_that_map_keep_the_in_memory_layout_even_if_a_view_exists():
    result = _result((3, 4, 5), "ZYX", scales=[0.5, 0.2, 0.1])
    plan = plan_send(result)
    assert plan.source_axes == ("Z", "Y", "X") and not any("saved in" in n for n in plan.notes)


def test_neither_layout_mapping_is_refused_naming_both():
    result = _ViewResult(view_labels=("Angle", "Y", "X"))
    with pytest.raises(NotSendable) as err:
        plan_send(result)
    text = str(err.value)
    assert "Dataset" in text and "Angle" in text and "Neither" in text


def test_a_real_monalisa_result_is_sent_with_dataset_times_base_as_channels():
    from imswitch.improcess.reconstructors.monalisa.result import MonalisaProcessingResult

    coeffs = np.arange(1 * 2 * 4 * 2 * 5, dtype=np.float32).reshape(1, 2, 4, 2, 5)
    scan_params = {
        'dimensions': ['Right-Left', 'Up-Down', 'Back-Front', 'Timepoints'],
        'directions': ['pos', 'pos', 'pos'], 'steps': ['2', '2', '1', '1'],
        'step_sizes': ['40', '80', '120', '1'], 'unidirectional': True,
    }
    labels = {'r_l_text': 'Right-Left', 'u_d_text': 'Up-Down', 'b_f_text': 'Back-Front',
              'timepoints_text': 'Timepoints', 'p_text': 'pos', 'n_text': 'neg'}
    result = MonalisaProcessingResult.from_coeffs("monalisa", coeffs, scan_params, labels)
    plan = plan_send(result)
    view = result.serialization_view()
    assert plan.source_axes == ("T", "Z", "C", "Y", "X")
    assert plan.data.shape[IMAGEJ_AXES.index("C")] == len(view.channel_names) == len(plan.channel_names)
    np.testing.assert_array_equal(np.asarray(plan.data), np.asarray(view.data))
    assert any("saved in" in n for n in plan.notes)
