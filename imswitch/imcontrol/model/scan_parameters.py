"""Advanced scan parameter serialization.

Extracted from ``ScanControllerAdvanced`` (audit 2026-06, report 07 [P2]) so the
UI-state <-> scan-dict translation is a small, controller-free service that can
be unit-tested with a fake widget. The controller now mostly binds widgets to
this serializer.

The serializer is pure translation: it owns no controller/hardware state and
takes the widget (duck-typed reader/writer) plus the setup's positioner and TTL
device maps as explicit arguments. Behavior — including the deliberately
lenient ``try/except`` around individual widget accessors — is preserved
verbatim from the original controller methods.

Dict formats:

* ``analog`` — compatible with ``GalvoScanDesigner.expectedParameters``:
  ``target_device``, ``axis_length``, ``axis_step_size``, ``axis_centerpos``,
  ``axis_startpos``, ``scan_dim_target_device``, ``sequence_time``,
  ``phase_delay``, ``d3step_delay``.
* ``digital`` — consumed by ``AdvancedScanTTLCycleDesigner``: ``target_device``,
  ``n_linesteps``, ``Nx``, ``Ny``, ``linestep_enable``, ``pulse_starts_s``,
  ``pulse_ends_s``, ``sequence_time``, ``advanced_mode``, plus line-program and
  intra-pixel positioner-movement keys.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def pixels_for_length_step(length, step) -> int:
    """Canonical pixel/step count for one scan axis: ``round(length / step)``.

    Single source of truth for ``pixels = axis_length / axis_step_size`` so the
    GUI display, ``getDimsScan()``, the digital ``Nx``/``Ny``, and the scan
    signal designers cannot drift onto different rounding rules again (they
    previously used a mix of ``round`` / ``int`` / ``ceil``, so the GUI pixel
    count, the recorded OME dimensions, and the real number of scanned lines all
    disagreed for non-divisible ratios). Returns at least 1 for any active axis
    (``step != 0``), matching the designers' "always run one position" guard.
    """
    step = float(step)
    if step == 0:
        return 1
    return max(1, int(round(float(length) / step)))


def scan_axis_provenance(positioners_scan, positioners_info):
    """WRITE-ONLY recording provenance for the scanned dims.

    Returns ``(devices, physical_axes)``: the devices assigned to scan dims
    (in dim order, ``'None'`` dims dropped) and each device's physical stage
    axis from its :class:`PositionerInfo` (``'?'`` when unknown). Recorded
    images are stored with compatibility ``YX`` axes even for a single-axis
    scan (a Z-only profile is a ``(1, N)`` image whose ``PhysicalSizeX`` is
    the Z step), so this is what preserves *which physical axis was actually
    scanned* — e.g. ``(['ND-PiezoZ'], ['Z'])``.

    Deliberately not consumed anywhere in ImSwitch (no ImProcess reader): it
    exists for the person or tool opening the file. See
    ``docs/galvo-designer-single-axis-findings.md``, phase C.
    """
    devices = [dev for dev in positioners_scan if dev and dev != 'None']
    physical = []
    for dev in devices:
        info = positioners_info.get(dev) if positioners_info else None
        axes = list(getattr(info, 'axes', None) or [])
        physical.append(str(axes[0]) if axes else '?')
    return devices, physical


def axis_pixel_positions(n_pixels, step, *, center=None, start=0.0):
    """Physical positions of ``n_pixels`` scan pixels spaced *exactly* ``step``.

    Convention A (see the ``scan-realized-step-spacing`` audit): the requested
    step size IS the realized pixel pitch, so the scan visits pixels ``step``
    apart and the reported ``pixel_sizes`` / OME ``PhysicalSize`` are truthful.
    Previously the designers spread ``n`` pixels across the full ROI with
    ``linspace`` (endpoint-inclusive), giving a pitch of ``length/(n-1)`` (Beta,
    up to 100% off) or ``length/n`` (Galvo step axis) that did not equal the
    reported step.

    Anchoring (each designer keeps its existing convention so absolute scan
    positions do not move):
      * ``center`` given -- positions centered on it, spanning ``(n-1)*step``.
      * else ``start``   -- first pixel at ``start``, each next pixel ``+step``.
    Returns a float ndarray of length ``max(1, n_pixels)``.
    """
    n = max(1, int(n_pixels))
    offsets = np.arange(n, dtype=float) * float(step)
    if center is not None:
        return float(center) + offsets - (n - 1) / 2.0 * float(step)
    return float(start) + offsets


class AdvancedScanParameterSerializer:
    """Round-trips ``ScanWidgetAdvanced`` UI state <-> analog/digital scan dicts."""

    def build_analog(self, widget, positioners) -> tuple[dict, list]:
        """Serialize PointScan-compatible analog scan parameters from the widget.

        Returns ``(analogParameterDict, positionersScan)``.
        """
        analogParameterDict: dict[str, Any] = {
            "target_device": [],
            "axis_length": [],
            "axis_step_size": [],
            "axis_centerpos": [],
            "axis_startpos": [],
        }

        positionersScan = [widget.getScanDim(i) for i in range(len(positioners))]
        analogParameterDict["scan_dim_target_device"] = list(positionersScan)

        for positionerName in positionersScan:
            if positionerName == "None":
                continue

            size = widget.getScanSize(positionerName)
            stepSize = widget.getScanStepSize(positionerName)
            center = widget.getScanCenterPos(positionerName)

            analogParameterDict["target_device"].append(positionerName)
            analogParameterDict["axis_length"].append(size)
            analogParameterDict["axis_step_size"].append(stepSize)
            analogParameterDict["axis_centerpos"].append(center)
            analogParameterDict["axis_startpos"].append([center])

        # Add non-scan axes as dummy entries to keep older scan designers happy.
        for positionerName in positioners:
            if positionerName not in positionersScan:
                center = widget.getScanCenterPos(positionerName)
                analogParameterDict["target_device"].append(positionerName)
                analogParameterDict["axis_length"].append(1.0)
                analogParameterDict["axis_step_size"].append(1.0)
                analogParameterDict["axis_centerpos"].append(center)
                analogParameterDict["axis_startpos"].append([center])

        seq_time = widget.getSeqTimePar()
        analogParameterDict["sequence_time"] = seq_time
        try:
            analogParameterDict["phase_delay"] = widget.getPhaseDelayPar()
        except Exception:
            analogParameterDict["phase_delay"] = 0
        try:
            analogParameterDict["d3step_delay"] = widget.getd3StepDelayPar()
        except Exception:
            analogParameterDict["d3step_delay"] = 0

        return analogParameterDict, positionersScan

    @staticmethod
    def pixels_for_scan_device(analogParameterDict, deviceName: str) -> int:
        """Return pixel count for a selected scan device from analog params."""
        if deviceName is None or deviceName == "None":
            return 1
        try:
            idx = analogParameterDict["target_device"].index(deviceName)
        except ValueError:
            return 1
        return pixels_for_length_step(
            analogParameterDict["axis_length"][idx],
            analogParameterDict["axis_step_size"][idx],
        )

    def build_digital(self, widget, analogParameterDict, positioners, ttl_devices) -> dict:
        """Serialize advanced TTL and line-step parameters from the widget."""
        seq_time = analogParameterDict["sequence_time"]
        x_dev = widget.getScanDim(0)
        y_dev = widget.getScanDim(1)
        Nx = self.pixels_for_scan_device(analogParameterDict, x_dev)
        Ny = self.pixels_for_scan_device(analogParameterDict, y_dev)

        try:
            widget.commitAdvancedProgramEdits()
        except Exception:
            pass

        try:
            S = int(widget.getNumLineSteps())
        except Exception:
            S = 1

        try:
            advanced_mode = bool(widget.isAdvancedTTLMode())
        except Exception:
            advanced_mode = False
        try:
            advanced_program_mode = widget.getAdvancedProgramMode()
        except Exception:
            advanced_program_mode = "timing"
        sequence_mode = advanced_mode and advanced_program_mode == "sequence"

        included_devices = []
        linestep_enable = {}
        pulse_starts_s = {}
        pulse_ends_s = {}

        for deviceName in ttl_devices.keys():
            # IMPORTANT:
            # linestep_enable must be length S (per linestep), NOT Ny*S.
            # The TTL designer (and scanInfoDict) currently operate with img_dims[1] = Ny (not expanded),
            # and the designer maps expanded_line_idx -> s via (idx % S).
            try:
                enable_vec = [bool(widget.getLineStepEnabled(deviceName, s)) for s in range(S)]
            except Exception:
                enable_vec = [bool(widget.getTTLIncluded(deviceName))] + [False] * (S - 1)

            starts_steps = [[] for _ in range(S)]
            ends_steps = [[] for _ in range(S)]

            if advanced_mode:
                for s in range(S):
                    try:
                        segments = widget.getPulseSegmentsOrFull(deviceName, s)
                        if segments is None:
                            starts_steps[s] = []
                            ends_steps[s] = []
                        else:
                            starts_steps[s] = [t0 for t0, _ in segments]
                            ends_steps[s] = [t1 for _, t1 in segments]
                    except Exception:
                        starts_steps[s] = []
                        ends_steps[s] = []

                    if sequence_mode and starts_steps[s] and ends_steps[s]:
                        enable_vec[s] = True

            # Include device if any step enabled OR any pulses specified
            any_pulses = any(len(starts_steps[s]) or len(ends_steps[s]) for s in range(S))
            if any(enable_vec) or any_pulses:
                included_devices.append(deviceName)
                linestep_enable[deviceName] = enable_vec
                pulse_starts_s[deviceName] = starts_steps
                pulse_ends_s[deviceName] = ends_steps

        # Per-device per-linestep power (%) for AO-capable lasers
        linestep_power_percent = {}
        for deviceName in ttl_devices.keys():
            try:
                vec = [float(widget.getLineStepPowerPercent(deviceName, s)) for s in range(S)]
                vec = [max(0.0, min(100.0, v)) for v in vec]
                linestep_power_percent[deviceName] = vec
            except Exception:
                pass

        # Intra-pixel positioner movement metadata travels with the advanced
        # UI state, but only scan designers that understand these keys consume it.
        try:
            intra_pixel_positioner_movement = bool(widget.isIntraPixelPositionersMode())
        except Exception:
            intra_pixel_positioner_movement = False

        positioner_target_device = []
        positioner_linestep_enable = {}
        positioner_movement_starts_s = {}
        positioner_movement_ends_s = {}
        positioner_step_size_um = {}

        if advanced_mode and intra_pixel_positioner_movement:
            for positionerName in positioners.keys():
                starts_steps = [[] for _ in range(S)]
                ends_steps = [[] for _ in range(S)]
                step_sizes = [[] for _ in range(S)]
                enable_vec = [False for _ in range(S)]

                for s in range(S):
                    try:
                        starts_steps[s] = list(widget.getPulseStarts(positionerName, s) or [])
                    except Exception:
                        starts_steps[s] = []

                    try:
                        ends_steps[s] = list(widget.getPulseEnds(positionerName, s) or [])
                    except Exception:
                        ends_steps[s] = []

                    try:
                        step_sizes[s] = list(widget.getLineStepPositionerStepUm(positionerName, s) or [])
                    except Exception:
                        step_sizes[s] = []

                    enable_vec[s] = bool(starts_steps[s] and ends_steps[s])

                if any(enable_vec):
                    positioner_target_device.append(positionerName)
                    positioner_linestep_enable[positionerName] = enable_vec
                    positioner_movement_starts_s[positionerName] = starts_steps
                    positioner_movement_ends_s[positionerName] = ends_steps
                    positioner_step_size_um[positionerName] = step_sizes

        digitalParameterDict = {
            "target_device": included_devices,
            "n_linesteps": S,
            "Nx": Nx,
            "Ny": Ny,
            "linestep_enable": linestep_enable,
            "pulse_starts_s": pulse_starts_s,
            "pulse_ends_s": pulse_ends_s,
            "sequence_time": seq_time,
            "advanced_mode": advanced_mode,
            "linestep_power_percent": linestep_power_percent,
            "intra_pixel_positioner_movement": intra_pixel_positioner_movement,
            "positioner_target_device": positioner_target_device,
            "positioner_linestep_enable": positioner_linestep_enable,
            "positioner_movement_starts_s": positioner_movement_starts_s,
            "positioner_movement_ends_s": positioner_movement_ends_s,
            "positioner_step_size_um": positioner_step_size_um,
        }

        try:
            digitalParameterDict["advanced_program_mode"] = (
                widget.getAdvancedProgramMode()
            )
            digitalParameterDict["advanced_sequence_rows"] = (
                widget.getAdvancedSequenceRows()
            )
            digitalParameterDict["line_program_devices_enabled"] = (
                widget.isLineProgramDevicesMode()
            )
            digitalParameterDict["advanced_device_lock_master"] = (
                widget.getAdvancedDeviceLockMaster()
            )
            digitalParameterDict["advanced_device_lock_target"] = (
                widget.getAdvancedDeviceLockTarget()
            )
        except Exception:
            pass

        return digitalParameterDict

    def apply(
        self,
        widget,
        analogParameterDict,
        digitalParameterDict,
        positioners,
        ttl_devices,
    ) -> None:
        """Write cached analog/digital parameter dicts back into the widget.

        The reverse of :meth:`build_analog`/:meth:`build_digital` (used by
        ``loadScan``). The caller owns the surrounding lifecycle (the
        ``settingParameters`` guard and the post-apply pixel/plot refresh).
        """
        # --- analog back into widget (like PointScan) ---
        for i, scanDimName in enumerate(analogParameterDict.get("scan_dim_target_device", [])):
            try:
                widget.setScanDim(i, scanDimName)
            except Exception:
                pass

        for i in range(len(analogParameterDict.get("target_device", []))):
            positionerName = analogParameterDict["target_device"][i]
            if positionerName == "None":
                continue
            try:
                widget.setScanSize(positionerName, analogParameterDict["axis_length"][i])
                widget.setScanStepSize(positionerName, analogParameterDict["axis_step_size"][i])
                widget.setScanCenterPos(positionerName, analogParameterDict["axis_centerpos"][i])
            except Exception:
                pass

        # timing
        if "sequence_time" in digitalParameterDict:
            try:
                widget.setSeqTimePar(digitalParameterDict["sequence_time"])
            except Exception:
                pass
        dig = digitalParameterDict or {}

        try:
            widget.setAdvancedTTLMode(bool(dig.get("advanced_mode", False)))
        except Exception:
            pass

        try:
            widget.setLineProgramDevicesMode(
                bool(dig.get("line_program_devices_enabled", False))
            )
        except Exception:
            pass

        try:
            widget.setNumLineSteps(int(dig.get("n_linesteps", 1)))
        except Exception:
            pass

        S = int(dig.get("n_linesteps", 1))
        linestep_enable = dig.get("linestep_enable", {}) or {}
        pulse_starts_s = dig.get("pulse_starts_s", {}) or {}
        pulse_ends_s = dig.get("pulse_ends_s", {}) or {}

        linestep_power_percent = dig.get("linestep_power_percent", {}) or {}
        for dev, vec in linestep_power_percent.items():
            try:
                for s in range(min(S, len(vec))):
                    widget.setLineStepPowerPercent(dev, s, float(vec[s]))
            except Exception:
                pass

        try:
            widget.setIntraPixelPositionersMode(
                bool(dig.get("intra_pixel_positioner_movement", False))
            )
        except Exception:
            pass

        try:
            widget.setAdvancedDeviceLockState(
                dig.get("advanced_device_lock_master", {}) or {},
                dig.get("advanced_device_lock_target", {}) or {},
            )
        except Exception:
            pass

        positioner_starts_s = dig.get("positioner_movement_starts_s", {}) or {}
        positioner_ends_s = dig.get("positioner_movement_ends_s", {}) or {}
        positioner_step_size_um = dig.get("positioner_step_size_um", {}) or {}
        for dev in positioners.keys():
            starts_steps = positioner_starts_s.get(dev, None)
            ends_steps = positioner_ends_s.get(dev, None)
            if starts_steps is not None:
                for s in range(min(S, len(starts_steps))):
                    try:
                        ends = ends_steps[s] if ends_steps is not None and s < len(ends_steps) else []
                        widget.setPulseTimes(dev, s, starts_steps[s], ends)
                    except Exception:
                        pass

            steps = positioner_step_size_um.get(dev, None)
            if steps is not None:
                for s in range(min(S, len(steps))):
                    try:
                        widget.setLineStepPositionerStepUm(dev, s, steps[s])
                    except Exception:
                        pass

        for dev in ttl_devices.keys():
            enable_vec = linestep_enable.get(dev, None)
            if enable_vec is not None:
                for s in range(min(S, len(enable_vec))):
                    try:
                        widget.setLineStepEnabled(dev, s, bool(enable_vec[s]))
                    except Exception:
                        pass

            # pulses only matter if advanced_mode, but restoring them always is fine
            starts_steps = pulse_starts_s.get(dev, None)
            ends_steps = pulse_ends_s.get(dev, None)
            if starts_steps is not None and ends_steps is not None:
                for s in range(min(S, len(starts_steps), len(ends_steps))):
                    try:
                        widget.setPulseTimes(dev, s, starts_steps[s], ends_steps[s])
                    except Exception:
                        pass

        try:
            widget.setAdvancedProgramMode(
                dig.get("advanced_program_mode", "timing")
            )
            widget.setAdvancedSequenceRows(
                dig.get("advanced_sequence_rows", []) or []
            )
        except Exception:
            pass

        # ensure the advanced panel reflects the stored model
        try:
            widget._syncPulseEditsFromModel()
        except Exception:
            pass


__all__ = ["AdvancedScanParameterSerializer"]
