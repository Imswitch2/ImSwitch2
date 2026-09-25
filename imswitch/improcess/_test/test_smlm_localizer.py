"""Phase 2 tests: SMLM detection, fitting, and the localizer reconstructor."""

from __future__ import annotations

import numpy as np
import pytest
from qtpy import QtWidgets

from imswitch.improcess.model.localization_result import LocalizationResult
from imswitch.improcess.reconstructors.smlm.detection import detect_spots
from imswitch.improcess.reconstructors.smlm.fitting import fit_spot, fit_spots
from imswitch.improcess.reconstructors.smlm.localizer import (
    SmlmLocalizer,
    iter_frames,
    localize_stack,
)


def _gaussian_spot(frame, y, x, amplitude=200.0, sigma=1.3):
    rows, cols = np.indices(frame.shape)
    frame += amplitude * np.exp(
        -((cols - x) ** 2 + (rows - y) ** 2) / (2 * sigma ** 2)
    )
    return frame


def _synthetic_frame(shape=(64, 64), emitters=((16.0, 20.0), (40.0, 48.0), (30.0, 10.0)),
                     background=10.0, amplitude=300.0):
    frame = np.full(shape, background, dtype=np.float32)
    for y, x in emitters:
        _gaussian_spot(frame, y, x, amplitude=amplitude)
    return frame


# -- detection --------------------------------------------------------------


def test_detect_spots_finds_three_emitters():
    emitters = [(16.0, 20.0), (40.0, 48.0), (30.0, 10.0)]
    frame = _synthetic_frame(emitters=emitters)
    coords = detect_spots(frame, threshold=20.0, roi=7, sigma=1.0)
    assert coords.shape[1] == 2
    assert len(coords) == 3
    for y, x in emitters:
        distances = np.hypot(coords[:, 0] - y, coords[:, 1] - x)
        assert distances.min() <= 1.5


def test_detect_spots_blank_frame_finds_nothing():
    frame = np.full((48, 48), 10.0, dtype=np.float32)
    coords = detect_spots(frame, threshold=20.0)
    assert coords.shape == (0, 2)


def test_detect_spots_excludes_edges():
    frame = np.full((32, 32), 5.0, dtype=np.float32)
    _gaussian_spot(frame, 1.0, 1.0, amplitude=500.0)  # near the corner
    coords = detect_spots(frame, threshold=10.0, roi=7)
    assert len(coords) == 0


def test_detect_spots_rejects_non_2d():
    with pytest.raises(ValueError):
        detect_spots(np.zeros((3, 8, 8)), threshold=1.0)


def test_detect_spots_finds_broad_saturated_bead_over_noise():
    """Regression (2026-07-08): a broad, saturated bead over camera noise must
    be detected at a threshold that rejects the noise. The old single-pixel
    'net gradient' (a discrete Laplacian) vanished on the bead's flat top, so
    the true peak was invisible at any usable threshold while noise spikes were
    picked instead — the real Picasso net gradient (inward slopes integrated
    over the ROI) stays sharply peaked at the centre even for a flat top."""
    rng = np.random.default_rng(2)
    height = width = 128
    yy, xx = np.ogrid[:height, :width]
    cy, cx = 80, 40
    bead = 6000.0 * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 4.0 ** 2)))
    bead = np.clip(bead, 0, 3000.0)  # saturated flat top
    frame = (bead + 100.0 + rng.normal(0, 20.0, (height, width))).astype(np.float32)

    coords = detect_spots(frame, threshold=500.0, roi=7, sigma=1.0)

    # The bead is found...
    assert len(coords) >= 1
    dist = np.hypot(coords[:, 0] - cy, coords[:, 1] - cx)
    assert dist.min() <= 4.0
    # ...and the same threshold produces no detections on the noise alone.
    noise_only = (100.0 + rng.normal(0, 20.0, (height, width))).astype(np.float32)
    assert len(detect_spots(noise_only, threshold=500.0, roi=7, sigma=1.0)) == 0


def test_net_gradient_peaks_at_emitter_not_on_flat_background():
    """The net-gradient score is ~0 on flat regions and strongly positive at a
    peak, regardless of the peak's absolute brightness/width."""
    from imswitch.improcess.reconstructors.smlm.detection import net_gradient_map

    frame = np.full((48, 48), 500.0, dtype=np.float32)
    rows, cols = np.indices(frame.shape)
    frame += 200.0 * np.exp(-(((cols - 24) ** 2 + (rows - 24) ** 2) / (2 * 1.5 ** 2)))

    ng = net_gradient_map(frame, roi=7, sigma=1.0)
    assert ng[24, 24] > 100.0
    assert abs(ng[5, 5]) < ng[24, 24] * 0.05  # flat corner ~ 0


# -- fitting ----------------------------------------------------------------


@pytest.mark.parametrize("method", ["gausslq", "mle"])
def test_fit_spot_recovers_position(method):
    frame = np.full((64, 64), 8.0, dtype=np.float32)
    _gaussian_spot(frame, 31.4, 20.7, amplitude=400.0, sigma=1.3)
    fit = fit_spot(frame, 31, 21, roi=9, method=method)
    assert fit["x"] == pytest.approx(20.7, abs=0.4)
    assert fit["y"] == pytest.approx(31.4, abs=0.4)
    assert fit["intensity"] > 0
    assert fit["sigma_x"] > 0


def test_fit_spot_out_of_bounds_raises():
    frame = np.zeros((16, 16), dtype=np.float32)
    with pytest.raises(ValueError):
        fit_spot(frame, 0, 0, roi=7)


def test_fit_spot_dark_roi_raises():
    frame = np.full((16, 16), 5.0, dtype=np.float32)
    with pytest.raises(ValueError):
        fit_spot(frame, 8, 8, roi=7)  # flat ROI -> zero after background subtraction


def test_fit_spots_skips_failures():
    frame = _synthetic_frame()
    coords = np.array([[16, 20], [0, 0], [40, 48]])  # middle one is out of bounds
    fits = fit_spots(frame, coords, roi=7)
    assert len(fits) == 2


def test_fit_spot_unknown_method():
    frame = _synthetic_frame()
    with pytest.raises(ValueError):
        fit_spot(frame, 16, 20, method="bogus")


# -- stack localization -----------------------------------------------------


def test_iter_frames_flattens_leading_dims():
    stack = np.zeros((2, 3, 8, 8))
    frames = list(iter_frames(stack))
    assert len(frames) == 6
    assert frames[0][1].shape == (8, 8)


def test_iter_frames_single_frame():
    frames = list(iter_frames(np.zeros((8, 8))))
    assert len(frames) == 1
    assert frames[0][0] == 0


def test_localize_stack_recovers_nm_positions():
    emitters = [(16.0, 20.0), (40.0, 48.0)]
    frame = _synthetic_frame(emitters=emitters)
    stack = np.stack([frame, frame])  # two identical frames
    locs = localize_stack(
        stack, threshold=20.0, roi=9, sigma=1.0, method="gausslq", pixel_size_nm=100.0
    )
    assert len(locs) == 4  # 2 emitters x 2 frames
    # nm conversion: first emitter x ~= 20 px * 100 nm
    xs = np.sort(np.unique(np.round(locs.x_nm / 100.0)))
    assert set(xs).issuperset({20.0, 48.0})
    assert set(locs.frame.tolist()) == {0, 1}


def test_localize_stack_blank_returns_empty():
    stack = np.full((3, 32, 32), 10.0, dtype=np.float32)
    locs = localize_stack(stack, threshold=50.0, pixel_size_nm=100.0)
    assert len(locs) == 0


# -- reconstructor ----------------------------------------------------------


class _FakeDataObj:
    def __init__(self, data, name="blink"):
        self._data = np.asarray(data)
        self.name = name
        self.dataLoaded = False

    @property
    def data(self):
        return self._data

    def checkAndLoadData(self):
        self.dataLoaded = True

    def checkAndUnloadData(self):
        self.dataLoaded = False


def _recorded_source(stack, layout, *, axis_labels=None, axis_scales=None, unit="px"):
    """A DataObj-like source that publishes a producer-authored layout."""
    from imswitch.improcess.model.acquisition_layout_resolver import (
        ResolvedAcquisitionLayout,
    )

    data_obj = _FakeDataObj(stack)
    data_obj.acquisition_layout = ResolvedAcquisitionLayout(
        layout=layout, source="explicit", confidence="certain"
    )
    data_obj.axis_labels = axis_labels or []
    data_obj.axis_scales = axis_scales or []
    data_obj.scale_unit = unit
    return data_obj


def _two_condition_layout(frames_per_condition=3):
    from imswitch.imcommon.model.acquisition_layout import (
        ACQUISITION_LAYOUT_SCHEMA,
        PAYLOAD_DETECTOR_FRAME_STREAM,
        AcquisitionLayout,
        AcquisitionLoop,
    )

    return AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="Cam",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(
            AcquisitionLoop("linestep", "condition", 2, labels=("A", "B")),
            AcquisitionLoop("time", "time", frames_per_condition),
        ),
        scan_source="ScanControllerAdvanced",
    )


def test_localizer_refuses_to_flatten_a_condition_loop_into_time():
    """Two illumination states are not six timepoints of one blinking trace."""
    frame = _synthetic_frame(emitters=[(16.0, 20.0)])
    data_obj = _recorded_source(np.stack([frame] * 6), _two_condition_layout())

    with pytest.raises(ValueError, match="not a plain frame stream"):
        SmlmLocalizer().process(
            data_obj, {"threshold": 20.0, "roi": 9, "pixel_size_nm": 100.0}
        )


def test_localizer_accepts_an_explicit_loop_selection():
    frame = _synthetic_frame(emitters=[(16.0, 20.0)])
    data_obj = _recorded_source(np.stack([frame] * 6), _two_condition_layout())

    result = SmlmLocalizer().process(
        data_obj,
        {
            "threshold": 20.0,
            "roi": 9,
            "pixel_size_nm": 100.0,
            "loop_selection": {"linestep": 1},
        },
    )

    # Only condition B's three frames were localized, and the choice is recorded.
    assert set(result.locs["frame"].tolist()) <= {0, 1, 2}
    assert result.metadata["loop_selection"] == {"linestep": 1}


def test_localizer_accepts_a_recorded_plain_frame_stream():
    """A time-only recording needs no selection."""
    from imswitch.imcommon.model.acquisition_layout import (
        ACQUISITION_LAYOUT_SCHEMA,
        PAYLOAD_DETECTOR_FRAME_STREAM,
        AcquisitionLayout,
        AcquisitionLoop,
    )

    frame = _synthetic_frame(emitters=[(16.0, 20.0)])
    layout = AcquisitionLayout(
        schema=ACQUISITION_LAYOUT_SCHEMA,
        payload_kind=PAYLOAD_DETECTOR_FRAME_STREAM,
        detector="Cam",
        storage_axes=("frame", "detector_y", "detector_x"),
        event_loops=(AcquisitionLoop("time", "time", 3),),
    )

    result = SmlmLocalizer().process(
        _recorded_source(np.stack([frame] * 3), layout),
        {"threshold": 20.0, "roi": 9, "pixel_size_nm": 100.0},
    )

    assert result.count > 0
    assert "loop_selection" not in result.metadata


def test_source_calibration_outranks_the_widget_pixel_size():
    """The file knows its camera pitch; the manual entry is recorded, not used."""
    frame = _synthetic_frame(emitters=[(16.0, 20.0)])
    data_obj = _FakeDataObj(np.stack([frame] * 2))
    data_obj.axis_labels = ["T", "Y", "X"]
    data_obj.axis_scales = [1.0, 0.065, 0.065]
    data_obj.scale_unit = "um"

    result = SmlmLocalizer().process(
        data_obj, {"threshold": 20.0, "roi": 9, "pixel_size_nm": 100.0}
    )

    assert result.pixel_size_nm == pytest.approx(65.0)
    assert result.metadata["pixel_size_source"] == "source"
    assert result.metadata["pixel_size_nm_manual"] == pytest.approx(100.0)


def test_manual_pixel_size_is_used_and_recorded_when_the_source_has_none():
    frame = _synthetic_frame(emitters=[(16.0, 20.0)])

    result = SmlmLocalizer().process(
        _FakeDataObj(np.stack([frame] * 2)),
        {"threshold": 20.0, "roi": 9, "pixel_size_nm": 100.0},
    )

    assert result.pixel_size_nm == pytest.approx(100.0)
    assert result.metadata["pixel_size_source"] == "manual"


def test_localizer_process_produces_localization_result():
    frame = _synthetic_frame(emitters=[(16.0, 20.0), (40.0, 48.0)])
    stack = np.stack([frame, frame, frame])
    data_obj = _FakeDataObj(stack)

    localizer = SmlmLocalizer()
    params = {"threshold": 20.0, "roi": 9, "sigma": 1.0, "method": "gausslq",
              "pixel_size_nm": 100.0}
    result = localizer.process(data_obj, params)

    assert isinstance(result, LocalizationResult)
    assert result.count == 6
    assert result.pixel_size_nm == pytest.approx(100.0)
    assert result.dims == "2D"
    assert result.source_shape == (64, 64)
    assert result.metadata["fit_method"] == "gausslq"


def test_localizer_registers_in_registry():
    from imswitch.improcess.reconstructors import (
        available_reconstructor_ids,
        register_default_reconstructors,
    )
    from imswitch.improcess.reconstructors.registry import PluginRegistry

    assert "smlm-localizer" in available_reconstructor_ids()
    registry = PluginRegistry()
    register_default_reconstructors(registry, ["smlm-localizer"])
    plugin = registry.get_reconstructor("smlm-localizer")
    assert plugin.id == "smlm-localizer"
    assert plugin.name == "SMLM localizer"


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def test_localizer_params_widget_get_values(qapp):
    localizer = SmlmLocalizer()
    widget = localizer.make_param_widget(None)
    values = widget.get_values()
    assert values["method"] == "gausslq"
    assert values["roi"] == 7
    # No pixel size unless one was chosen. Sending the spinbox's default on
    # every run meant the recording's own calibration was never consulted and
    # an anisotropic one could not object, because the localizer could not
    # tell a default from a decision.
    assert values["pixel_size_nm"] is None
    assert localizer.make_metadata_dialog(None) is None


def test_localizer_params_widget_sends_a_chosen_pixel_size(qapp):
    widget = SmlmLocalizer().make_param_widget(None)
    calibration = widget.p.param("Calibration")

    calibration.param("Pixel size").setValue("Enter below")
    calibration.param("Pixel size (nm)").setValue(65.0)

    assert widget.get_values()["pixel_size_nm"] == pytest.approx(65.0)
