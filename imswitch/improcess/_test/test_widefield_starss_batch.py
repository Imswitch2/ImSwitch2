import h5py
import numpy as np
import pytest
import tifffile as tiff

from imswitch.improcess.reconstructors.widefield_starss.analysis import (
    WidefieldStarssBatchCancelled,
    WidefieldStarssParams,
    discover_widefield_starss_pairs,
    discover_widefield_starss_pairs_in_folder,
    run_widefield_starss_batch,
    run_widefield_starss_batch_from_folder,
)


def _mosaic_frame(i0, i45, i90, i135, shape=(8, 10)):
    frame = np.zeros(shape, dtype=np.float32)
    frame[1::2, 1::2] = i0
    frame[1::2, ::2] = i45
    frame[::2, ::2] = i90
    frame[::2, 1::2] = i135
    return frame


def _alternating_stack(signal_frame, background_frame, scales=(0.95, 1.0, 1.05)):
    frames = []
    for scale in scales:
        frames.append(signal_frame * scale)
        frames.append(background_frame)
    return np.stack(frames, axis=0)


def _write_pair(tmp_path, sample_id, scale=1.0):
    background = np.full((8, 10), 2, dtype=np.float32)
    h_signal = background + _mosaic_frame(
        i0=100 * scale,
        i45=75 * scale,
        i90=50 * scale,
        i135=75 * scale,
    )
    v_signal = background + _mosaic_frame(
        i0=80 * scale,
        i45=60 * scale,
        i90=40 * scale,
        i135=60 * scale,
    )
    h_path = tmp_path / f"{sample_id}_h.tif"
    v_path = tmp_path / f"{sample_id}_v.tif"
    tiff.imwrite(h_path, _alternating_stack(h_signal, background))
    tiff.imwrite(v_path, _alternating_stack(v_signal, background))
    return h_path, v_path


def test_discover_widefield_starss_pairs_matches_hv_files(tmp_path):
    h0, v0 = _write_pair(tmp_path, "cell_000")
    h1, v1 = _write_pair(tmp_path, "cell_001")
    unmatched = tmp_path / "cell_002_h.tif"
    tiff.imwrite(unmatched, np.zeros((2, 8, 10), dtype=np.float32))

    pairs, unmatched_paths = discover_widefield_starss_pairs([v1, unmatched, h0, v0, h1])

    assert [pair.sample_id for pair in pairs] == ["cell_000", "cell_001"]
    assert pairs[0].h_path == h0
    assert pairs[0].v_path == v0
    assert unmatched_paths == [unmatched]


def test_discover_widefield_starss_pairs_in_folder(tmp_path):
    _write_pair(tmp_path, "sample")

    pairs, unmatched = discover_widefield_starss_pairs_in_folder(tmp_path)

    assert len(pairs) == 1
    assert pairs[0].sample_id == "sample"
    assert unmatched == []


def test_run_widefield_starss_batch_aggregates_regions_and_summary(tmp_path):
    _write_pair(tmp_path, "cell_000", scale=1.0)
    _write_pair(tmp_path, "cell_001", scale=1.2)
    params = WidefieldStarssParams(
        segmentation_mode="none",
        anisotropy_mode="stokes",
        smooth_sigma=1.0,
    )

    result = run_widefield_starss_batch_from_folder(tmp_path, params=params)

    assert len(result.pairs) == 2
    assert len(result.analyses) == 2
    assert len(result.regions) == 2
    assert len(result.summary) == 2
    assert {"sample_id", "source_h_path", "source_v_path", "anisotropy_direct"}.issubset(
        result.regions.columns
    )
    assert result.summary["region_count"].tolist() == [1, 1]
    assert result.summary["anisotropy_mode"].tolist() == ["stokes", "stokes"]


def test_run_widefield_starss_batch_reports_progress(tmp_path):
    _write_pair(tmp_path, "cell_000")
    pairs, unmatched = discover_widefield_starss_pairs_in_folder(tmp_path)
    events = []

    run_widefield_starss_batch(
        pairs,
        params=WidefieldStarssParams(segmentation_mode="none"),
        unmatched=unmatched,
        progress_callback=events.append,
    )

    assert [event["state"] for event in events] == ["processing", "completed"]
    assert events[0]["sample_id"] == "cell_000"
    assert events[0]["completed"] == 0
    assert events[1]["completed"] == 1
    assert events[1]["pair_count"] == 1


def test_run_widefield_starss_batch_can_cancel_between_pairs(tmp_path):
    _write_pair(tmp_path, "cell_000")
    _write_pair(tmp_path, "cell_001")
    pairs, unmatched = discover_widefield_starss_pairs_in_folder(tmp_path)
    cancel_requested = False

    def progress(event):
        nonlocal cancel_requested
        if event["state"] == "completed":
            cancel_requested = True

    with pytest.raises(WidefieldStarssBatchCancelled):
        run_widefield_starss_batch(
            pairs,
            params=WidefieldStarssParams(segmentation_mode="none"),
            unmatched=unmatched,
            progress_callback=progress,
            cancel_callback=lambda: cancel_requested,
        )


def test_widefield_starss_batch_exports_csv_and_hdf5(tmp_path):
    _write_pair(tmp_path, "cell_000")
    result = run_widefield_starss_batch_from_folder(
        tmp_path,
        params=WidefieldStarssParams(segmentation_mode="none"),
    )
    out_dir = tmp_path / "out"

    regions_csv, summary_csv = result.save_csv(out_dir)
    h5_path = result.save_hdf5(out_dir / "batch.h5")

    assert regions_csv.exists()
    assert summary_csv.exists()
    with h5py.File(h5_path, "r") as h5:
        assert "regions" in h5
        assert "summary" in h5
        assert "pairs" in h5
        assert h5.attrs["pair_count"] == 1
        assert h5.attrs["region_count"] == 1
