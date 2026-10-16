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

import sys
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import Any

import numpy as np


#: How a requested duration becomes a whole number of samples, in every
#: designer and in the Advanced controller: the ``step`` rule of the
#: ``advanced-compatible-v1`` timing policy (ceil: a dwell is never
#: shortened). A test holds the two together.
DWELL_ROUNDING = 'ceil'

#: A product that lands this close to a whole number is that whole number:
#: ``2e-05 * 100000`` is not exactly 2 in floating point, and a ceil of
#: it would be 3 samples. Relative to the product, two ulps.
GRID_RELATIVE_ERROR = 2 * sys.float_info.epsilon


#: A product of seconds and rate within this many samples of a whole or a
#: half is taken as it: times are typed as decimals (0.015 ms), and the
#: float that carries one (1.4999999999999999e-05) must not land its
#: product on the wrong side of 1.5.
SAMPLE_PRODUCT_RESOLUTION = Decimal('1e-9')


def samples_for_duration(seconds, rate_hz, rounding=DWELL_ROUNDING) -> int:
    """The whole number of samples a duration takes on a ``rate_hz`` grid,
    under ``rounding``: 'ceil' (never shorter), 'nearest-even'
    (``round``), or 'exact' (a duration off the grid raises ValueError
    naming its two neighbours). The ONE arithmetic the galvo and Beta
    designers, the TTL designer, the Advanced controller and the scan
    program's timing policies share (plan scan-timing, rule 3). The product
    is taken as the decimals were written (``repr``), to a nanosample, so
    ``2e-05 * 100000`` is 2 and ``35e-6 * 100000`` is 3.5, not their float
    neighbours."""
    product = (Decimal(repr(float(seconds))) * Decimal(repr(float(rate_hz)))) \
        .quantize(SAMPLE_PRODUCT_RESOLUTION)
    if rounding == 'ceil':
        return int(product.to_integral_value(rounding=ROUND_CEILING))
    if rounding == 'nearest-even':
        return int(product.to_integral_value(rounding=ROUND_HALF_EVEN))
    if rounding == 'exact':
        whole = product.to_integral_value(rounding=ROUND_HALF_EVEN)
        if whole == product:
            return int(whole)
        low = int(product.to_integral_value(rounding=ROUND_FLOOR))
        raise ValueError(f'{float(seconds):g} s is not on the {1 / float(rate_hz):g} s grid: '
                         f'{low / float(rate_hz):g} or {(low + 1) / float(rate_hz):g} s')
    raise ValueError(f'Unknown rounding {rounding!r}')


def realized_dwell_s(dwell_s, rate_hz, rounding=DWELL_ROUNDING) -> float:
    """The dwell as it plays: a whole number of samples on the grid."""
    return samples_for_duration(dwell_s, rate_hz, rounding) / float(rate_hz)


def dwell_request_if_changed(dwell_s, rate_hz, rounding=DWELL_ROUNDING):
    """The requested dwell when realizing it changed it, else ``None``: what
    a contract records beside ``dwell_time``. A request that is the realized
    dwell to within the grid tolerance (``1e-5`` typed as ``10 * 1e-6``) is
    not a change."""
    realized = realized_dwell_s(dwell_s, rate_hz, rounding)
    if abs(float(dwell_s) - realized) <= GRID_RELATIVE_ERROR * max(abs(realized), abs(float(dwell_s))):
        return None
    return float(dwell_s)


def lag_samples_for(phase_delay_us, rate_hz) -> int:
    """The phase delay -- the galvo's response lag, in µs -- as whole
    samples of a clock at ``rate_hz``, nearest (half to even). The one
    rounding the digital lines' delay (on the scan clock) and the point
    detectors' delay (on their timer) share."""
    lag_us = float(phase_delay_us or 0)
    if lag_us <= 0:
        return 0
    return samples_for_duration(lag_us * 1e-6, rate_hz, 'nearest-even')


def lag_report(phase_delay_us, scan_rate_hz, timer_rate_hz) -> dict:
    """What a phase delay realizes (plan scan timing, S5): the lag asked for,
    the delay the digital lines play (whole scan-clock samples), the delay
    the point detectors read with (whole timer samples), and the residual
    between the two -- at most half a scan-clock sample, where the gate and
    the detector windows do not coincide. Seconds throughout."""
    requested = max(0.0, float(phase_delay_us or 0)) * 1e-6
    gate = lag_samples_for(phase_delay_us, scan_rate_hz) / float(scan_rate_hz)
    detector = lag_samples_for(phase_delay_us, timer_rate_hz) / float(timer_rate_hz)
    return {'requested_s': requested, 'gate_delay_s': gate, 'detector_delay_s': detector,
            'residual_s': detector - gate}


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


def scan_axis_provenance(positioners_scan, positioners_info,
                         axis_lengths=None, axis_step_sizes=None):
    """WRITE-ONLY recording provenance for the scanned dims.

    Returns ``(devices, physical_axes)``: the devices that actually scan
    (in dim order) and each device's physical stage axis from its
    :class:`PositionerInfo` (``'?'`` when unknown). Recorded images are
    stored with compatibility ``YX`` axes even for a single-axis scan (a
    Z-only profile is a ``(1, N)`` image whose ``PhysicalSizeX`` is the Z
    step), so this is what preserves *which physical axis was actually
    scanned* — e.g. ``(['ND-PiezoZ'], ['Z'])``.

    ``'None'`` dims are always dropped. When ``axis_lengths`` and
    ``axis_step_sizes`` are given (index-aligned with ``positioners_scan``,
    e.g. the analog dict's ``target_device``/``axis_length``/
    ``axis_step_size``), devices whose axis collapses to a single step are
    dropped too — the same :func:`pixels_for_length_step` ``> 1`` rule the
    designers use for active-axis collapse, so a one-plane assigned axis
    (which emits no waveform) is not claimed as scanned. This also filters
    the 1-length dummy entries ``build_analog`` appends for non-scan axes.
    An entry with no corresponding length/step is kept (legacy behavior).

    Deliberately not consumed anywhere in ImSwitch (no ImProcess reader): it
    exists for the person or tool opening the file. See
    ``docs/galvo-designer-single-axis-findings.md``, phase C.
    """
    devices = []
    for i, dev in enumerate(positioners_scan):
        if not dev or dev == 'None':
            continue
        if (axis_lengths is not None and axis_step_sizes is not None
                and i < len(axis_lengths) and i < len(axis_step_sizes)
                and pixels_for_length_step(axis_lengths[i],
                                           axis_step_sizes[i]) <= 1):
            continue
        devices.append(dev)
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

        # Per-axis visit order (plan M2), aligned with target_device; only
        # written when an axis is not forward, so a forward scan's dict (and
        # its saved file) is unchanged.
        orders = [
            widget.getScanAxisOrder(name)
            if name in positionersScan and hasattr(widget, "getScanAxisOrder") else "forward"
            for name in analogParameterDict["target_device"]
        ]
        if any(order != "forward" for order in orders):
            analogParameterDict["axis_order"] = orders

        seq_time = widget.getSeqTimePar()
        analogParameterDict["sequence_time"] = seq_time
        # Only a widget without the field falls back to zero. A blank or
        # garbled entry raises: a bare except here used to turn a typo into a
        # silent zero-microsecond phase delay.
        try:
            analogParameterDict["phase_delay"] = widget.getPhaseDelayPar()
        except AttributeError:
            analogParameterDict["phase_delay"] = 0
        # The pause before a slice: only the old PointScan panel still has a
        # field for it; elsewhere it is the slice positioner's declared
        # settleTimeS, read by the designer, and no key is written.
        if hasattr(widget, "getd3StepDelayPar"):
            analogParameterDict["d3step_delay"] = widget.getd3StepDelayPar()

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

        # Per-device per-linestep power (%) for AO-capable lasers. The
        # enable flag is device-wide; missing support in older widgets defaults
        # to enabled to preserve historical scan behaviour.
        linestep_power_percent = {}
        linestep_power_enabled = {}
        for deviceName in ttl_devices.keys():
            try:
                vec = [float(widget.getLineStepPowerPercent(deviceName, s)) for s in range(S)]
                vec = [max(0.0, min(100.0, v)) for v in vec]
                linestep_power_percent[deviceName] = vec
            except Exception:
                pass
            try:
                linestep_power_enabled[deviceName] = bool(
                    widget.isLinestepPowerEnabled(deviceName)
                )
            except Exception:
                linestep_power_enabled[deviceName] = True

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
            "linestep_power_enabled": linestep_power_enabled,
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
            if hasattr(widget, "setScanAxisOrder"):
                # A scan saved without orders is forward on every axis.
                orders = analogParameterDict.get("axis_order") or []
                widget.setScanAxisOrder(
                    positionerName, orders[i] if i < len(orders) else "forward")

        # timing
        if "sequence_time" in digitalParameterDict:
            try:
                widget.setSeqTimePar(digitalParameterDict["sequence_time"])
            except Exception:
                pass
        # The delays are serialised by build_analog and were never written
        # back, so a calibrated phase delay came back as 0 after a restart and
        # the persisted value was overwritten by the next scan.
        for key, setter in (("phase_delay", "setPhaseDelayPar"),
                            ("d3step_delay", "setd3StepDelayPar")):
            if key in analogParameterDict and hasattr(widget, setter):
                try:
                    getattr(widget, setter)(analogParameterDict[key])
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

        # Older saved scans have no enable map and must load with modulation
        # enabled, matching the pre-checkbox behaviour.
        linestep_power_enabled = dig.get("linestep_power_enabled", None)
        for dev in ttl_devices.keys():
            try:
                enabled = True if linestep_power_enabled is None else bool(
                    (linestep_power_enabled or {}).get(dev, True)
                )
                widget.setLinestepPowerEnabled(dev, enabled)
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


__all__ = ["AdvancedScanParameterSerializer", "DWELL_ROUNDING", "GRID_RELATIVE_ERROR",
           "dwell_request_if_changed", "lag_report", "lag_samples_for",
           "pixels_for_length_step", "realized_dwell_s", "samples_for_duration"]


def seed_scan_delays_from_setup(widget, setupInfo) -> None:
    """Seed the panel's phase and D3-step delay fields from the setup file.

    Both are rig facts -- the galvo's response lag and the settle a slice
    change needs -- so they belong in ``scan.scanDesignerParams`` as
    ``phase_delay``/``d3step_delay`` (microseconds), not in a widget literal.
    So is a sensible first dwell, ``dwell_ms``, for a rig the panel's
    galvo-oriented default does not suit.
    The two panels used to open with different literals (100 and 0) for the
    same mirror. A persisted widget state applied later still wins, as it
    should: it is what the operator last calibrated.
    """
    scan = getattr(setupInfo, "scan", None)
    params = getattr(scan, "scanDesignerParams", None) or {}
    for key, setter in (("phase_delay", "setPhaseDelayPar"),
                        ("d3step_delay", "setd3StepDelayPar")):
        if key in params and hasattr(widget, setter):
            getattr(widget, setter)(params[key])
    # The first dwell (ms), when the panel's own (0.02 ms, made for galvos)
    # does not suit the rig: a stepped stage needs its move and settle inside
    # every pixel. Optional; designers ignore the key.
    if "dwell_ms" in params and hasattr(widget, "setSeqTimePar"):
        widget.setSeqTimePar(float(params["dwell_ms"]) / 1000.0)
