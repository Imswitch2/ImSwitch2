"""The FLIM worker end to end on the mock card.

For the first time the whole path runs without hardware: the scan designer's
line clock becomes edges on the mock card, the detector builds its markers
and ``Flim`` on it, the worker reads the frame, orients it, subtracts the
background, fits every pixel, and publishes live products -- in forward and
in reverse (conditional-filter) mode. The tolerances pin what the three
fitters do on a realistic decay: with a 350 ps IRF the moment and the phasor
reference the IRF *peak*, not its centre, and read low; a narrow IRF shows
them recovering the lifetime. ``exp1`` per pixel needs thousands of photons;
its global fit on the aggregated decay is the trustworthy one.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcontrol.model.SetupInfo import TimeTaggerInfo
from imswitch.imcontrol.model.interfaces.timetagger_mock import (
    SAMPLE_PRESETS,
    MockSample,
    MockTimeTaggerApi,
    SignalModel,
)
from imswitch.imcontrol.model.managers._scan_execution import PARTICIPANTS_KEY
from imswitch.imcontrol.model.managers.TimeTaggerManager import TimeTaggerManager
from imswitch.imcontrol.model.managers.detectors.SwabianTimeTaggerManager import (
    SwabianTimeTaggerManager,
    _TTFlimWorker,
)
from imswitch.imcontrol.model.timeresolved import (
    GateSpec,
    LiveProducts,
    TimeResolvedScanConfig,
)

pytestmark = pytest.mark.nohardware

SAMPLE_RATE = 100_000  # Hz, the mock setups' scan.sampleRate
DWELL_SAMPLES = 10     # 100 us per pixel
FLYBACK_SAMPLES = 50
NX, NY = 16, 8


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _Nidaq:
    isSimulated = True

    def __init__(self):
        self.sigScanBuilt = _Signal()
        self.sigScanStarted = _Signal()
        self.sigScanDone = _Signal()
        self.scanBuildFailures = []

    def reportScanBuildFailure(self, stage, error):
        self.scanBuildFailures.append((stage, error))


class _DetectorInfo:
    forAcquisition = True
    forFocusLock = False

    def __init__(self, props):
        self.managerProperties = props


def _ttl_dict(ny=NY, nx=NX):
    """The designer's line clock: one 10-sample pulse per line, with flyback."""
    line = np.zeros(nx * DWELL_SAMPLES + FLYBACK_SAMPLES, dtype=bool)
    line[:10] = True
    return {"line_clock": np.tile(line, ny)}


def _scan_info(name, ny=NY, nx=NX):
    return {
        PARTICIPANTS_KEY: [name],
        "img_dims": [nx, ny],
        "img_axes_phys": ["x", "y"],
        "pixel_sizes": [1.0, 1.0],
        "dwell_time": DWELL_SAMPLES / SAMPLE_RATE,
        "tot_scan_time_s": ny * (nx * DWELL_SAMPLES + FLYBACK_SAMPLES) / SAMPLE_RATE,
    }


def _rig(*, sample="uniform", irf_fwhm_ps=350.0, filter_on=False, faults=None,
         detector_props=None, model_kwargs=None):
    """A mock card, a fake NI-DAQ and a FLIM detector wired together."""
    nidaq = _Nidaq()
    info = TimeTaggerInfo(
        simulation=True, photonsChannel=-1, photonsTriggerV=-0.25,
        filterSyncByPhotons=filter_on, mockSample=sample, mockFaults=faults or {},
    )
    model = SignalModel.from_info(info)
    model.irf_fwhm_ps = irf_fwhm_ps
    for key, value in (model_kwargs or {}).items():
        setattr(model, key, value)
    api = MockTimeTaggerApi(model=model)
    setup = SimpleNamespace(scan=SimpleNamespace(sampleRate=SAMPLE_RATE))
    card = TimeTaggerManager(info, setup, nidaq, api=api)
    props = {"laser_rep_rate_mhz": 80.0, "min_counts_per_pixel": 20}
    props.update(detector_props or {})
    det = SwabianTimeTaggerManager(
        _DetectorInfo(props), "FLIM", nidaq, timeTaggerManager=card,
    )
    return nidaq, card, det


def _run_scan(nidaq, det, ny=NY, nx=NX):
    """Build the scan, run the worker to its final frame, return the frames."""
    frames = []
    live = []
    det.sigTimeResolvedProducts.connect(live.append)
    nidaq.sigScanBuilt.emit(_scan_info(det.name, ny, nx), {"TTLCycleSignalsDict": _ttl_dict(ny, nx)}, [])
    assert det._preparedScanGeneration is not None, nidaq.scanBuildFailures
    generation = det._preparedScanGeneration
    det._activeScanGeneration = generation
    worker = _TTFlimWorker(det, generation)
    worker.sigFrameReady.connect(lambda *args: frames.append(args))
    worker.sigFrameReady.connect(det._on_frame_ready)
    worker.signal_done()  # one read, then the final frame
    worker.run()
    return frames, live


def _lifetimes(frames):
    intensity, lifetime_s, is_final, decay, t_axis_ns, tau_ns, _gen, live = frames[-1]
    assert is_final
    return intensity, lifetime_s * 1e9, decay, t_axis_ns, tau_ns, live


# --------------------------------------------------------------------------- #
# Forward mode                                                                 #
# --------------------------------------------------------------------------- #


def test_forward_scan_images_the_sample_and_publishes_live_products():
    nidaq, card, det = _rig(sample="uniform", detector_props={"fit_method": "moment"})
    frames, live = _run_scan(nidaq, det)

    intensity, lifetime_ns, decay, t_axis_ns, tau_ns, last_live = _lifetimes(frames)
    assert intensity.shape == (NY, NX)
    # 1 Mcps for 100 us, plus 2 % afterpulsing and 2 kHz dark: about 102 photons.
    assert 85 < intensity.mean() < 120
    assert np.all(lifetime_ns > 0)
    assert isinstance(last_live, LiveProducts)
    assert last_live.is_final and last_live.tcspc_direction == "forward"
    assert last_live.lifetime_ns.shape == (NY, NX)
    assert last_live.metadata["overflows"] == 0 and last_live.metadata["frame_valid"]
    assert live and live[-1] is last_live, "re-emitted on the manager's signal"
    assert live[-1].frame_index == 0
    assert not card.scanHeld, "released once the final frame landed"
    # The display frame is the lifetime image in ns.
    assert det.getLatestFrame().shape == (1, NY, NX)


@pytest.mark.parametrize("fit_method, tolerance", [("moment", 0.10), ("phasor", 0.10)])
def test_narrow_irf_lets_moment_and_phasor_recover_the_lifetime(fit_method, tolerance):
    nidaq, _, det = _rig(sample="uniform", irf_fwhm_ps=20.0,
                         detector_props={"fit_method": fit_method})
    frames, _ = _run_scan(nidaq, det)
    _, lifetime_ns, _, _, tau_ns, _ = _lifetimes(frames)
    assert np.median(lifetime_ns) == pytest.approx(2.5, rel=tolerance)
    assert tau_ns == pytest.approx(2.5, rel=tolerance)


@pytest.mark.parametrize("fit_method, low, high", [("moment", 0.75, 1.0), ("phasor", 0.65, 1.0)])
def test_wide_irf_biases_moment_and_phasor_low_by_a_bounded_amount(fit_method, low, high):
    """Both reference the IRF *peak*, which sits after its centre: the known
    bias the IRF-aware fit in the follow-up plan removes."""
    nidaq, _, det = _rig(sample="uniform", irf_fwhm_ps=350.0,
                         detector_props={"fit_method": fit_method})
    frames, _ = _run_scan(nidaq, det)
    _, lifetime_ns, _, _, _, _ = _lifetimes(frames)
    ratio = np.median(lifetime_ns) / 2.5
    assert low < ratio <= high


def test_global_exp1_fit_recovers_the_lifetime_with_background_subtracted():
    # 2 kHz dark + 2 % afterpulsing on 1 Mcps = 22 kHz of flat background.
    nidaq, _, det = _rig(sample="uniform", detector_props={
        "fit_method": "exp1", "background_rate_hz": 22_000.0,
    })
    frames, _ = _run_scan(nidaq, det)
    _, _, _, _, tau_ns, live = _lifetimes(frames)
    assert tau_ns == pytest.approx(2.5, rel=0.08)
    assert live.background_per_bin > 0


def test_beads_sample_separates_the_two_lifetime_populations():
    nidaq, _, det = _rig(sample="beads_two_lifetimes",
                         detector_props={"fit_method": "moment", "min_counts_per_pixel": 50})
    frames, _ = _run_scan(nidaq, det, ny=24, nx=32)
    intensity, lifetime_ns, *_ = _lifetimes(frames)
    truth_tau = SAMPLE_PRESETS["beads_two_lifetimes"]().lifetime_map(24, 32)
    bright = intensity > np.percentile(intensity, 80)
    short = bright & (truth_tau < 2.0)
    long = bright & (truth_tau > 3.5)
    assert short.any() and long.any()
    assert np.median(lifetime_ns[long]) > 1.5 * np.median(lifetime_ns[short])


def test_gates_requested_by_a_session_arrive_in_live_products():
    nidaq, _, det = _rig(sample="uniform")
    det.configureTimeResolvedProducts(
        TimeResolvedScanConfig(
            gates=(GateSpec("early", 0.0, 1.0, reference="peak"),
                   GateSpec("late", 1.0, 10.0, reference="peak")),
            include_live_products=True,
        ),
        owner="test",
    )
    frames, _ = _run_scan(nidaq, det)
    *_, live = _lifetimes(frames)
    assert set(live.gate_images) == {"early", "late"}
    assert live.gate_images["late"].sum() > live.gate_images["early"].sum()
    products = det.getLastTimeResolvedProducts()
    assert set(products.gate_images) == {"early", "late"}
    assert products.metadata["tcspc_direction"] == "forward"
    assert products.metadata["time_tagger"]["is_mock"]


# --------------------------------------------------------------------------- #
# Reverse mode                                                                 #
# --------------------------------------------------------------------------- #


def test_reverse_mode_swaps_the_flim_slots_and_reads_forward_time():
    nidaq, card, det = _rig(sample="uniform", irf_fwhm_ps=20.0, filter_on=True,
                            detector_props={"fit_method": "moment"})
    assert card.tcspcDirection == "reverse"
    frames, _ = _run_scan(nidaq, det)
    flim = det._flim
    assert flim.start_channel == -1 and flim.click_channel == 2, "photon starts, sync stops"
    assert card.tagger.getInputDelay(-1) == 0, "no photon delay in reverse mode"
    _, lifetime_ns, decay, t_axis_ns, tau_ns, live = _lifetimes(frames)
    assert live.tcspc_direction == "reverse"
    assert np.all(np.diff(t_axis_ns) > 0), "forward, ascending axis"
    # The peak sits where the model puts it (1 ns after the sync), not near
    # T_rep. With a 2.5 ns decay the bins right after a sharp peak differ by
    # less than their Poisson noise, so argmax jitters by a few 32 ps bins.
    assert t_axis_ns[np.argmax(decay)] == pytest.approx(1.0, abs=0.25)
    assert np.median(lifetime_ns) == pytest.approx(2.5, rel=0.10)
    assert tau_ns == pytest.approx(2.5, rel=0.10)


def test_reverse_mode_refuses_a_window_shorter_than_the_period():
    nidaq, card, det = _rig(sample="uniform", filter_on=True,
                            detector_props={"n_bins": 64})
    nidaq.sigScanBuilt.emit(_scan_info(det.name), {"TTLCycleSignalsDict": _ttl_dict()}, [])
    assert det._preparedScanGeneration is None
    stage, error = nidaq.scanBuildFailures[0]
    assert "must span at least one period" in str(error)
    assert not card.scanHeld


def test_reverse_mode_t0_is_a_roll_not_a_delay():
    nidaq, card, det = _rig(sample="uniform", irf_fwhm_ps=20.0, filter_on=True,
                            detector_props={"t0_ps": 1000})
    frames, _ = _run_scan(nidaq, det)
    assert card.tagger.getInputDelay(-1) == 0
    _, _, decay, t_axis_ns, _, live = _lifetimes(frames)
    assert live.metadata["t0_roll_bins"] == round(1000 / 32)
    # The IRF peak (1 ns after the sync) now sits at the start of the axis
    # (argmax jitters by a few bins on the flat top of a slow decay).
    assert t_axis_ns[np.argmax(decay)] < 0.3


def test_forward_slots_with_the_filter_on_give_a_flat_histogram():
    """The symptom tutorial 05 shows: each click measured against the
    previous photon's sync."""
    nidaq, card, det = _rig(sample="uniform", filter_on=True)
    # Force the forward assignment against the card's direction.
    card._info = TimeTaggerInfo(simulation=True, photonsChannel=-1, filterSyncByPhotons=False)
    frames, _ = _run_scan(nidaq, det)
    _, _, decay, _, _, _ = _lifetimes(frames)
    assert decay.max() < 2.0 * decay.mean()


# --------------------------------------------------------------------------- #
# Faults the debugging tutorials rely on                                       #
# --------------------------------------------------------------------------- #


def test_missing_line_clock_gives_an_empty_frame_and_a_warning(caplog):
    nidaq, _, det = _rig(sample="uniform", faults={"missing_line_clock": True})
    frames, _ = _run_scan(nidaq, det)
    intensity, *_ = _lifetimes(frames)
    assert intensity.sum() == 0
    assert "zero photons" in caplog.text


def test_overflows_during_a_scan_mark_the_frame_invalid(caplog):
    # A Time Tagger 20 with an unfiltered 80 MHz sync is far over budget.
    nidaq, card, det = _rig(sample="uniform", faults={"model": "Time Tagger 20"})
    frames, _ = _run_scan(nidaq, det)
    *_, live = _lifetimes(frames)
    assert live.metadata["overflows"] > 0
    assert live.metadata["frame_valid"] is False
    assert "USB overflow" in caplog.text
    # The same scan with the filter on is within budget.
    nidaq, card, det = _rig(sample="uniform", filter_on=True, faults={"model": "Time Tagger 20"})
    frames, _ = _run_scan(nidaq, det)
    *_, live = _lifetimes(frames)
    assert live.metadata["overflows"] == 0


def test_line_delay_fault_shifts_the_image_and_the_delay_setting_cancels_it():
    stripes = MockSample(
        "stripes",
        lambda ny, nx: np.tile(np.where(np.arange(nx) % 8 < 4, 2.0e6, 1e5), (ny, 1)),
        lambda ny, nx: np.full((ny, nx), 2.5),
    )
    # A 3-pixel late line clock.
    delay_ps = 3 * DWELL_SAMPLES * int(1e12 / SAMPLE_RATE)
    nidaq, card, det = _rig(faults={"line_delay_ps": delay_ps},
                            model_kwargs={"sample": stripes})
    frames, _ = _run_scan(nidaq, det)
    shifted, *_ = _lifetimes(frames)
    truth = stripes.rate_map(NY, NX)
    assert np.corrcoef(shifted[0], truth[0])[0, 1] < 0.5, "shifted by three pixels"
    assert np.corrcoef(shifted[0], np.roll(truth[0], -3))[0, 1] > 0.95

    card.setDelay("line_clock", delay_ps)
    frames, _ = _run_scan(nidaq, det)
    aligned, *_ = _lifetimes(frames)
    assert np.corrcoef(aligned[0], truth[0])[0, 1] > 0.95
