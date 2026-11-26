"""The roi-dff processor (frozen mask, per-ROI baseline, trigger) and pick's max-column rule."""

import numpy as np
import pytest

from imswitch.improcess.model.array_result import ArrayProcessingResult
from imswitch.improcess.model.points_result import PointsResult
from imswitch.improcess.processors.pick import PickProcessor
from imswitch.improcess.processors.roi_dff import RoiDffProcessor


def _image_result(image, t=0.0, index=0):
    return ArrayProcessingResult(
        "cam", np.asarray(image), ["Y", "X"], metadata={"frame_index": index, "frame": {"t": t, "identity": None}}
    )


def _scene(*, a=1.0, b=1.0, c=1.0, shape=(64, 64)):
    """Three 8x8 cells on a dark background; the factors scale each cell."""
    image = np.full(shape, 20.0)
    for (y, x), gain in (((8, 8), a), ((8, 40), b), ((40, 20), c)):
        image[y:y + 8, x:x + 8] = 200.0 * gain
    return image


def _run(proc, state, image, t=0.0, index=0, **params):
    merged = {**proc.default_params(), "min_area": 10, "smooth_sigma": 0.0, **params, "_state": state}
    return proc.apply(_image_result(image, t, index), merged)


def _rows(result):
    return {int(label): i for i, label in enumerate(result.properties["label"])}


def test_mask_is_frozen_and_warmup_never_triggers():
    proc, state = RoiDffProcessor(), {}
    out = [_run(proc, state, _scene(), index=i, baseline_frames=5, threshold=0.2) for i in range(5)]
    assert out[0].count == 3
    assert [r.metadata["state"] for r in out[:4]] == [f"warming up, {k} of 5" for k in range(1, 5)]
    assert out[4].metadata["state"] == "warming up, 5 of 5"
    assert not any(bool(np.any(r.properties["triggered"])) for r in out)
    # a bright cell during warm-up is part of the baseline, not a trigger
    state2 = {}
    first = _run(proc, state2, _scene(a=3.0), baseline_frames=3, threshold=0.2)
    assert not first.properties["triggered"].any()
    labels = state["rois"]["label"].copy()
    _run(proc, state, _scene(a=2.0), baseline_frames=5, threshold=0.2)      # a changed scene does not relabel
    assert np.array_equal(state["rois"]["label"], labels)


def test_dff_values_and_sorted_rows_and_box_columns():
    proc, state = RoiDffProcessor(), {}
    for i in range(4):
        _run(proc, state, _scene(), index=i, baseline_frames=4, threshold=0.3)
    result = _run(proc, state, _scene(a=1.5, b=2.0), index=4, baseline_frames=4, threshold=0.3)
    dff = result.properties["dff"]
    assert np.all(np.diff(dff) <= 0)                       # sorted descending
    assert dff[0] == pytest.approx(1.0)                    # cell b doubled
    assert dff[1] == pytest.approx(0.5)
    assert dff[2] == pytest.approx(0.0)
    assert result.properties["triggered"].tolist() == [True, True, False]
    top = 0
    assert result.properties["F"][top] == pytest.approx(400.0)
    assert result.properties["F0"][top] == pytest.approx(200.0)
    # bounding box of cell b: rows 8..15, cols 40..47 -> half-pixel edges
    assert [result.properties[k][top] for k in ("bbox_y0", "bbox_x0", "bbox_y1", "bbox_x1")] == [7.5, 39.5, 15.5, 47.5]
    assert result.coordinates[top].tolist() == [11.5, 43.5]
    assert result.properties["area"][top] == 64


def test_consecutive_frames_and_refractory():
    proc, state = RoiDffProcessor(), {}
    kw = dict(baseline_frames=3, threshold=0.3, min_consecutive=2, refractory_frames=3)
    for i in range(3):
        _run(proc, state, _scene(), index=i, **kw)
    flags = []
    for i in range(3, 12):
        r = _run(proc, state, _scene(b=2.0), index=i, **kw)
        flags.append(bool(r.properties["triggered"].any()))
    # frame 3 above (streak 1), frame 4 triggers; refractory 3 frames; again on frame 8
    assert flags == [False, True, False, False, False, True, False, False, False]


def test_refractory_seconds_uses_the_frame_clock():
    proc, state = RoiDffProcessor(), {}
    kw = dict(baseline_frames=2, threshold=0.3, refractory_s=5.0)
    for i in range(2):
        _run(proc, state, _scene(), t=float(i), index=i, **kw)
    hits = [bool(_run(proc, state, _scene(b=2.0), t=float(t), index=t, **kw).properties["triggered"].any())
            for t in (2, 3, 4, 8, 9)]
    assert hits == [True, False, False, True, False]


@pytest.mark.parametrize("rule", ["running-median", "running-percentile"])
def test_running_baseline_excludes_the_newest_frames(rule):
    proc, state = RoiDffProcessor(), {}
    kw = dict(baseline=rule, baseline_frames=4, baseline_exclude_frames=2, threshold=0.3)
    seen = []
    for i in range(6):                         # N + M = 6 frames of warm-up
        seen.append(_run(proc, state, _scene(), index=i, **kw).metadata["state"])
    assert seen[-1] == "warming up, 6 of 6"
    # a slow rise: +10 percent per frame never reaches 0.3 against a baseline older than M frames... until it does
    gain, fired = 1.0, []
    for i in range(6, 16):
        gain *= 1.1
        fired.append(bool(_run(proc, state, _scene(b=gain), index=i, **kw).properties["triggered"].any()))
    assert not fired[0] and fired[-1]


def test_ema_baseline_is_frozen_while_above_threshold():
    proc, state = RoiDffProcessor(), {}
    kw = dict(baseline="ema", baseline_frames=2, ema_tau_frames=2.0, threshold=0.3)
    for i in range(2):
        _run(proc, state, _scene(), index=i, **kw)
    f0 = state["f0"].copy()
    for i in range(2, 8):
        r = _run(proc, state, _scene(b=3.0), index=i, **kw)
    assert np.allclose(state["f0"], f0)        # nothing moved while the cell stayed up
    assert r.properties["dff"][0] == pytest.approx(2.0)
    for i in range(8, 40):                     # back to rest: the baseline follows the others
        _run(proc, state, _scene(a=1.1, b=1.0), index=i, **kw)
    assert state["f0"].max() > f0.max() * 0.99


def test_zero_baseline_is_invalid_not_infinite():
    proc, state = RoiDffProcessor(), {}
    dark = _scene(a=0.0, b=0.0, c=0.0)          # cells exist in the mask but are dark in the baseline
    mask = np.zeros((64, 64), dtype=np.int32)
    mask[8:16, 8:16] = 1
    labels = ArrayProcessingResult("lab", mask, ["Y", "X"])
    p = dict(mask_source="input", baseline_frames=2, min_f0=1.0, threshold=0.3)
    for i in range(2):
        proc.apply(_image_result(np.zeros((64, 64)), index=i), {**proc.default_params(), **p, "_state": state,
                                                                  "results": [_image_result(np.zeros((64, 64))), labels]})
    r = proc.apply(_image_result(dark + 100, index=2), {**proc.default_params(), **p, "_state": state,
                                                          "results": [_image_result(dark + 100), labels]})
    assert not r.properties["valid"].any() and not r.properties["triggered"].any()
    assert np.isfinite(r.properties["dff"]).all()


def test_background_subtraction_and_median():
    proc, state = RoiDffProcessor(), {}
    kw = dict(baseline_frames=1, background="outside-mean", statistic="median", threshold=0.1)
    _run(proc, state, _scene(), index=0, **kw)
    r = _run(proc, state, _scene(b=2.0), index=1, **kw)
    assert r.properties["F"][0] == pytest.approx(400.0 - 20.0)
    assert r.properties["F0"][0] == pytest.approx(200.0 - 20.0)


def test_shape_change_resets_with_a_message():
    proc, state = RoiDffProcessor(), {}
    for i in range(3):
        _run(proc, state, _scene(), index=i, baseline_frames=3)
    r = _run(proc, state, _scene(shape=(80, 80)), index=3, baseline_frames=3)
    assert "reset" in r.metadata["message"] and r.metadata["state"].startswith("warming up, 1")


def test_segment_from_averaged_reference_frames_and_generation_reset():
    proc, state = RoiDffProcessor(), {}
    r = _run(proc, state, _scene(), index=0, reference_frames=3, baseline_frames=2)
    assert r.count == 0 and r.metadata["state"] == "collecting reference, 1 of 3"
    _run(proc, state, _scene(), index=1, reference_frames=3, baseline_frames=2)
    r = _run(proc, state, _scene(), index=2, reference_frames=3, baseline_frames=2)
    assert r.count == 3
    r = _run(proc, state, _scene(), index=3, reference_frames=3, baseline_frames=2, generation=1)
    assert r.count == 0 and "reset" in r.metadata["message"]


def test_labels_from_a_saved_file(tmp_path):
    mask = np.zeros((64, 64), dtype=np.int32)
    mask[8:16, 8:16] = 5
    path = tmp_path / "mask.npy"
    np.save(path, mask)
    proc, state = RoiDffProcessor(), {}
    r = _run(proc, state, _scene(), mask_source="file", labels_path=str(path), baseline_frames=1)
    assert r.count == 1 and int(r.properties["label"][0]) == 5


def test_pick_max_column_where_triggered():
    props = {"dff": np.array([0.9, 0.7, 0.2]), "triggered": np.array([False, True, True]),
             "label": np.array([1, 2, 3])}
    points = PointsResult("p", np.array([[1.0, 1.0], [2.0, 2.0], [3.0, 3.0]]), properties=props)
    pick = PickProcessor()
    both = pick.apply(points, {"rule": "max-column", "column": "dff", "where": "triggered"})
    assert both.count == 1 and int(both.properties["label"][0]) == 2 and both.metadata["picked_row"] == 1
    plain = pick.apply(points, {"rule": "max-column", "column": "dff"})
    assert int(plain.properties["label"][0]) == 1
    none = pick.apply(PointsResult("p", points.coordinates, properties={**props, "triggered": np.zeros(3, bool)}),
                      {"rule": "max-column", "column": "dff", "where": "triggered"})
    assert none.count == 0
    with pytest.raises(ValueError, match="needs a Column"):
        pick.apply(points, {"rule": "max-column"})
    assert pick.apply(points, {"rule": "first"}).count == 1


def test_per_frame_cost_scales_with_roi_pixels_not_the_frame():
    proc, state = RoiDffProcessor(), {}
    image = _scene(shape=(512, 512))
    _run(proc, state, image, baseline_frames=1)
    import time

    t0 = time.perf_counter()
    for i in range(1, 11):
        _run(proc, state, image, index=i, baseline_frames=1)
    assert (time.perf_counter() - t0) / 10 < 0.05


def test_the_frozen_mask_and_a_bounded_trace_are_kept_for_the_records():
    from imswitch.improcess.processors.roi_dff.processor import TRACE_FRAMES, trace_snapshot

    proc, state = RoiDffProcessor(), {}
    assert trace_snapshot(state) is None
    for i in range(5):
        _run(proc, state, _scene(a=2.0 if i == 4 else 1.0), index=i, baseline_frames=2, threshold=0.2)
    labels = state["labels"]
    assert labels.shape == (64, 64) and set(np.unique(labels)) == {0, 1, 2, 3}
    assert not labels.flags.writeable
    trace = trace_snapshot(state)
    assert trace["dff"].shape == (5, 3) and list(trace["frame"]) == [0, 1, 2, 3, 4]
    column = int(np.flatnonzero(trace["label"] == labels[10, 10])[0])
    assert trace["dff"][-1, column] == pytest.approx(1.0, rel=1e-3)     # the bright cell, last frame
    assert trace["F0"][-1, column] == pytest.approx(200.0)
    trace["dff"][:] = 0      # a copy: the state is not touched
    assert trace_snapshot(state)["dff"][-1, column] == pytest.approx(1.0, rel=1e-3)
    for i in range(5, TRACE_FRAMES + 20):                       # bounded: a ring of TRACE_FRAMES
        _run(proc, state, _scene(), index=i, baseline_frames=2, threshold=0.2)
    trace = trace_snapshot(state)
    assert trace["dff"].shape == (TRACE_FRAMES, 3)
    assert trace["frame"][0] == 20 and trace["frame"][-1] == TRACE_FRAMES + 19
    assert np.all(np.diff(trace["frame"]) == 1)
