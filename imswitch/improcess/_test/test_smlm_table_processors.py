"""SMLM Phase 6 table processors: filter, drift correction, group/link."""

import numpy as np
import pytest

from imswitch.improcess.analysis.smlm_tables import (
    apply_drift,
    cross_correlation_shift,
    estimate_drift,
    filter_by_property,
    histogram_bin_count,
    histogram_edges,
    filter_localizations,
    informative_columns,
    link_localizations,
)
from imswitch.improcess.model import LocalizationResult, localizations_from_columns
from imswitch.improcess.processors import available_processor_ids
from imswitch.improcess.processors.smlm_drift import (
    DriftCorrectedLocalizationResult,
    SmlmDriftProcessor,
)
from imswitch.improcess.processors.smlm_filter import SmlmFilterProcessor
from imswitch.improcess.processors.smlm_group import SmlmGroupProcessor


def _locs(**overrides):
    columns = {
        "frame": np.array([0, 1, 2, 3]),
        "x_nm": np.array([100.0, 200.0, 300.0, 400.0]),
        "y_nm": np.array([150.0, 250.0, 350.0, 450.0]),
        "sigma_x_nm": np.array([100.0, 120.0, 140.0, 160.0]),
        "sigma_y_nm": np.array([100.0, 120.0, 140.0, 160.0]),
        "photons": np.array([500.0, 1000.0, 1500.0, 2000.0]),
    }
    columns.update(overrides)
    return localizations_from_columns(columns)


def _result(locs, name="locs"):
    return LocalizationResult(name, locs, pixel_size_nm=100.0)


# --- filtering ---------------------------------------------------------------


def test_filter_localizations_by_photons_sigma_and_frame():
    locs = _locs()

    filtered, mask = filter_localizations(
        locs, min_photons=800.0, max_sigma_nm=150.0, max_frame=2
    )

    assert mask.tolist() == [False, True, True, False]
    assert list(filtered.frame) == [1, 2]


def test_filter_by_property_bandpass_keeps_the_band_inclusive():
    filtered, mask = filter_by_property(_locs(), "photons", lower=1000.0, upper=1500.0)
    assert mask.tolist() == [False, True, True, False]
    assert list(filtered.frame) == [1, 2]


def test_filter_by_property_bandstop_drops_the_band():
    _filtered, mask = filter_by_property(
        _locs(), "photons", lower=1000.0, upper=1500.0, mode="bandstop"
    )
    assert mask.tolist() == [True, False, False, True]


def test_filter_by_property_leaves_an_unset_side_open():
    _filtered, lower_only = filter_by_property(_locs(), "frame", lower=2)
    _filtered, upper_only = filter_by_property(_locs(), "frame", upper=1)
    _filtered, open_band = filter_by_property(_locs(), "sigma_x_nm")
    assert lower_only.tolist() == [False, False, True, True]
    assert upper_only.tolist() == [True, True, False, False]
    assert open_band.all()


def test_filter_by_property_refuses_nonsense():
    with pytest.raises(ValueError, match="Unknown localization property"):
        filter_by_property(_locs(), "brightness")
    with pytest.raises(ValueError, match="Unknown filter mode"):
        filter_by_property(_locs(), "photons", mode="notch")
    with pytest.raises(ValueError, match="above the upper"):
        filter_by_property(_locs(), "photons", lower=10.0, upper=5.0)


def test_informative_columns_skip_the_all_zero_ones():
    columns = informative_columns(_locs())
    assert columns == ("frame", "x_nm", "y_nm", "sigma_x_nm", "sigma_y_nm", "photons")
    with_z = informative_columns(_locs(z_nm=np.array([0.0, 10.0, 0.0, 0.0])))
    assert "z_nm" in with_z


def test_filter_processor_produces_localization_result():
    result = _result(_locs())

    filtered = SmlmFilterProcessor().apply(result, {"property": "photons", "lower": 800.0})

    assert isinstance(filtered, LocalizationResult)
    assert filtered.kind == "localization"
    assert filtered.count == 3
    assert filtered.pixel_size_nm == result.pixel_size_nm
    assert filtered.metadata["filter_kept"] == 3
    assert filtered.metadata["filter_total"] == 4
    assert filtered.metadata["filter_property"] == "photons"
    assert "(Photons filtered 3/4)" in filtered.name


def test_filter_processor_defaults_keep_everything():
    result = _result(_locs())
    filtered = SmlmFilterProcessor().apply(result, SmlmFilterProcessor.default_params())
    assert filtered.count == result.count


def test_filter_processor_bandstop_drops_the_band():
    filtered = SmlmFilterProcessor().apply(
        _result(_locs()),
        {"property": "sigma_x_nm", "mode": "bandstop", "lower": 110.0, "upper": 150.0},
    )
    assert list(filtered.locs.sigma_x_nm) == [100.0, 160.0]


def test_filter_processor_migrates_a_one_property_version_1_run():
    processor = SmlmFilterProcessor()
    assert processor.params_version == 2
    migrated = processor.migrate_params(
        {"min_photons": 800.0, "max_photons": 0.0, "min_sigma_nm": 0.0,
         "max_sigma_nm": 0.0, "min_frame": 0, "max_frame": 0},
        1,
    )
    assert migrated == {"property": "photons", "mode": "bandpass", "lower": 800.0, "upper": None}
    assert processor.migrate_params({"min_frame": 0, "max_frame": 2}, 1) == {
        "property": "frame", "mode": "bandpass", "lower": None, "upper": 2.0,
    }
    # Nothing bounded: the open default.
    assert processor.migrate_params({}, 1) == SmlmFilterProcessor.default_params()
    # Already current: untouched.
    assert processor.migrate_params({"property": "frame"}, 2) == {"property": "frame"}


def test_filter_processor_refuses_to_migrate_what_one_band_cannot_say():
    processor = SmlmFilterProcessor()
    with pytest.raises(ValueError, match="one filter per property"):
        processor.migrate_params({"min_photons": 800.0, "max_frame": 2}, 1)
    with pytest.raises(ValueError, match="both lateral sigmas"):
        processor.migrate_params({"max_sigma_nm": 150.0}, 1)


# --- drift correction --------------------------------------------------------


def test_cross_correlation_shift_recovers_known_shift():
    rng = np.random.default_rng(1)
    reference = rng.random((64, 64))
    shifted = np.roll(np.roll(reference, 3, axis=0), -5, axis=1)

    dy, dx = cross_correlation_shift(reference, shifted)

    assert dy == pytest.approx(3.0, abs=0.2)
    assert dx == pytest.approx(-5.0, abs=0.2)


def _drifting_cloud(n_frames=40, drift_per_frame=(4.0, -3.0), seed=0):
    """A fixed emitter pattern re-observed every frame under linear drift."""
    rng = np.random.default_rng(seed)
    base_x = rng.uniform(0, 4000, 60)
    base_y = rng.uniform(0, 4000, 60)
    frames = np.repeat(np.arange(n_frames), base_x.size)
    x = np.tile(base_x, n_frames) + frames * drift_per_frame[0]
    y = np.tile(base_y, n_frames) + frames * drift_per_frame[1]
    return localizations_from_columns(
        {
            "frame": frames,
            "x_nm": x,
            "y_nm": y,
            "photons": np.full(frames.size, 1000.0),
        }
    )


def test_estimate_drift_recovers_linear_drift():
    locs = _drifting_cloud()

    estimate = estimate_drift(locs, segments=8, render_pixel_size_nm=10.0)

    # Compare drift accumulated between the first and last segment centers
    # against the known linear drift rate.
    span = estimate.segment_centers[-1] - estimate.segment_centers[0]
    assert estimate.segment_dx_nm[-1] / span == pytest.approx(4.0, rel=0.15)
    assert estimate.segment_dy_nm[-1] / span == pytest.approx(-3.0, rel=0.15)


def test_apply_drift_tightens_the_point_cloud():
    locs = _drifting_cloud()
    estimate = estimate_drift(locs, segments=8, render_pixel_size_nm=10.0)

    corrected = apply_drift(locs, estimate)

    # Per-emitter spread: emitters repeat in tile order every 60 rows.
    x_before = locs.x_nm.reshape(-1, 60)
    x_after = corrected.x_nm.reshape(-1, 60)
    assert x_after.std(axis=0).mean() < 0.2 * x_before.std(axis=0).mean()


def test_drift_processor_returns_corrected_result_with_trace():
    result = _result(_drifting_cloud(), name="storm")

    corrected = SmlmDriftProcessor().apply(
        result, {"segments": 8, "render_pixel_size_nm": 10.0}
    )

    assert isinstance(corrected, DriftCorrectedLocalizationResult)
    assert corrected.kind == "localization"
    assert corrected.count == result.count
    assert corrected.metadata["drift_max_nm"] > 50.0
    payloads = corrected.plot_payloads()
    assert payloads[0].title == "Estimated drift"
    assert {series.name for series in payloads[0].series} == {"X drift", "Y drift"}


def test_estimate_drift_rejects_degenerate_inputs():
    with pytest.raises(ValueError, match="empty"):
        estimate_drift(_locs()[:0], segments=2)
    with pytest.raises(ValueError, match="at least 2"):
        estimate_drift(_locs(), segments=1)
    with pytest.raises(ValueError, match="too short"):
        estimate_drift(_locs(), segments=100)


# --- grouping / linking ------------------------------------------------------


def test_link_localizations_merges_consecutive_frames():
    locs = localizations_from_columns(
        {
            "frame": np.array([0, 0, 1, 1, 2]),
            # Emitter A near (100, 100) blinks in frames 0-2; emitter B near
            # (5000, 5000) appears in frames 0-1.
            "x_nm": np.array([100.0, 5000.0, 110.0, 5010.0, 105.0]),
            "y_nm": np.array([100.0, 5000.0, 105.0, 4995.0, 102.0]),
            "photons": np.array([1000.0, 500.0, 1000.0, 500.0, 2000.0]),
        }
    )

    merged = link_localizations(locs, radius_nm=50.0)

    assert len(merged) == 2
    merged_a = merged[np.argmin(merged.x_nm)]
    merged_b = merged[np.argmax(merged.x_nm)]
    assert merged_a["photons"] == pytest.approx(4000.0)
    assert merged_b["photons"] == pytest.approx(1000.0)
    assert merged_a["frame"] == 0
    # Photon-weighted mean of 100, 110, 105 with weights 1000, 1000, 2000.
    assert merged_a["x_nm"] == pytest.approx((100 + 110 + 2 * 105) / 4.0)


def test_link_localizations_respects_dark_frames():
    locs = localizations_from_columns(
        {
            "frame": np.array([0, 3]),
            "x_nm": np.array([100.0, 100.0]),
            "y_nm": np.array([100.0, 100.0]),
            "photons": np.array([1000.0, 1000.0]),
        }
    )

    strict = link_localizations(locs, radius_nm=50.0, max_dark_frames=0)
    tolerant = link_localizations(locs, radius_nm=50.0, max_dark_frames=2)

    assert len(strict) == 2
    assert len(tolerant) == 1


def test_group_processor_produces_localization_result():
    locs = localizations_from_columns(
        {
            "frame": np.array([0, 1]),
            "x_nm": np.array([100.0, 105.0]),
            "y_nm": np.array([100.0, 103.0]),
            "photons": np.array([800.0, 1200.0]),
        }
    )
    result = _result(locs)

    grouped = SmlmGroupProcessor().apply(result, {"radius_nm": 50.0})

    assert isinstance(grouped, LocalizationResult)
    assert grouped.count == 1
    assert grouped.metadata["group_input"] == 2
    assert grouped.metadata["group_output"] == 1


# --- registration and gating -------------------------------------------------


def test_smlm_table_processors_are_registered():
    ids = available_processor_ids()
    assert "smlm-filter" in ids
    assert "smlm-drift" in ids
    assert "smlm-group" in ids


def test_smlm_table_processors_accept_only_localization_results():
    from imswitch.improcess.model import ArrayProcessingResult

    image = ArrayProcessingResult(
        name="img", data=np.zeros((8, 8), dtype=np.float32), axis_labels=["Y", "X"]
    )
    locs_result = _result(_locs())
    for processor in (SmlmFilterProcessor(), SmlmDriftProcessor(), SmlmGroupProcessor()):
        assert processor.accepts(locs_result)
        assert not processor.accepts(image)


def test_a_bandstop_with_no_threshold_is_refused_not_emptied():
    # With neither side set the whole range is the band: it would silently
    # return an empty table.
    with pytest.raises(ValueError, match="band-stop needs at least one threshold"):
        filter_by_property(_locs(), "photons", mode="bandstop")
    # One side is enough, and a bandpass with none keeps everything.
    assert filter_by_property(_locs(), "photons", mode="bandstop", upper=1000.0)[1].sum() == 2
    assert filter_by_property(_locs(), "photons")[1].all()


def test_histogram_bins_follow_the_data_and_never_exceed_the_plot():
    rng = np.random.default_rng(0)
    values = rng.normal(0.0, 1.0, 100_000)
    # Freedman-Diaconis: 2 * IQR * n**(-1/3) = 2 * 1.349 / 46.4 = 0.058 wide.
    assert 100 < histogram_bin_count(values, max_bins=10_000) < 200
    assert histogram_bin_count(values, max_bins=50) == 50
    # One far outlier must not decide the count through the range alone.
    assert histogram_bin_count(np.append(values, 1e9), max_bins=400) == 400
    # Mostly one value: IQR is zero, Sturges' rule answers.
    ties = np.append(np.zeros(1000), [5.0])
    assert histogram_bin_count(ties, max_bins=400) == int(np.ceil(np.log2(1001))) + 1
    assert histogram_bin_count(np.array([3.0]), max_bins=400) == 1
    assert histogram_bin_count(np.full(10, 2.0), max_bins=400) == 1


def test_whole_numbers_get_one_bin_each_and_other_data_equal_bins():
    frames = np.repeat(np.arange(40), 25)
    edges = histogram_edges(frames, max_bins=600)
    counts, _ = np.histogram(frames, bins=edges)
    assert len(counts) == 40 and (counts == 25).all()  # no bin holds two frames

    # Whole numbers too many for the plot: equal bins instead.
    wide = np.arange(0, 20000)
    assert len(histogram_edges(wide, max_bins=400)) - 1 <= 400

    continuous = np.random.default_rng(0).gamma(4.0, 600.0, 50_000)
    edges = histogram_edges(continuous, max_bins=600)
    assert edges[0] == continuous.min() and edges[-1] == continuous.max()
    assert np.allclose(np.diff(edges), np.diff(edges)[0])

    assert list(histogram_edges(np.full(5, 3.0), max_bins=100)) == [2.5, 3.5]
    assert list(histogram_edges(np.array([]), max_bins=100)) == [0.0, 1.0]
