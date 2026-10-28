"""GalvoScanDesigner: the fast-axis speed limit, and the scan-time estimate.

Speed: a smoothly swept fast axis moves one step per dwell. The designer built
the turnaround to ``vel_max`` but never compared the sweep itself against it:
on the STED-like mock, 2 µm pixels at 10 µs (twice vel_max) overshot a 100 µm
line to ±250 µm, and only the voltage check stopped it, with a reason that did
not name the cause. ``make_signal`` now refuses such a design with
ScanDesignRefusedError, naming the axis, the limit and a dwell or pixel size
that would pass.

Estimate: the duration of a scan without building it at full size, for the
SimplePointScan panel's time readout (docs/simple-point-scan-plan.md, F3/F4).
Flyback dominates overview frames, so pixels x dwell is off by up to 4x; the
estimate builds the scan with 2 and 3 steps on every slow axis and
extrapolates, and is held here to 1 % of full builds.
"""
import copy
from types import SimpleNamespace

import numpy as np
import pytest

from imswitch.imcommon.framework import SignalInterface
from imswitch.imcontrol.controller.basecontrollers import SuperScanController
from imswitch.imcontrol.controller.controllers.ScanControllerAdvanced import (
    ScanControllerAdvanced,
)
from imswitch.imcontrol.model.errors import ScanDesignRefusedError
from imswitch.imcontrol.model.managers._scan_execution import (
    ScanExecutionCoordinator,
)
from imswitch.imcontrol.model.scan_request import ScanRequestRejectedError
from imswitch.imcontrol.model.signaldesigners.BetaScanDesigner import (
    BetaScanDesigner,
)
from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import (
    GalvoScanDesigner,
)

from .test_galvo_signal_goldens import _params, _setup_sted_like
from .test_mock_scan_simulation_coordinator import _setup_from_user_default
from .test_scan_design_refusal import (
    _Channel,
    _Logger,
    _Widget,
    _facade,
)

# GalvoX on the STED-like mock: vel_max 0.1 µm/µs, 17.44 µm/V.
X_VEL_MAX = 0.1


def _rigLikeSetup():
    """The STED-like mock with its piezo stepped, as example_sted and the
    shipped galvo/APD mock configure it."""
    setup = _setup_sted_like()
    setup.positioners["PiezoZ"].managerProperties["smoothScan"] = False
    return setup


def _xScan(length, step, dwell_s, *, lengthY=1.0, stepY=1.0):
    params = _params(["GalvoX", "GalvoY", "PiezoZ"],
                     [length, lengthY, 1.0], [step, stepY, 1.0])
    params["sequence_time"] = dwell_s
    return params


def _peakSpeedUmPerUs(signalVolts, conversionFactor, sampleRate):
    return float(np.max(np.abs(np.diff(signalVolts)))
                 * conversionFactor * sampleRate / 1e6)


# --------------------------------------------------------------------------
# Speed limit
# --------------------------------------------------------------------------

def test_a_sweep_faster_than_vel_max_is_refused_with_what_would_pass():
    params = _xScan(100.0, 2.0, 10e-6)  # 0.2 µm/µs, twice vel_max
    designer = GalvoScanDesigner()

    reason = designer.scanSpeedRefusal(params, _setup_sted_like())
    assert reason == (
        'Fast axis GalvoX would sweep at 0.2 µm/µs, above its vel_max of '
        '0.1 µm/µs. Use a dwell of at least 20 µs for 2 µm pixels, or pixels '
        'of at most 1 µm at a 10 µs dwell.'
    )
    with pytest.raises(ScanDesignRefusedError) as refused:
        designer.make_signal(params, _setup_sted_like())
    assert refused.value.message == reason


@pytest.mark.parametrize('step, dwell_s', [
    (2.0, 20e-6),   # exactly vel_max
    (1.0, 20e-6),
    (0.1, 20e-6),   # the Advanced widget's defaults
    (0.5, 10e-3),
])
def test_an_accepted_sweep_never_exceeds_vel_max(step, dwell_s):
    """What the rule protects: nothing it lets through drives the fast axis
    faster than its configured limit, turnaround included.

    The turnaround spline rounds the corners of its velocity profile; at a
    sweep of exactly vel_max that peaks 0.25 % above it. Before the rule the
    peak was the sweep speed itself, 2x and more."""
    setup = _setup_sted_like()
    params = _xScan(20.0, step, dwell_s, lengthY=3 * step, stepY=step)

    assert GalvoScanDesigner().scanSpeedRefusal(params, setup) == ''
    signals, _, _ = GalvoScanDesigner().make_signal(params, setup)
    peak = _peakSpeedUmPerUs(signals["GalvoX"], 17.44, setup.scan.sampleRate)
    assert peak <= X_VEL_MAX * 1.005


def test_only_the_fast_axis_is_limited():
    """The slow axis steps between lines; its step size is not a speed."""
    params = _xScan(20.0, 0.5, 10e-6, lengthY=50.0, stepY=5.0)

    assert GalvoScanDesigner().scanSpeedRefusal(params, _setup_sted_like()) == ''


def test_the_first_active_axis_is_the_fast_axis():
    """A collapsed first dim hands the fast axis to the next one."""
    params = _params(["GalvoX", "GalvoY", "PiezoZ"],
                     [1.0, 100.0, 1.0], [1.0, 2.0, 1.0])
    params["sequence_time"] = 10e-6

    reason = GalvoScanDesigner().scanSpeedRefusal(params, _setup_sted_like())
    assert reason.startswith('Fast axis GalvoY would sweep at 0.2 µm/µs')


def test_a_stepped_fast_axis_is_exempt():
    """A piezo or stage holds each position for the dwell; it is not swept."""
    setup = _setup_sted_like()
    setup.positioners["PiezoZ"].managerProperties.update(
        vel_max=1e-6, smoothScan=False)
    params = _params(["PiezoZ", "GalvoX", "GalvoY"],
                     [6.0, 1.0, 1.0], [0.5, 1.0, 1.0], centers=[5.0, 0, 0])
    params["sequence_time"] = 10e-6

    assert GalvoScanDesigner().scanSpeedRefusal(params, setup) == ''
    GalvoScanDesigner().make_signal(params, setup)


def test_a_missing_vel_max_is_left_to_the_missing_limits_error():
    setup = _setup_sted_like()
    del setup.positioners["GalvoX"].managerProperties["vel_max"]
    params = _xScan(100.0, 2.0, 10e-6)

    assert GalvoScanDesigner().scanSpeedRefusal(params, setup) == ''
    with pytest.raises(ValueError, match="requires 'vel_max'"):
        GalvoScanDesigner().make_signal(params, setup)


def test_the_shipped_mock_setup_refuses_the_measured_overshoot():
    """The case measured on galvo_apd_mock_scan_setup.json while planning:
    2 µm at 10 µs used to build and run the X galvo out to ±250 µm."""
    setup = _setup_from_user_default('galvo_apd_mock_scan_setup.json')
    params = {
        'target_device': ['X', 'Y', 'Z'],
        'axis_length': [100.0, 20.0, 1.0],
        'axis_step_size': [2.0, 2.0, 1.0],
        'axis_centerpos': [0.0, 0.0, 0.0],
        'axis_startpos': [[0.0], [0.0], [0.0]],
        'sequence_time': 10e-6,
        'phase_delay': 0,
        'd3step_delay': 0,
    }
    with pytest.raises(ScanDesignRefusedError, match='Fast axis X'):
        GalvoScanDesigner().make_signal(params, setup)


# --------------------------------------------------------------------------
# Through the Advanced controller: a refused start, with the reason
# --------------------------------------------------------------------------

def _advancedController(channel, analog):
    """The Advanced controller on the shipped galvo/APD mock setup, handed
    ``analog`` as if its widget had produced it."""
    setup = _setup_from_user_default('galvo_apd_mock_scan_setup.json')
    ctrl = ScanControllerAdvanced.__new__(ScanControllerAdvanced)
    SignalInterface.__init__(ctrl)
    ctrl.__dict__.update(
        _commChannel=channel,
        _setupInfo=setup,
        # The positions a scan design starts from.
        _master=SimpleNamespace(positionersManager={
            axis: SimpleNamespace(axes=[axis], position={axis: 0.0})
            for axis in ('X', 'Y', 'Z')
        }),
        _widget=_Widget(),
        _logger=_Logger(),
        _scanCoordinator=ScanExecutionCoordinator(None, None),
        _scanRunToken=None,
        _scanRunStartingPublished=False,
        _analogParameterDict=analog,
        _digitalParameterDict={
            'target_device': [], 'n_linesteps': 1, 'linestep_enable': {},
            'pulse_starts_s': {}, 'pulse_ends_s': {},
            'sequence_time': analog['sequence_time'], 'advanced_mode': False,
        },
        _positionersScan=['X', 'Y', 'None'],
        _designCache=None,
        signalDict=None,
        scanInfoDict=None,
        TTLDevices={},
        _suppressUnreferencedScanWarning=True,
    )
    ctrl.getParameters = lambda: None
    channel.source = ctrl
    return ctrl


def _mockAnalog(stepX, dwell_s, length=100.0):
    return {
        'target_device': ['X', 'Y', 'Z'],
        'axis_length': [length, 20.0, 1.0],
        'axis_step_size': [stepX, 2.0, 1.0],
        'axis_centerpos': [0.0, 0.0, 5.0],
        'axis_startpos': [[0.0], [0.0], [5.0]],
        'scan_dim_target_device': ['X', 'Y', 'None'],
        'sequence_time': dwell_s,
        'phase_delay': 0,
        'd3step_delay': 0,
    }


def test_an_api_scan_too_fast_for_the_galvo_is_rejected_with_the_reason(qtbot):
    channel = _Channel()
    ctrl = _advancedController(channel, _mockAnalog(2.0, 10e-6))

    with pytest.raises(ScanRequestRejectedError) as rejected:
        _facade(channel).runScan()

    assert str(rejected.value).startswith(
        'Fast axis X would sweep at 0.2 µm/µs, above its vel_max of 0.1 µm/µs.'
    )
    assert ctrl._scanCoordinator.activeRunToken is None
    assert ctrl.isRunning is False


def test_the_scan_button_too_fast_for_the_galvo_publishes_only_the_refusal():
    channel = _Channel()
    ctrl = _advancedController(channel, _mockAnalog(2.0, 10e-6))

    SuperScanController.runScan(ctrl)

    assert [event[0] for event in channel.lifecycle] == ['sigScanRequestRejected']
    assert 'Fast axis X' in channel.lifecycle[0][1]
    assert ctrl._scanCoordinator.activeRunToken is None


def test_plotting_a_too_fast_scan_warns_instead_of_raising():
    channel = _Channel()
    ctrl = _advancedController(channel, _mockAnalog(2.0, 10e-6))
    warnings = []
    ctrl._logger.warning = lambda message, *args, **kwargs: warnings.append(message)
    ctrl._widget.isPlotTTLIncluded = lambda: False

    ScanControllerAdvanced.plotScanCurves(ctrl)

    assert len(warnings) == 1
    assert warnings[0].startswith('Nothing to plot: Fast axis X')
    assert ctrl._logger.errors == []


# --------------------------------------------------------------------------
# Scan-time estimate
# --------------------------------------------------------------------------

def _withDelay(params, d3step_delay_us):
    params = copy.deepcopy(params)
    params["d3step_delay"] = d3step_delay_us
    return params


ESTIMATE_CASES = {
    # the one fast line alone: nothing to extrapolate
    "line_x": _xScan(50.0, 0.5, 20e-6),
    # overview-like: few big pixels, turnaround-dominated
    "overview_xy": _xScan(100.0, 1.0, 20e-6, lengthY=100.0, stepY=1.0),
    # acquisition-like: many small pixels
    "fine_xy": _xScan(20.0, 0.05, 20e-6, lengthY=10.0, stepY=0.05),
    "long_dwell_xy": _xScan(10.0, 0.2, 1e-3, lengthY=8.0, stepY=0.2),
    "xy_linesteps": dict(_xScan(20.0, 0.2, 20e-6, lengthY=16.0, stepY=0.2),
                         n_linesteps=3),
    # the rig's XZ: galvo fast, piezo staircase slow
    "xz_galvo_piezo": _params(["GalvoX", "PiezoZ", "GalvoY"],
                              [30.0, 6.0, 1.0], [0.2, 0.3, 1.0],
                              centers=[0.0, 5.0, 0.0]),
    "xyz": _withDelay(_params(["GalvoX", "GalvoY", "PiezoZ"],
                              [30.0, 20.0, 4.0], [0.5, 0.5, 0.5],
                              centers=[0.0, 0.0, 5.0]), 1000),
    "xyz_linesteps": _withDelay(
        _params(["GalvoX", "GalvoY", "PiezoZ"], [20.0, 12.0, 3.0],
                [0.5, 0.5, 0.5], centers=[0.0, 0.0, 5.0], n_linesteps=2),
        500),
    # a stepped (piezo) fast axis
    "zx_piezo_fast": _params(["PiezoZ", "GalvoX", "GalvoY"],
                             [6.0, 10.0, 1.0], [0.3, 0.5, 1.0],
                             centers=[5.0, 0.0, 0.0]),
    # slow axes of three steps or fewer are built at their real count
    "few_slow_steps": _params(["GalvoX", "GalvoY", "PiezoZ"],
                              [20.0, 1.5, 1.5], [0.5, 0.5, 0.5],
                              centers=[0.0, 0.0, 5.0]),
}


@pytest.mark.parametrize('name', sorted(ESTIMATE_CASES))
def test_the_estimate_matches_a_full_build(name):
    """Within 1 % or 2 ms, whichever is larger. The error is a slow galvo
    axis travelling to its first position and back, which a 2- or 3-step
    proxy does over a shorter span: a few milliseconds per slow sweep,
    visible in relative terms only on scans that take milliseconds
    (zx_piezo_fast: 9.8 ms estimated for 9.1 ms)."""
    params = ESTIMATE_CASES[name]
    setup = _rigLikeSetup()

    estimate = GalvoScanDesigner().estimateScanTime(params, setup)
    _, _, scanInfo = GalvoScanDesigner().make_signal(params, setup)
    full = scanInfo['tot_scan_time_s']

    assert abs(estimate - full) <= max(0.01 * full, 2e-3)


@pytest.mark.parametrize('name', ['line_x', 'few_slow_steps'])
def test_nothing_to_extrapolate_is_exact(name):
    params = ESTIMATE_CASES[name]
    setup = _rigLikeSetup()

    estimate = GalvoScanDesigner().estimateScanTime(params, setup)
    _, _, scanInfo = GalvoScanDesigner().make_signal(params, setup)

    assert estimate == pytest.approx(scanInfo['tot_scan_time_s'], rel=1e-12)


def test_the_estimate_is_not_pixels_times_dwell():
    """Why the panel needs it: on an overview frame the turnaround is most
    of the time, so the naive product is off by a large factor."""
    params = ESTIMATE_CASES["overview_xy"]
    naive = 100 * 100 * params["sequence_time"]

    estimate = GalvoScanDesigner().estimateScanTime(params, _rigLikeSetup())

    assert estimate > 2 * naive


@pytest.mark.parametrize('name, builds', [
    ('line_x', 1),          # no slow axis to extrapolate
    ('few_slow_steps', 1),  # slow axes of <= 3 steps: built as they are
    ('overview_xy', 2),     # one slow axis: 2 and 3 steps
    ('xyz', 4),             # two slow axes: 2 x 2 corners
])
def test_the_estimate_builds_only_small_proxies(name, builds, monkeypatch):
    params = ESTIMATE_CASES[name]
    designer = GalvoScanDesigner()
    built = []
    original = designer.make_signal

    def countingMakeSignal(parameterDict, setupInfo):
        built.append(list(parameterDict['axis_length']))
        return original(parameterDict, setupInfo)

    monkeypatch.setattr(designer, 'make_signal', countingMakeSignal)
    designer.estimateScanTime(params, _rigLikeSetup())

    assert len(built) == builds
    for lengths in built:
        # the fast axis is never shortened
        assert lengths[0] == params['axis_length'][0]


def test_estimating_a_refused_design_raises_the_reason():
    with pytest.raises(ScanDesignRefusedError, match='Fast axis GalvoX'):
        GalvoScanDesigner().estimateScanTime(_xScan(100.0, 2.0, 10e-6),
                                             _rigLikeSetup())


def test_a_designer_without_an_estimate_says_so():
    params = _xScan(10.0, 1.0, 20e-6)
    assert BetaScanDesigner().estimateScanTime(params, _rigLikeSetup()) is None


def test_the_advanced_controller_estimates_what_it_would_build():
    """The controller hands the estimate exactly what it hands a build:
    the setup's designer params, the analog dict and the line-step count."""
    channel = _Channel()
    analog = _mockAnalog(0.5, 20e-6, length=20.0)
    ctrl = _advancedController(channel, analog)
    ctrl._digitalParameterDict['n_linesteps'] = 2

    estimate = ScanControllerAdvanced.estimateScanTimeS(ctrl)
    _, scanInfo = ScanControllerAdvanced._make_full_scan(
        ctrl, ctrl._analogParameterDict, ctrl._digitalParameterDict
    )

    assert scanInfo['n_linesteps'] == 2
    assert estimate == pytest.approx(scanInfo['tot_scan_time_s'], rel=0.01)


def test_the_shipped_mock_setup_estimate_matches_the_planning_measurement():
    """F3 of the plan: a 200 µm, 200 x 200 px, 20 µs overview frame on
    galvo_apd_mock_scan_setup.json takes 1.65 s, of which 0.8 s is pixels."""
    setup = _setup_from_user_default('galvo_apd_mock_scan_setup.json')
    params = {
        'target_device': ['X', 'Y', 'Z'],
        'axis_length': [200.0, 200.0, 1.0],
        'axis_step_size': [1.0, 1.0, 1.0],
        'axis_centerpos': [0.0, 0.0, 0.0],
        'axis_startpos': [[0.0], [0.0], [0.0]],
        'sequence_time': 20e-6,
        'phase_delay': 0,
        'd3step_delay': 0,
    }
    estimate = GalvoScanDesigner().estimateScanTime(params, setup)

    assert estimate == pytest.approx(1.651, rel=0.01)


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
