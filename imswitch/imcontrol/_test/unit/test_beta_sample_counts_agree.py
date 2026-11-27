"""Beta's scan and its TTL designers count samples by one rule (scan timing
plan, S0; the Beta audit of 2026-10-16).

S0 moved BetaScanDesigner onto ``samples_for_duration`` -- the exact count of
a dwell's samples -- while BetaTTLCycleDesigner still took the ceiling of a
float product. For about 2 % of dwells that product is noise above a whole
number (a 5.03 ms dwell typed in ms: 503.00000000000006 samples at 100 kHz),
so the TTL cycle got one sample more than the scan's pixel and the two
streams no longer had the same length. These pin the pairing the shipped
setups use -- Beta with BetaTTLCycleDesigner (Base and MoNaLISA widgets) and
with AdvancedScanTTLCycleDesigner (Advanced) -- on exactly such dwells.
"""
import math

import numpy as np
import pytest

from imswitch.imcontrol.model.scan_parameters import samples_for_duration
from imswitch.imcontrol.model.signaldesigners.AdvancedScanTTLCycleDesigner import (
    AdvancedScanTTLCycleDesigner,
)
from imswitch.imcontrol.model.signaldesigners.BetaScanDesigner import BetaScanDesigner
from imswitch.imcontrol.model.signaldesigners.BetaTTLCycleDesigner import BetaTTLCycleDesigner

from .test_beta_signal_goldens import _params, _setup

#: (sample rate, dwell typed in ms) whose seconds times the rate is float
#: noise just above a whole number of samples
NOISY_DWELLS = [(100_000, 5.03), (100_000, 5.69), (1_000_000, 5.003), (1_000_000, 5.017)]
#: and one that is a whole number exactly, and one that is not one at all
PLAIN_DWELLS = [(100_000, 5.0), (100_000, 4.025)]


def _scan(rate_hz, dwell_ms, return_ms=10.0):
    setup = _setup()
    setup.scan.sampleRate = rate_hz
    params = _params([20.0, 10.0, 0.0], [5.0, 5.0, 1.0], dwell=dwell_ms / 1000,
                     return_time=return_ms / 1000)
    signals, _positions, info = BetaScanDesigner().make_signal(params, setup)
    return setup, params, signals, info


@pytest.mark.parametrize('rate_hz,dwell_ms', NOISY_DWELLS)
def test_the_noisy_dwells_are_what_this_file_says_they_are(rate_hz, dwell_ms):
    seconds = dwell_ms / 1000
    assert math.ceil(seconds * rate_hz) == samples_for_duration(seconds, rate_hz) + 1


@pytest.mark.parametrize('rate_hz,dwell_ms', NOISY_DWELLS + PLAIN_DWELLS)
def test_beta_and_its_ttl_designer_play_streams_of_one_length(rate_hz, dwell_ms):
    setup, params, signals, info = _scan(rate_hz, dwell_ms)
    seconds = params['sequence_time']
    ttl = BetaTTLCycleDesigner().make_signal(
        {'target_device': ['Camera'], 'TTL_start': [[0.0]], 'TTL_end': [[seconds / 2]],
         'sequence_time': seconds}, setup, info)
    assert info['samples_per_pixel'] == samples_for_duration(seconds, rate_hz)
    for name, analog in signals.items():
        assert len(ttl['Camera']) == len(analog) == info['scan_samples_total'], name
    # one camera trigger per pixel, each at the start of the pixel's dwell
    rises = np.flatnonzero(np.diff(np.concatenate([[False], ttl['Camera']]).astype(np.int8)) == 1)
    assert len(rises) == info['positions'][0] * info['positions'][1]
    assert rises[1] - rises[0] == info['samples_per_pixel']


@pytest.mark.parametrize('rate_hz,dwell_ms', NOISY_DWELLS + PLAIN_DWELLS)
def test_beta_and_the_advanced_ttl_designer_play_streams_of_one_length(rate_hz, dwell_ms):
    setup, params, signals, info = _scan(rate_hz, dwell_ms)
    digital = {'target_device': ['L488'], 'n_linesteps': 1, 'linestep_enable': {'L488': [True]},
               'pulse_starts_s': {'L488': [[]]}, 'pulse_ends_s': {'L488': [[]]},
               'advanced_mode': False, 'sequence_time': params['sequence_time']}
    ttl, _ = AdvancedScanTTLCycleDesigner().make_signal(digital, setup, info)
    for name, analog in signals.items():
        assert len(ttl['L488']) == len(analog), name
    on = np.flatnonzero(ttl['L488'])
    runs = np.split(on, np.flatnonzero(np.diff(on) > 1) + 1)
    assert [len(run) for run in runs] == [info['scan_samples'][1]] * info['positions'][1]


def test_beta_estimates_the_time_that_plays():
    """The maxScanTimeMin cap and the estimate read the dwell and return as
    played -- whole samples -- not as typed."""
    setup, params, signals, info = _scan(100_000, 4.025, return_ms=10.005)
    estimate = BetaScanDesigner().estimateScanSeconds(params, setup)
    assert estimate == pytest.approx(info['scan_samples_total'] / 100_000)



@pytest.mark.parametrize('dwell_ms', [5.0, 5.03])
def test_a_camera_pulse_that_fills_the_cycle_is_refused_with_the_reason(dwell_ms):
    """A camera pulse from the start to the end of the dwell never falls
    between pixels: one rising edge per line, while the recording counts one
    frame per pixel and waits. On main a clean dwell already did that; a
    noisy one (5.03 ms) had a one-sample gap by accident, which the exact
    count closed (review 2026-10-16). Refused, with what to change."""
    from imswitch.imcontrol.model.errors import ScanDesignRefusedError
    setup, params, _signals, info = _scan(100_000, dwell_ms)
    setup.detectors = {'Camera': object()}
    seconds = params['sequence_time']
    with pytest.raises(ScanDesignRefusedError, match="Camera's pulse fills the whole"):
        BetaTTLCycleDesigner().make_signal(
            {'target_device': ['Camera'], 'TTL_start': [[0.0]], 'TTL_end': [[seconds]],
             'sequence_time': seconds}, setup, info)
    # a laser on for the whole pixel is fine, and so is a camera pulse that falls
    ttl = BetaTTLCycleDesigner().make_signal(
        {'target_device': ['L488', 'Camera'], 'TTL_start': [[0.0], [0.0]],
         'TTL_end': [[seconds], [seconds - 1e-5]], 'sequence_time': seconds}, setup, info)
    rises = np.flatnonzero(np.diff(np.r_[False, ttl['Camera']].astype(np.int8)) == 1)
    assert len(rises) == info['positions'][0] * info['positions'][1]


# --- end to end on the simulated rig ----------------------------------------------------------

def _betaRig(sampleRate):
    import json
    from imswitch.imcontrol._test import optionsBasic
    from imswitch.imcontrol.controller.CommunicationChannel import CommunicationChannel
    from imswitch.imcontrol.controller.MasterController import MasterController
    from imswitch.imcontrol.controller.basecontrollers import ImConWidgetControllerFactory
    from imswitch.imcontrol.controller.controllers.ScanControllerAdvanced import (
        ScanControllerAdvanced,
    )
    from imswitch.imcontrol.view import ViewSetupInfo
    from imswitch.imcontrol.view.widgets.ScanWidgetAdvanced import ScanWidgetAdvanced
    from .test_scan_controller_advanced_orders import MOCK_STAGE, AdvancedRig
    from .test_simple_point_scan_controller import SETUP, _Main

    rig = AdvancedRig.__new__(AdvancedRig)
    setup = json.load(open(SETUP))
    setup['scan'].update(scanWidgetType='Advanced', scanDesigner='BetaScanDesigner',
                         scanDesignerParams=dict(MOCK_STAGE), sampleRate=sampleRate)
    rig.setup = ViewSetupInfo.from_json(json.dumps(setup))
    rig.main = _Main()
    rig.channel = CommunicationChannel(rig.main, rig.setup)
    rig.master = MasterController(rig.setup, rig.channel, rig.main._moduleCommChannel)
    rig.factory = ImConWidgetControllerFactory(rig.setup, rig.master, rig.channel,
                                               rig.main._moduleCommChannel)
    rig.widget = ScanWidgetAdvanced(optionsBasic)
    rig.scan = rig.factory.createController(ScanControllerAdvanced, rig.widget)
    rig.main.controllers['Scan'] = rig.scan
    rig.ended = []
    rig.channel.sigScanEnded.connect(lambda *a: rig.ended.append(1))
    return rig


@pytest.mark.parametrize('rate_hz,dwell_ms', [(100_000, 0.51), (1_000_000, 0.318)])
def test_a_beta_scan_with_a_noisy_dwell_runs_end_to_end_with_its_ttl(qtbot, rate_hz, dwell_ms):
    """Through the real Advanced controller, the simulated NI-DAQ and the
    mock APD: the laser line and the stage waveforms have one length, each
    pixel is the exact sample count, and the image is whole."""
    rig = _betaRig(rate_hz)
    try:
        rig.load(lengths=[3.0, 2.0, 0.0], steps=[0.5, 0.5, 1.0], dwell=dwell_ms / 1000, ttl=True)
        image = rig.run(timeoutS=60.0)
        info = rig.scan.scanInfoDict
        assert info['samples_per_pixel'] == samples_for_duration(dwell_ms / 1000, rate_hz)
        assert image.shape[-2:] == (4, 6) and np.isfinite(image).all() and image.sum() > 0
        devices, digital, shift = rig.master.nidaqManager._lastDigitalWrite
        assert devices and shift == 0                          # Beta plays no phase delay
        assert {len(line) for line in digital} == {info['scan_samples_total']}
        assert info['lag']['requested_s'] == 0.0
    finally:
        rig.close()
