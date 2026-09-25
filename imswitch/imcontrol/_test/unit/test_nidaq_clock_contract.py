"""The NI-DAQ clocks are named once, and the point detectors read them.

The APD and PMT managers hardcoded ``ctr2InternalOutput`` and 1 MHz while the
setup file chose which counter generates the timer pulse train; the scan
clock's 100 kHz was a literal at eight sites while ``scan.sampleRate`` was a
required config field; reads inherited nidaqmx's 10 s timeout; and the
analog-input range was accepted and dropped.
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from imswitch.imcontrol.model import DetectorInfo, SetupInfo
from imswitch.imcontrol.model.managers.NidaqManager import (
    NidaqManager, NidaqManagerError, READ_TIMEOUT_FLOOR_S, SCAN_CLOCK_RATE_HZ,
    TIMER_COUNTER_RATE_HZ,
)
from imswitch.imcontrol.model.managers.detectors.APDManager import APDManager
from imswitch.imcontrol.model.managers.detectors.PMTManager import PMTManager


def _setup(timerCounterChannel):
    return SetupInfo.from_json(json.dumps({
        'detectors': {}, 'lasers': {}, 'positioners': {},
        'scan': {
            'scanWidgetType': 'PointScan', 'scanDesigner': 'GalvoScanDesigner',
            'scanDesignerParams': {}, 'TTLCycleDesigner': 'PointScanTTLCycleDesigner',
            'TTLCycleDesignerParams': {}, 'sampleRate': 100000,
        },
        'nidaq': {'simulation': True, 'timerCounterChannel': timerCounterChannel,
                  'startTrigger': False},
    }), infer_missing=True)


def _apd_info(**props):
    return DetectorInfo(analogChannel=None, digitalLine=None, managerName='APDManager',
                        managerProperties={'ctrInputLine': 0, 'terminal': '/Dev1/PFI0',
                                           'deviceName': 'Dev1', **props},
                        forAcquisition=True)


def _pmt_info(**props):
    return DetectorInfo(analogChannel=None, digitalLine=None, managerName='PMTManager',
                        managerProperties={'analogInputLine': 0, 'deviceName': 'Dev1', **props},
                        forAcquisition=True)


@pytest.mark.parametrize('channel, terminal', [
    ('Dev1/ctr2', '/Dev1/Ctr2InternalOutput'),
    ('Dev2/CTR0', '/Dev2/Ctr0InternalOutput'),
    (3, '/Dev1/Ctr3InternalOutput'),
    (None, None),
])
def test_the_timer_clock_terminal_follows_the_configured_counter(channel, terminal):
    assert NidaqManager(_setup(channel)).getTimerClockTerminal() == terminal


def test_a_counter_name_that_is_not_one_is_refused():
    with pytest.raises(NidaqManagerError, match='must name a counter'):
        NidaqManager(_setup('Dev1/port0/line1')).getTimerClockTerminal()


def test_the_point_detectors_take_the_clock_from_the_manager():
    nidaq = NidaqManager(_setup('Dev1/ctr3'))
    apd = APDManager(_apd_info(), 'APD', nidaq)
    pmt = PMTManager(_pmt_info(), 'PMT', nidaq)
    assert apd._nidaq_clock_source == '/Dev1/Ctr3InternalOutput'
    assert pmt._nidaq_clock_source == '/Dev1/Ctr3InternalOutput'
    assert apd._detection_samplerate == pmt._detection_samplerate == TIMER_COUNTER_RATE_HZ
    assert apd.requireTimerClock() == '/Dev1/Ctr3InternalOutput'


def test_no_timer_counter_is_a_clear_refusal_not_a_dead_terminal():
    """Six of the seven shipped setups leave timerCounterChannel null; the
    detectors used to arm against ctr2InternalOutput, which nothing drove."""
    nidaq = NidaqManager(_setup(None))
    apd = APDManager(_apd_info(), 'APD', nidaq)
    assert apd._nidaq_clock_source is None
    with pytest.raises(RuntimeError, match='nidaq.timerCounterChannel is not set'):
        apd.requireTimerClock()


def test_a_test_double_without_the_accessors_still_constructs():
    nidaq = Mock()
    apd = APDManager(_apd_info(), 'APD', nidaq)
    assert apd._nidaq_clock_source is None
    assert apd._detection_samplerate == 1e6


def test_the_pmt_declares_its_analog_input_range():
    nidaq = NidaqManager(_setup('Dev1/ctr2'))
    assert PMTManager(_pmt_info(), 'PMT', nidaq)._aiVoltageRange == (-5.0, 5.0)
    declared = PMTManager(_pmt_info(aiVoltageMin=0.0, aiVoltageMax=10.0), 'PMT', nidaq)
    assert declared._aiVoltageRange == (0.0, 10.0)


def test_the_read_timeout_follows_the_read():
    task = SimpleNamespace(timing=SimpleNamespace(samp_clk_rate=1e6))
    assert NidaqManager._readTimeoutFor(task, 1000) == pytest.approx(READ_TIMEOUT_FLOOR_S + 0.002)
    # a 30 s line: twice its length plus the floor, not the driver's 10 s
    assert NidaqManager._readTimeoutFor(task, 30_000_000) == pytest.approx(70.0)
    clockless = SimpleNamespace(timing=SimpleNamespace())
    assert NidaqManager._readTimeoutFor(clockless, 30_000_000) == READ_TIMEOUT_FLOOR_S


def test_reads_pass_the_derived_timeout_to_the_driver():
    nidaq = NidaqManager(_setup('Dev1/ctr2'))
    calls = []
    nidaq.tasks['APD'] = SimpleNamespace(
        timing=SimpleNamespace(samp_clk_rate=1e6),
        read=lambda samples, timeout: calls.append((samples, timeout)) or [],
    )
    nidaq.readInputTask('APD', 30_000_000)
    assert calls == [(30_000_000, pytest.approx(70.0))]


def test_a_scan_rate_the_timebase_cannot_run_is_refused_on_hardware():
    NidaqManager._checkScanClockRate(SCAN_CLOCK_RATE_HZ)
    NidaqManager._checkScanClockRate(None)
    with pytest.raises(NidaqManagerError, match='scan.sampleRate is 1000000'):
        NidaqManager._checkScanClockRate(1_000_000)
    # simulation honours any rate; only hardware is clocked from the timebase
    NidaqManager(_setup('Dev1/ctr2'))
