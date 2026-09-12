"""The two point-scan delays are times in microseconds, seeded from the setup
file, restored on load, and never silently zeroed."""

from types import SimpleNamespace

import pytest

from imswitch.imcontrol.model.scan_parameters import (
    AdvancedScanParameterSerializer, seed_scan_delays_from_setup,
)


class _Widget:
    def __init__(self, phase='100', d3='0'):
        self.phase = phase
        self.d3 = d3
        self.set = []

    def getPhaseDelayPar(self):
        return float(self.phase)

    def getd3StepDelayPar(self):
        return float(self.d3)

    def setPhaseDelayPar(self, value):
        self.set.append(('phase', value))

    def setd3StepDelayPar(self, value):
        self.set.append(('d3', value))

    def setSeqTimePar(self, value):
        self.set.append(('seq', value))

    def getSeqTimePar(self):
        return 0.02


def test_the_setup_file_seeds_both_delays():
    widget = _Widget()
    setup = SimpleNamespace(scan=SimpleNamespace(
        scanDesignerParams={'phase_delay': 85, 'd3step_delay': 500}))
    seed_scan_delays_from_setup(widget, setup)
    assert widget.set == [('phase', 85), ('d3', 500)]


def test_a_setup_without_the_delays_leaves_the_fields_alone():
    widget = _Widget()
    seed_scan_delays_from_setup(widget, SimpleNamespace(scan=SimpleNamespace(scanDesignerParams={})))
    seed_scan_delays_from_setup(widget, SimpleNamespace(scan=None))
    assert widget.set == []


def test_the_advanced_serializer_restores_the_delays_it_serialised():
    serializer = AdvancedScanParameterSerializer()
    widget = _Widget()
    serializer.apply(
        widget,
        {'target_device': [], 'scan_dim_target_device': [], 'phase_delay': 85, 'd3step_delay': 20},
        {'sequence_time': 0.02},
        positioners={}, ttl_devices={},
    )
    assert ('phase', 85) in widget.set and ('d3', 20) in widget.set


def test_a_garbled_delay_is_an_error_not_a_silent_zero():
    """A bare `except Exception: = 0` turned a typo into a 0 µs phase delay."""
    serializer = AdvancedScanParameterSerializer()
    widget = _Widget(phase='1,5')
    widget.getScanDim = lambda i: 'None'
    with pytest.raises(ValueError):
        serializer.build_analog(widget, positioners=[])
