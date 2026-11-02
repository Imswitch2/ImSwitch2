"""The small result kinds a smart pipeline is made of: points in a frame of
reference, one number, one yes/no."""
import csv
import json

import numpy as np
import pytest

from imswitch.improcess.model import (
    RESULT_KINDS, EventResult, PointsResult, ValueResult, result_kind,
)
from imswitch.improcess.model.workfloweditor.previews import make_preview


def test_the_kinds_are_declared_result_kinds():
    assert {"points", "value", "event"} <= set(RESULT_KINDS)


def test_points_carry_their_frame_and_source_frame_index():
    points = PointsResult("spots", [[1.0, 2.0], [3.0, 4.5]], frame="detector:Camera",
                          frame_index=7, properties={"area": [10, 20]})

    assert result_kind(points) == "points"
    assert points.frame == "detector:Camera" and points.frame_index == 7
    assert points.count == 2 and points.table_columns() == ["y", "x", "area"]
    assert points.table_records()[1] == {"y": 3.0, "x": 4.5, "area": 20}


def test_one_point_may_be_given_flat_and_bad_shapes_are_refused():
    assert PointsResult("p", [5.0, 6.0]).coordinates.shape == (1, 2)
    with pytest.raises(ValueError, match=r"\(N, 2\) or \(N, 3\)"):
        PointsResult("p", np.zeros((2, 4)))


def test_in_frame_re_expresses_the_points_and_keeps_everything_else():
    points = PointsResult("spots", [[1.0, 2.0]], frame="detector:Camera", frame_index=3,
                          properties={"area": [10]}, metadata={"note": "kept"})

    moved = points.in_frame("stage:XY", [[100.0, 200.0]])

    assert moved.frame == "stage:XY" and moved.frame_index == 3
    assert moved.coordinates.tolist() == [[100.0, 200.0]]
    assert moved.properties["area"].tolist() == [10] and moved.metadata["note"] == "kept"
    assert points.frame == "detector:Camera"  # the original is untouched


def test_a_value_is_one_number_with_a_unit():
    value = ValueResult("focus score", np.float32(3.5), unit="um")

    assert result_kind(value) == "value" and value.value == 3.5 and value.unit == "um"
    assert value.table_records() == [{"value": 3.5, "unit": "um"}]
    with pytest.raises(ValueError, match="single number"):
        ValueResult("v", np.zeros(3))


def test_an_event_is_a_truth_with_a_payload():
    event = EventResult("detected", True, payload={"x": 1.5, "n": 2})

    assert result_kind(event) == "event" and bool(event) and not bool(EventResult("e", False))
    assert event.table_columns() == ["fired", "x", "n"]
    assert event.table_records() == [{"fired": True, "x": 1.5, "n": 2}]


def test_values_and_events_save_as_a_csv_row_with_a_companion(tmp_path):
    from imswitch.improcess.model.save_protocol import companion_json_path

    class Document:  # the provenance document, as the save protocol sees it
        def to_json(self):
            return json.dumps({"name": "doc"})

    event = EventResult("detected", True, payload={"where": [1, 2]})
    path = tmp_path / "event.csv"
    plan = event.plan_save(path, "csv")
    event.write_files(plan, Document())

    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [{"fired": "True", "where": "[1, 2]"}]
    assert companion_json_path(path).exists()
    assert json.loads(companion_json_path(path).read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="CSV"):
        ValueResult("v", 1.0).plan_save(path, "tiff")


def test_the_editor_previews_them_as_rows():
    points = PointsResult("spots", [[1.0, 2.0]], frame="detector:Camera")
    for result in (points, ValueResult("v", 2.0), EventResult("e", False)):
        preview = make_preview(result)
        assert preview.kind == result_kind(result) and preview.row_count == 1
        assert "1 row" in preview.describe()
