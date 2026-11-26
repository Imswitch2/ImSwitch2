"""The ROI set a user controls: stable ids through segmentation, drawing and
deletion; drawn shapes mapped from the displayed (rotated/flipped) live view
to the camera's pixels exactly; saved and loaded with its grid; read by
roi-dff's ``mask_source: file`` with the same ids."""
import itertools

import numpy as np
import pytest

from imswitch.imcontrol.controller.display_transform import DisplayTransform, apply_display_transform
from imswitch.imcontrol.model.smartpipeline.roi_set import (
    RoiSet, display_polygon_to_detector, polygon_mask, segment, shape_polygon,
)


def _cells(shape=(40, 60)):
    image = np.full(shape, 10.0)
    image[5:12, 5:12] = 200.0          # cell 1
    image[20:30, 30:40] = 200.0        # cell 2
    image[5:10, 45:55] = 200.0         # cell 3
    return image


PARAMS = {"threshold_method": "otsu", "min_area": 5, "smooth_sigma": 0.0, "fill_holes": True, "clear_border": False}


def test_ids_are_stable_through_segment_draw_delete_and_resegment():
    rois = RoiSet.empty((40, 60))
    first = rois.replace_segmented(segment(_cells(), PARAMS), PARAMS)
    assert first == [1, 2, 3] and len(rois) == 3
    v0 = rois.version
    drawn = rois.add_polygon([[30, 2], [30, 12], [38, 12], [38, 2]], kind="rectangle")
    assert drawn == 4 and rois.info[4]["source"] == "drawn" and rois.version > v0
    assert rois.remove([2]) == [2]
    assert rois.ids() == [1, 3, 4]                                   # nothing renumbered
    assert rois.add_polygon([[0.3, 0.3], [0.3, 0.6], [0.6, 0.6]]) is None  # no pixel centre inside
    # re-segment: the segmented ROIs are replaced by new ids, the drawn one stays
    again = rois.replace_segmented(segment(_cells(), PARAMS), PARAMS)
    assert again == [5, 6, 7] and rois.ids() == [4, 5, 6, 7]
    assert rois.info[4]["source"] == "drawn" and rois.segmentation["min_area"] == 5
    table = {row["id"]: row for row in rois.table()}
    assert table[4]["area"] == 9 * 11 and table[4]["centroid"] == pytest.approx((34.0, 7.0))
    assert table[4]["bbox"] == pytest.approx((29.5, 1.5, 38.5, 12.5))


def test_a_drawn_roi_takes_its_pixels_and_an_emptied_roi_is_gone():
    rois = RoiSet.empty((40, 60))
    rois.replace_segmented(segment(_cells(), PARAMS))
    rois.add_polygon([[4, 4], [4, 13], [13, 13], [13, 4]])          # covers cell 1 completely
    assert 1 not in rois.ids() and rois.ids() == [2, 3, 4]
    outline = rois.outline()
    assert set(np.unique(outline)) <= set(rois.ids()) | {0}
    assert outline[8, 8] == 0 and outline[4, 8] == 4                 # inside empty, border kept


@pytest.mark.parametrize("rotation, flip_x, flip_y",
                         list(itertools.product((0, 90, 180, 270), (False, True), (False, True))))
def test_a_shape_drawn_on_the_displayed_image_selects_exactly_the_camera_pixels_shown_inside_it(rotation, flip_x,
                                                                                                flip_y):
    raw_shape = (30, 50)                       # not square: rows and columns must not be swapped by mistake
    transform = DisplayTransform(rotation, flip_x, flip_y)
    ids = np.arange(raw_shape[0] * raw_shape[1]).reshape(raw_shape)
    displayed, _ = apply_display_transform(ids, None, transform)
    # a rectangle and a triangle drawn on the displayed image, in displayed pixels
    for kind, display_vertices in (("rectangle", [[2.5, 3.5], [2.5, 12.5], [9.5, 12.5], [9.5, 3.5]]),
                                   ("polygon", [[1.2, 1.3], [14.7, 4.1], [6.1, 15.8]])):
        polygon = shape_polygon(kind, display_vertices)
        shown_inside = polygon_mask(polygon, displayed.shape)
        expected = set(displayed[shown_inside].tolist())
        rois = RoiSet.empty(raw_shape)
        rois.add_polygon(display_polygon_to_detector(polygon, raw_shape, transform), kind=kind)
        got = set(ids[rois.labels > 0].tolist())
        assert got == expected and got, (kind, transform)


def test_an_ellipse_from_the_corners_of_its_box():
    corners = np.array([[10.0, 10.0], [10.0, 30.0], [20.0, 30.0], [20.0, 10.0]])
    outline = shape_polygon("ellipse", corners)
    assert outline.shape == (64, 2)
    assert outline[:, 0].min() == pytest.approx(10.0) and outline[:, 0].max() == pytest.approx(20.0, abs=0.02)
    assert outline[:, 1].min() == pytest.approx(10.0, abs=0.05) and outline[:, 1].max() == pytest.approx(30.0)
    mask = polygon_mask(outline, (40, 40))
    assert mask[15, 20] and not mask[10, 10] and mask.sum() == pytest.approx(np.pi * 5 * 10, rel=0.1)


def test_save_and_load_keep_ids_grid_region_and_stage(tmp_path):
    identity = {"shape": [40, 60], "binning": [2, 2], "offset": [100, 200], "axis_order": "yx"}
    rois = RoiSet.empty((40, 60), detector="Camera", identity=identity, region=3,
                        stage_position_um={"Stage": {"X": 1.5, "Y": -2.0}})
    rois.replace_segmented(segment(_cells(), PARAMS), PARAMS)
    rois.add_polygon([[30, 2], [30, 12], [38, 12], [38, 2]])
    rois.remove([1])
    path = rois.save(tmp_path / "rois")
    assert path.endswith(".tif") and (tmp_path / "rois.json").exists()
    loaded = RoiSet.load(tmp_path / "rois.json")
    assert loaded.ids() == [2, 3, 4] and loaded.next_id == 5 and np.array_equal(loaded.labels, rois.labels)
    assert loaded.info[4]["source"] == "drawn" and loaded.region == 3 and loaded.detector == "Camera"
    assert loaded.stage_position_um == {"Stage": {"X": 1.5, "Y": -2.0}} and loaded.identity == identity
    assert loaded.add_polygon([[0, 0], [0, 3], [3, 3], [3, 0]]) == 5           # never reuses an id
    # the grid: the same fits, another binning or ROI offset does not, and says what to do
    assert loaded.grid_problems(identity) == []
    problems = loaded.grid_problems({**identity, "binning": [1, 1]})
    assert problems and "binning" in problems[0] and "segment / draw the ROIs again" in problems[0]
    # a bare label image loads too: its labels are the ids
    np.save(tmp_path / "plain.npy", rois.labels)
    plain = RoiSet.load(tmp_path / "plain.npy")
    assert plain.ids() == [2, 3, 4] and plain.info[2]["source"] == "input"


def test_roi_dff_reads_a_saved_set_with_the_same_ids(tmp_path):
    from imswitch.improcess.model.array_result import ArrayProcessingResult
    from imswitch.improcess.processors.roi_dff import RoiDffProcessor

    rois = RoiSet.empty((40, 60))
    rois.replace_segmented(segment(_cells(), PARAMS))
    rois.remove([2])
    path = rois.save(tmp_path / "rois")
    proc, state = RoiDffProcessor(), {}
    params = {**proc.default_params(), "mask_source": "file", "labels_path": path, "baseline_frames": 1,
              "_state": state}
    out = proc.apply(ArrayProcessingResult("cam", _cells(), ["Y", "X"], metadata={"frame_index": 0}), params)
    assert sorted(int(v) for v in out.properties["label"]) == [1, 3]


def test_preflight_classifies_each_roi_scan_before_the_experiment():
    from imswitch.imcommon.model.smartpipeline import ScaleFlip
    from imswitch.imcontrol.model.smartpipeline.preflight import ERROR, Check, classify_rois, errors, roi_checks

    transform = ScaleFlip('t', 'detector:Camera', 'scanner:galvo', pixel_size=(0.5, 0.5), origin=(32.0, 32.0))
    rows = [{'id': 1, 'centroid': (32.0, 32.0), 'bbox': (28.5, 28.5, 35.5, 35.5)},      # centre: fits
            {'id': 5, 'centroid': (2.0, 32.0), 'bbox': (-0.5, 28.5, 4.5, 35.5)},        # -15 um: outside
            {'id': 9, 'centroid': (50.0, 32.0), 'bbox': (46.5, 28.5, 53.5, 35.5)}]      # 9 um: partly outside
    out = classify_rois(rows, transform, axes=['Y', 'X'], params={'margin_um': 0.5, 'min_overlap': 0.5},
                        steps={'Y': 0.5, 'X': 0.5}, ranges={'Y': (-10.0, 10.0), 'X': (-10.0, 10.0)})
    assert [(c['id'], c['status']) for c in out] == [(1, 'ok'), (5, 'skipped'), (9, 'clipped')]
    checks = roi_checks(out)
    assert '1 scan whole, 1 clipped to the scan range, 1 would be skipped' in checks[0].text
    assert any('ROI 5' == c.topic and 'SKIPPED' in c.text for c in checks)
    assert errors(checks + [Check(ERROR, 'x', 'y')])[0].topic == 'x' and not errors(checks)
