"""Qt-free focus-lock trust module: features, verdict, learning, state
machine, relock planning, persistence; plus the simulation that decided
centroid-vs-correlation and the disturbance confusion table (smaller sample
sizes than the report's run, same conclusions).
"""
import json
import math
import time

import numpy as np
import pytest
import scipy.ndimage as ndi

from imswitch.imcontrol.model import focus_lock_quality as flq

H = W = 300
_yy, _xx = np.mgrid[0:H, 0:W]


def spot(row, col=150, amp=1000.0, sigma=12.0):
    return amp * np.exp(-((_yy - row) ** 2 + (_xx - col) ** 2) / (2 * sigma ** 2))


def raw(img, rng, noise=10.0):
    return np.clip(100 + img + rng.normal(0, noise, img.shape), 0, 65535).astype(np.uint16)


def centroid_existing(gf):
    """The controller's estimator (ProcessDataThread.update / _spotCentre)."""
    r, c = np.unravel_index(np.argmax(gf), gf.shape)
    r0, r1, c0, c1 = max(0, r - 50), min(H, r + 50), max(0, c - 50), min(W, c + 50)
    w = gf[r0:r1, c0:c1] - np.median(gf)
    w = np.clip(w - 0.1 * w.max(), 0, None)
    p = w.sum(1)
    return float((p * np.arange(p.size)).sum() / p.sum() + r0), int(r), int(c)


def features(img_raw, reference=None):
    gf = ndi.gaussian_filter(img_raw.astype(np.float32), 7)
    _, r, c = centroid_existing(gf)
    return flq.measure_frame(gf, img_raw, r, c, reference)


@pytest.fixture
def rng():
    return np.random.default_rng(3)


@pytest.fixture
def reference(rng):
    return flq.make_reference(features(raw(spot(150.0), rng, noise=0.0)))


# --------------------------------------------------------------- features

def test_correlation_recovers_a_subpixel_shift(rng, reference):
    for shift in (-7.3, -0.4, 0.0, 0.25, 3.6):
        f = features(raw(spot(150.0 + shift), rng), reference)
        assert f.correlationPosition == pytest.approx(150.0 + shift, abs=0.08)
        assert f.correlation > 0.99


def test_saturation_clipping_and_signature(rng, reference):
    img = raw(spot(150.0, amp=200000), rng)
    f = features(img, reference)
    assert f.saturatedFraction > 0.02
    edge = features(raw(spot(2.0), rng), reference)
    assert edge.clipped
    a = raw(spot(150.0), rng)
    assert flq.frame_signature(a) == flq.frame_signature(a.copy())
    assert flq.frame_signature(a) != flq.frame_signature(raw(spot(150.0), rng))


# ----------------------------------------------- accuracy: centroid vs NCC

def test_correlation_is_not_more_accurate_than_the_centroid(rng, reference):
    """The decision: the centroid stays the control signal; the correlation
    serves confidence and relock. (Report: noise 10 -> centroid 0.009 px RMS,
    correlation 0.012 px; noise 60 -> 0.055 vs 0.076 px.)"""
    rows = 150 + rng.uniform(-15, 15, 60)
    ce, co = [], []
    for row in rows:
        img = raw(spot(row), rng, noise=30)
        gf = ndi.gaussian_filter(img.astype(np.float32), 7)
        c, r_, c_ = centroid_existing(gf)
        f = flq.measure_frame(gf, img, r_, c_, reference)
        ce.append(c - row)
        co.append(f.correlationPosition - row)
    rms_c = float(np.sqrt(np.mean(np.square(ce))))
    rms_o = float(np.sqrt(np.mean(np.square(co))))
    assert rms_c < 0.05 and rms_o < 0.08
    assert rms_o >= 0.9 * rms_c, 'if this fails, revisit the decision'


def test_trust_features_are_cheap(rng, reference):
    img = raw(spot(150.0), rng)
    gf = ndi.gaussian_filter(img.astype(np.float32), 7)
    _, r, c = centroid_existing(gf)
    start = time.perf_counter()
    for _ in range(50):
        flq.measure_frame(gf, img, r, c, reference)
    per_frame = (time.perf_counter() - start) / 50
    assert per_frame < 0.02, f'{per_frame * 1e3:.1f} ms per frame'


# ------------------------------------------------------- confusion table

def _learned(rng, reference):
    learner = flq.TrustLearner(windowS=0.0, minSamples=30)
    learner.start(0.0)
    prev, result = None, None
    for _ in range(35):
        f = features(raw(spot(150.0 + rng.normal(0, 0.05)), rng), reference)
        innov = None if prev is None else abs(f.centroid - prev)
        result = learner.add(f, reference, innov, 1.0, flq.TrustThresholds())
        prev = f.centroid
    assert result is not None and result.learned
    return result


def _occluded(cut, factor, row=150.0):
    s = spot(row)
    s[:int(cut), :] *= factor
    return s


CASES = [
    # name, image, expected flagged (learned thresholds)
    ('noise x3', lambda: spot(150.0), 30, False),
    ('intensity drop to 30%', lambda: spot(150.0, amp=300), 10, True),
    ('intensity drop to 60%', lambda: spot(150.0, amp=600), 10, False),
    ('occlusion half x0.2', lambda: _occluded(150, 0.2), 10, True),
    ('occlusion half x0.5', lambda: _occluded(150, 0.5), 10, True),
    ('occlusion quarter x0.2', lambda: _occluded(142, 0.2), 10, True),
    ('shape change sigma 18', lambda: spot(150.0, sigma=18), 10, True),
    ('second spot outside window', lambda: spot(150.0) + spot(260.0, amp=800), 10, True),
    ('second spot inside window', lambda: spot(150.0) + spot(185.0, amp=600), 10, True),
    ('real small defocus 0.5 px', lambda: spot(150.5), 10, False),
    ('real large defocus step 15 px', lambda: spot(165.0, sigma=15), 10, True),
    ('spot gone', lambda: spot(150.0, amp=0), 10, True),
]


@pytest.mark.parametrize('name,make,noise,expected', CASES, ids=[c[0] for c in CASES])
def test_confusion_case(rng, reference, name, make, noise, expected):
    thresholds = _learned(rng, reference)
    flagged = 0
    for _ in range(4):
        f = features(raw(make(), rng, noise), reference)
        v = flq.assess_frame(f, reference, thresholds, flq.FrameContext(previousSignal=150.0))
        flagged += not v.trusted
    assert flagged == (4 if expected else 0), name


def test_persistent_occlusion_stays_flagged_after_the_jump(rng, reference):
    """The first occluded frame is a jump; the following ones must still fail
    (agreement), or a dirt particle would be locked onto after N frames."""
    thresholds = _learned(rng, reference)
    prev = 150.0
    for i in range(5):
        f = features(raw(_occluded(150, 0.5), rng), reference)
        v = flq.assess_frame(f, reference, thresholds, flq.FrameContext(previousSignal=prev),
                             strict=True)
        prev = f.centroid
        assert not v.trusted
        if i:
            assert 'agreement' in v.reasons


def test_clean_false_flag_rate_is_zero(rng, reference):
    thresholds = _learned(rng, reference)
    prev, flagged = 150.0, 0
    for _ in range(150):
        f = features(raw(spot(150.0 + rng.normal(0, 0.05)), rng), reference)
        flagged += not flq.assess_frame(f, reference, thresholds,
                                        flq.FrameContext(previousSignal=prev)).trusted
        prev = f.centroid
    assert flagged == 0


# ------------------------------------------------------------- verdict

def _f(**kw):
    base = dict(centroid=150.0, peakAboveBackground=500.0, noise=5.0, intensity=1000.0,
                width=10.0, secondPeakRatio=0.1, saturatedFraction=0.0, clipped=False,
                signature=1, correlation=0.999, correlationPosition=150.0,
                peakPosition=150.0)
    base.update(kw)
    return flq.FrameFeatures(**base)


REF = flq.SpotReference(profile=np.ones(10, np.float32), start=145, colLo=0, colHi=10,
                        centroid=150.0, intensity=1000.0, width=10.0, peak=500.0)


@pytest.mark.parametrize('kw,reason', [
    (dict(peakAboveBackground=20.0), 'signal'),
    (dict(intensity=300.0), 'intensity'),
    (dict(correlation=0.5), 'correlation'),
    (dict(width=20.0), 'width'),
    (dict(secondPeakRatio=0.9), 'second-peak'),
    (dict(saturatedFraction=0.5), 'saturation'),
    (dict(clipped=True), 'clipped'),
    (dict(centroid=160.0, correlationPosition=160.0, peakPosition=160.0), 'jump'),
    (dict(correlationPosition=153.0), 'agreement'),
])
def test_each_test_reports_its_own_reason(kw, reason):
    v = flq.assess_frame(_f(**kw), REF, flq.TrustThresholds(),
                         flq.FrameContext(previousSignal=150.0))
    assert not v.trusted and reason in v.reasons
    assert v.confidence <= 0.5


def test_expected_shift_from_the_last_correction_is_not_a_jump():
    ctx = flq.FrameContext(previousSignal=150.0, expectedShiftPx=8.0)
    f = _f(centroid=158.0, correlationPosition=158.0, peakPosition=158.0)
    assert flq.assess_frame(f, REF, flq.TrustThresholds(), ctx).trusted


def test_stale_frames_are_skipped_then_fail():
    t = flq.TrustThresholds()
    v = flq.assess_frame(_f(), REF, t, flq.FrameContext(staleCount=1))
    assert v.trusted and v.skip
    v = flq.assess_frame(_f(), REF, t, flq.FrameContext(staleCount=t.staleFramesAllowed + 1))
    assert not v.trusted and 'stale' in v.reasons


def test_strict_thresholds_are_stricter():
    t = flq.TrustThresholds()
    s = t.strict()
    assert s.minCorrelation > t.minCorrelation and s.maxJumpPx < t.maxJumpPx
    assert s.intensityBand[0] > t.intensityBand[0] and s.intensityBand[1] < t.intensityBand[1]
    f = _f(correlation=0.86)
    assert flq.assess_frame(f, REF, t, flq.FrameContext()).trusted
    assert not flq.assess_frame(f, REF, t, flq.FrameContext(), strict=True).trusted


# ----------------------------------------------------------- supervisor

def _v(ok, skip=False):
    return flq.FrameVerdict(ok, () if ok else ('x',), {}, 1.0 if ok else 0.0, skip)


def test_supervisor_hysteresis_and_timeout():
    sup = flq.LockSupervisor(flq.SupervisorSettings(goodFramesToResume=3, holdTimeoutS=1.0))
    assert sup.frame(_v(True), _v(True), 0.0) == 'apply'
    assert sup.frame(_v(False), _v(False), 0.1) == 'hold'
    # trusted by the entry thresholds but not the exit ones: still holding
    assert sup.frame(_v(True), _v(False), 0.2) == 'hold'
    assert sup.frame(_v(True), _v(True), 0.3) == 'hold'
    assert sup.frame(_v(True), _v(True), 0.4) == 'hold'
    assert sup.frame(_v(True), _v(True), 0.5) == 'resume'
    assert sup.state == flq.LOCKED
    sup.frame(_v(False), _v(False), 1.0)
    assert sup.frame(_v(False), _v(False), 2.1) == 'lost'


# --------------------------------------------------------------- relock

def test_relock_step_is_clamped_and_needs_calibration():
    limits = flq.RelockLimits()
    step = flq.plan_relock_step(50.0, 10.0, 0.0, 0.99, limits)
    assert step.stepUm == pytest.approx(0.5) and step.clamped
    step = flq.plan_relock_step(50.0, 10.0, 2.8, 0.99, limits)
    assert step.stepUm == pytest.approx(0.2)
    step = flq.plan_relock_step(50.0, 10.0, 3.0, 0.99, limits)
    assert step.stepUm == 0.0 and step.reason == 'excursion limit reached'
    assert flq.plan_relock_step(50.0, None, 0.0, 0.99, limits).reason == 'no valid calibration'
    assert flq.plan_relock_step(0.5, None, 0.0, 0.99, limits).done


def test_low_confidence_never_increases_the_excursion():
    limits = flq.RelockLimits()
    assert flq.plan_relock_step(5.0, 10.0, 0.0, 0.3, limits).stepUm == 0.0
    # back toward the last good Z is allowed even on low confidence
    step = flq.plan_relock_step(-5.0, 10.0, 1.0, 0.3, limits)
    assert step.stepUm == pytest.approx(-0.5)
    assert flq.plan_relock_step(5.0, 10.0, 0.0, float('nan'), limits).stepUm == 0.0


def test_search_offsets_stay_inside_the_budget():
    limits = flq.RelockLimits(searchEnabled=True, searchRangeUm=10.0, maxExcursionUm=1.0,
                              searchStepUm=0.4)
    offsets = flq.search_offsets(limits)
    assert offsets and max(abs(o) for o in offsets) <= 1.0


@pytest.mark.parametrize('kw', [dict(maxStepUm=0), dict(maxExcursionUm=-1),
                                dict(maxStepUm=5, maxExcursionUm=1),
                                dict(timeoutS=float('nan'))])
def test_invalid_limits_refused(kw):
    with pytest.raises(ValueError):
        flq.RelockLimits(**kw).validate()


# ---------------------------------------------------------- persistence

def test_state_round_trip_is_atomic_and_per_actuator(tmp_path, reference):
    path = tmp_path / 'state.json'
    engine = flq.FocusLockEngine()
    engine.reference, engine.pxPerUm, engine.setPoint = reference, 12.5, 151.2
    flq.save_lock_state('setupA', 'Piezo', engine.toState(), path=path)
    flq.save_lock_state('setupA', 'StageZ', {'setPoint': 1.0}, path=path)
    flq.save_lock_state('setupB', 'Piezo', {'setPoint': 2.0}, path=path)
    assert not list(tmp_path.glob('.focuslock_*')), 'no temporary file left behind'
    stored = flq.load_lock_state('setupA', 'Piezo', path=path)
    other = flq.FocusLockEngine()
    other.applyStoredLock(stored)
    assert other.setPoint == pytest.approx(151.2) and other.pxPerUm == 12.5
    assert np.allclose(other.reference.profile, reference.profile)
    assert flq.load_lock_state('setupA', 'Nope', path=path) is None
    data = json.loads(path.read_text())
    assert set(data) == {'setupA', 'setupB'}


def test_corrupt_or_foreign_state_is_refused(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({'s': {'Z': {'version': 99, 'setPoint': 1}}}))
    with pytest.raises(ValueError):
        flq.load_lock_state('s', 'Z', path=path)
    with pytest.raises(ValueError):
        flq.SpotReference.from_dict({'profile': [1, float('nan'), 1, 1, 1, 1], 'start': 0,
                                     'colLo': 0, 'colHi': 1, 'centroid': 1, 'intensity': 1,
                                     'width': 1, 'peak': 1})
    engine = flq.FocusLockEngine()
    with pytest.raises(ValueError, match='calibration'):
        engine.applyStoredLock({'setPoint': 1.0, 'pxPerUm': None})
    assert math.isfinite(flq.TrustThresholds().minSnr)
