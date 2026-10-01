"""Focus-lock and autofocus timing fields: units, validation, derivation."""

import pytest

from imswitch.imcontrol.model.SetupInfo import AutofocusInfo, FocusLockInfo


def _focus_lock(**overrides):
    fields = dict(camera='FocusCam', positioner='PiezoZ', updateFreq=10,
                  frameCropx=0, frameCropy=0, frameCropw=64, frameCroph=64,
                  swapImageAxes=False, piKp=0.1, piKi=0.01)
    fields.update(overrides)
    return FocusLockInfo(**fields)


def test_update_freq_is_a_positive_rate():
    with pytest.raises(ValueError, match='positive rate in hertz'):
        _focus_lock(updateFreq=0)


def test_the_reacquire_deadline_covers_the_sample_window():
    """At 5 Hz a five-sample window needs a second by itself; a 1 s deadline
    could never be met, and the lock gave up on every tile."""
    info = _focus_lock(updateFreq=5, reacquireSamples=5, reacquireTimeoutS=1.0)
    assert info.reacquireDeadlineS() == pytest.approx(1.0 + 5 / 5)
    fast = _focus_lock(updateFreq=100, reacquireSamples=5, reacquireTimeoutS=1.0)
    assert fast.reacquireDeadlineS() == pytest.approx(1.05)


def test_reacquire_samples_and_timeout_are_validated():
    with pytest.raises(ValueError, match='at least 2'):
        _focus_lock(reacquireSamples=1)
    with pytest.raises(ValueError, match='not be negative'):
        _focus_lock(reacquireTimeoutS=-1)


def test_autofocus_settle_is_declared_with_a_default():
    info = AutofocusInfo(camera='Cam', positioner='PiezoZ', updateFreq=10,
                         frameCropx=0, frameCropy=0, frameCropw=64, frameCroph=64)
    assert info.settleTimeMs == 150.0
