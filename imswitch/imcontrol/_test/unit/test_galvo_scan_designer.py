"""Coverage for GalvoScanDesigner.make_signal.

The legacy test_galvo_jerk_limit.py is skipped (written against a non-existent
API), so make_signal had NO passing coverage -- and it crashes with an opaque
scipy error when vel_max/acc_max are unrealistically large for the scan velocity,
which is exactly what happens on the 1e6 fallback used when a scanning positioner
omits vel_max/acc_max. See memory galvo-designer-bpoly-crash.
"""
import numpy as np
import pytest

from imswitch.imcontrol.model.SetupInfo import SetupInfo
from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import GalvoScanDesigner


def _setup(velacc):
    return SetupInfo.from_json(
        """
    {"positioners": {
      "X": {"analogChannel":0,"digitalLine":null,"managerName":"NidaqPositionerManager",
            "managerProperties":{"conversionFactor":1.0,"minVolt":-10,"maxVolt":10 %s},
            "axes":["X"],"forScanning":true,"forPositioning":true},
      "Y": {"analogChannel":1,"digitalLine":null,"managerName":"NidaqPositionerManager",
            "managerProperties":{"conversionFactor":1.0,"minVolt":-10,"maxVolt":10 %s},
            "axes":["Y"],"forScanning":true,"forPositioning":true}},
     "scan": {"scanDesigner":"GalvoScanDesigner","scanDesignerParams":{},
              "TTLCycleDesigner":"BetaTTLCycleDesigner","TTLCycleDesignerParams":{},
              "sampleRate":100000}}
    """ % (velacc, velacc),
        infer_missing=True,
    )


def _params(length=10, step=1.0, seq_time=0.001):
    return {
        "target_device": ["X", "Y"],
        "axis_length": [length, length],
        "axis_step_size": [step, step],
        "axis_centerpos": [0, 0],
        "axis_startpos": [[0], [0]],
        "sequence_time": seq_time,
        "phase_delay": 0,
        "d3step_delay": 100,
    }


def test_make_signal_succeeds_with_realistic_vel_acc():
    """Realistic galvo vel/acc (as example_sted configures) -> a valid signal."""
    setup = _setup(',"vel_max":0.5,"acc_max":0.05')
    sig, positions, scan_info = GalvoScanDesigner().make_signal(_params(), setup)

    assert set(sig.keys()) == {"X", "Y"}
    n = {len(v) for v in sig.values()}
    assert len(n) == 1 and n.pop() > 0
    assert list(positions) == list(scan_info["img_dims"])
    assert np.all(np.isfinite(sig["X"]))


def test_missing_vel_acc_raises_config_error_not_opaque_scipy():
    """No vel_max/acc_max configured (the old silent 1e6 fallback that crashed
    inside scipy BPoly) must now fail early with an actionable config error."""
    setup = _setup("")  # no vel_max/acc_max
    with pytest.raises(ValueError, match="requires 'vel_max'"):
        GalvoScanDesigner().make_signal(_params(), setup)


def test_present_but_unrealistic_vel_acc_raises_spline_guard():
    """vel_max/acc_max present but far too large for the scan velocity still
    collapses the spline knots; the guard must convert the opaque scipy error
    into an actionable one instead of letting BPoly raise 'x must be strictly
    increasing'."""
    setup = _setup(',"vel_max":5000,"acc_max":50000')
    with pytest.raises(ValueError, match="non-increasing time knots"):
        GalvoScanDesigner().make_signal(_params(), setup)
