"""Behavioral tests for AdvancedScanParameterSerializer (audit 07 extraction).

Before the extraction this widget-state <-> scan-dict translation lived inside
ScanControllerAdvanced and could only be exercised through a full Qt controller.
The serializer is controller-free, so a fake widget gives it real round-trip
coverage: build analog/digital dicts from a configured widget, apply them to a
fresh widget, and the rebuilt dicts must match.
"""

import numpy as np

from imswitch.imcontrol.model.scan_parameters import AdvancedScanParameterSerializer


class FakeScanWidget:
    """Minimal in-memory stand-in for ScanWidgetAdvanced.

    Stores exactly the state the serializer reads/writes. ``scan_dims`` maps a
    scan-dimension index to a positioner name (or ``"None"``); per-positioner
    ``size``/``step``/``center`` and per-device line-step state mirror the real
    widget accessors.
    """

    def __init__(self, n_scan_dims=3):
        self.scan_dims = {i: "None" for i in range(n_scan_dims)}
        self.size = {}
        self.step = {}
        self.center = {}
        self.seq_time = 1.0e-5
        self.phase_delay = 0
        self.d3step_delay = 0
        self.num_linesteps = 1
        self.advanced_ttl = False
        self.program_mode = "timing"
        self.line_program_devices = False
        self.intra_pixel = False
        self.included = {}          # dev -> bool (fallback TTL include)
        self.enable = {}            # (dev, s) -> bool
        self.pulse_segments = {}    # (dev, s) -> list[(t0, t1)] or None
        self.power = {}             # (dev, s) -> float
        self.lock_master = {}
        self.lock_target = {}
        self.sequence_rows = []
        self.pulse_times = {}       # (dev, s) -> (starts, ends)
        self.positioner_step_um = {}  # (dev, s) -> list
        self.synced = False

    # --- getters ---
    def getScanDim(self, i):
        return self.scan_dims.get(i, "None")

    def getScanSize(self, name):
        return self.size.get(name, 0.0)

    def getScanStepSize(self, name):
        return self.step.get(name, 0.0)

    def getScanCenterPos(self, name):
        return self.center.get(name, 0.0)

    def getSeqTimePar(self):
        return self.seq_time

    def getPhaseDelayPar(self):
        return self.phase_delay

    def getd3StepDelayPar(self):
        return self.d3step_delay

    def commitAdvancedProgramEdits(self):
        pass

    def getNumLineSteps(self):
        return self.num_linesteps

    def isAdvancedTTLMode(self):
        return self.advanced_ttl

    def getAdvancedProgramMode(self):
        return self.program_mode

    def getLineStepEnabled(self, dev, s):
        return self.enable.get((dev, s), False)

    def getTTLIncluded(self, dev):
        return self.included.get(dev, False)

    def getPulseSegmentsOrFull(self, dev, s):
        return self.pulse_segments.get((dev, s))

    def getLineStepPowerPercent(self, dev, s):
        return self.power.get((dev, s), 0.0)

    def isIntraPixelPositionersMode(self):
        return self.intra_pixel

    def getPulseStarts(self, name, s):
        return self.pulse_times.get((name, s), ([], []))[0]

    def getPulseEnds(self, name, s):
        return self.pulse_times.get((name, s), ([], []))[1]

    def getLineStepPositionerStepUm(self, name, s):
        return self.positioner_step_um.get((name, s), [])

    def getAdvancedSequenceRows(self):
        return self.sequence_rows

    def isLineProgramDevicesMode(self):
        return self.line_program_devices

    def getAdvancedDeviceLockMaster(self):
        return self.lock_master

    def getAdvancedDeviceLockTarget(self):
        return self.lock_target

    # --- setters ---
    def setScanDim(self, i, name):
        self.scan_dims[i] = name

    def setScanSize(self, name, val):
        self.size[name] = val

    def setScanStepSize(self, name, val):
        self.step[name] = val

    def setScanCenterPos(self, name, val):
        self.center[name] = val

    def setSeqTimePar(self, val):
        self.seq_time = val

    def setAdvancedTTLMode(self, val):
        self.advanced_ttl = val

    def setLineProgramDevicesMode(self, val):
        self.line_program_devices = val

    def setNumLineSteps(self, val):
        self.num_linesteps = val

    def setLineStepPowerPercent(self, dev, s, val):
        self.power[(dev, s)] = val

    def setIntraPixelPositionersMode(self, val):
        self.intra_pixel = val

    def setAdvancedDeviceLockState(self, master, target):
        self.lock_master = master
        self.lock_target = target

    def setPulseTimes(self, dev, s, starts, ends):
        self.pulse_times[(dev, s)] = (list(starts), list(ends))
        # Mirror into the segment accessor so a rebuild sees the same pulses.
        if starts and ends:
            self.pulse_segments[(dev, s)] = list(zip(starts, ends))

    def setLineStepPositionerStepUm(self, dev, s, val):
        self.positioner_step_um[(dev, s)] = val

    def setLineStepEnabled(self, dev, s, val):
        self.enable[(dev, s)] = val

    def setAdvancedProgramMode(self, mode):
        self.program_mode = mode

    def setAdvancedSequenceRows(self, rows):
        self.sequence_rows = rows

    def _syncPulseEditsFromModel(self):
        self.synced = True


# Positioner/TTL device maps are dict-like in the controller.
_POSITIONERS = {"X": object(), "Y": object(), "Z": object()}
_TTL = {"488": object(), "561": object()}


def _configured_widget():
    w = FakeScanWidget()
    w.scan_dims = {0: "X", 1: "Y", 2: "None"}
    w.size = {"X": 10.0, "Y": 8.0, "Z": 0.0}
    w.step = {"X": 0.5, "Y": 0.5, "Z": 1.0}
    w.center = {"X": 1.0, "Y": 2.0, "Z": 3.0}
    w.seq_time = 2.0e-5
    w.num_linesteps = 2
    w.advanced_ttl = True
    w.program_mode = "timing"
    w.enable = {("488", 0): True, ("488", 1): False, ("561", 0): False, ("561", 1): True}
    w.pulse_segments = {("488", 0): [(0.0, 1.0e-5)], ("561", 1): [(0.5e-5, 1.5e-5)]}
    w.power = {("488", 0): 80.0, ("488", 1): 0.0, ("561", 0): 0.0, ("561", 1): 50.0}
    w.lock_master = {"488": True}
    w.lock_target = {"561": "488"}
    return w


def test_build_analog_lists_scan_and_dummy_axes():
    serializer = AdvancedScanParameterSerializer()
    analog, positioners_scan = serializer.build_analog(_configured_widget(), _POSITIONERS)

    assert positioners_scan == ["X", "Y", "None"]
    assert analog["scan_dim_target_device"] == ["X", "Y", "None"]
    # X and Y are scan axes with their real size/step; Z is a dummy non-scan axis.
    assert analog["target_device"] == ["X", "Y", "Z"]
    assert analog["axis_length"] == [10.0, 8.0, 1.0]
    assert analog["axis_step_size"] == [0.5, 0.5, 1.0]
    assert analog["axis_centerpos"] == [1.0, 2.0, 3.0]
    assert analog["sequence_time"] == 2.0e-5


def test_pixels_for_scan_device_edge_cases():
    serializer = AdvancedScanParameterSerializer()
    analog = {
        "target_device": ["X"],
        "axis_length": [10.0],
        "axis_step_size": [0.5],
    }
    assert serializer.pixels_for_scan_device(analog, "X") == 20
    assert serializer.pixels_for_scan_device(analog, "None") == 1
    assert serializer.pixels_for_scan_device(analog, "missing") == 1
    zero = {"target_device": ["X"], "axis_length": [10.0], "axis_step_size": [0.0]}
    assert serializer.pixels_for_scan_device(zero, "X") == 1


def test_build_digital_includes_enabled_and_pulsed_devices():
    serializer = AdvancedScanParameterSerializer()
    w = _configured_widget()
    analog, _ = serializer.build_analog(w, _POSITIONERS)
    digital = serializer.build_digital(w, analog, _POSITIONERS, _TTL)

    assert digital["n_linesteps"] == 2
    assert digital["advanced_mode"] is True
    assert digital["Nx"] == 20  # 10.0 / 0.5
    assert digital["Ny"] == 16  # 8.0 / 0.5
    # 488 is enabled at s=0; 561 is enabled at s=1 -> both included.
    assert set(digital["target_device"]) == {"488", "561"}
    assert digital["linestep_enable"]["488"] == [True, False]
    assert digital["linestep_enable"]["561"] == [False, True]
    assert digital["pulse_starts_s"]["488"][0] == [0.0]
    assert digital["linestep_power_percent"]["488"] == [80.0, 0.0]
    assert digital["advanced_device_lock_master"] == {"488": True}


def test_disabled_device_without_pulses_is_excluded():
    serializer = AdvancedScanParameterSerializer()
    w = _configured_widget()
    w.enable = {("488", 0): False, ("488", 1): False, ("561", 0): False, ("561", 1): False}
    w.pulse_segments = {}
    analog, _ = serializer.build_analog(w, _POSITIONERS)
    digital = serializer.build_digital(w, analog, _POSITIONERS, _TTL)
    assert digital["target_device"] == []


def test_round_trip_build_apply_build_is_stable():
    """build -> apply to a fresh widget -> rebuild must reproduce the dicts."""
    serializer = AdvancedScanParameterSerializer()
    source = _configured_widget()
    analog = serializer.build_analog(source, _POSITIONERS)[0]
    digital = serializer.build_digital(source, analog, _POSITIONERS, _TTL)

    fresh = FakeScanWidget()
    serializer.apply(fresh, analog, digital, _POSITIONERS, _TTL)
    assert fresh.synced is True  # controller-owned refresh hook still invoked

    analog2 = serializer.build_analog(fresh, _POSITIONERS)[0]
    digital2 = serializer.build_digital(fresh, analog2, _POSITIONERS, _TTL)

    # Scan axes and their calibration round-trip exactly.
    assert analog2["scan_dim_target_device"] == analog["scan_dim_target_device"]
    for key in ("target_device", "axis_length", "axis_step_size", "axis_centerpos"):
        assert analog2[key] == analog[key], key
    # Digital: device inclusion, enables, pixel counts and lock state survive.
    assert set(digital2["target_device"]) == set(digital["target_device"])
    assert digital2["linestep_enable"] == digital["linestep_enable"]
    assert (digital2["Nx"], digital2["Ny"]) == (digital["Nx"], digital["Ny"])
    assert digital2["advanced_device_lock_master"] == digital["advanced_device_lock_master"]
    assert digital2["linestep_power_percent"] == digital["linestep_power_percent"]


def test_apply_tolerates_partial_dicts():
    """apply must not raise on sparse/empty dicts (loadScan of old states)."""
    serializer = AdvancedScanParameterSerializer()
    fresh = FakeScanWidget()
    serializer.apply(fresh, {}, {}, _POSITIONERS, _TTL)
    serializer.apply(fresh, {"scan_dim_target_device": ["X"]}, {"n_linesteps": 1}, _POSITIONERS, _TTL)
    assert fresh.scan_dims[0] == "X"
