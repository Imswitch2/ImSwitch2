"""Simplified MoNaLISA acquisition plan over the Advanced scan backend.

This is deliberately only the acquisition half of the future beginner workflow:
there is no widefield overview here.  The setup owns the fixed microscope facts
(FOV, uLens period, laser identities and readout/camera dead times); the user
chooses scan dimensionality, sampling and the editable RESOLFT timing.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from .scan_cloak import PlanNotRepresentable, ScanCloak
from .scan_parameters import pixels_for_length_step


CONFIG_DEFAULTS: Dict[str, Any] = {
    'fovUm': (40.0, 40.0),
    'ulensPeriodUm': 0.72,
    'defaultZScanLengthUm': 1.0,
    'defaultScanType': 'XY',
    'defaultLateralStepUm': 0.04,
    'defaultZStepUm': 0.15,
    'defaultMode': 'resolft',
    'defaultOffTimeMs': 2.0,
    'defaultOnTimeMs': 1.0,
    'readoutTimeMs': 1.0,
    'interPulseDelayMs': 0.1,
    'cameraReadoutTimeMs': 6.0,
    'offLaser': None,
    'onLaser': None,
    'readoutLaser': None,
    'readoutDetector': None,
}

SCAN_TYPES = ('XY', 'YZ', 'XZ', 'XYZ')
MODES = ('resolft', 'confocal')


def _configured_tuple(value, *, default=(40.0, 40.0)) -> Tuple[float, float]:
    if value is None:
        value = default
    if isinstance(value, (int, float)):
        number = float(value)
        return number, number
    values = tuple(float(v) for v in value)
    if not values:
        return tuple(map(float, default))
    if len(values) == 1:
        return values[0], values[0]
    return values[0], values[1]


def _first_named(mapping, token: str) -> Optional[str]:
    token = token.upper()
    for name in mapping:
        if token in str(name).upper():
            return str(name)
    return None


def _round_um(value: float) -> float:
    return round(float(value), 3)


def _snap_time_s(value_s: float, sample_rate: float) -> float:
    samples = max(0, round(float(value_s) * float(sample_rate)))
    return samples / float(sample_rate)


@dataclass(frozen=True)
class MoNaLISALimits:
    """Fixed microscope/setup facts used by the simplified MoNaLISA panel."""

    axis_devices: Mapping[str, str]       # logical X/Y/Z -> positioner name
    ttl_devices: Tuple[str, ...]
    sample_rate: float
    fov_um: Tuple[float, float]
    ulens_period_um: float
    off_laser: Optional[str]
    on_laser: Optional[str]
    readout_laser: Optional[str]
    readout_detector: Optional[str]
    readout_time_s: float
    inter_pulse_delay_s: float
    camera_readout_time_s: float
    return_time_s: float
    default_scan_type: str
    default_lateral_step_um: float
    default_z_scan_length_um: float
    default_z_step_um: float
    default_mode: str
    default_off_time_s: float
    default_on_time_s: float

    @classmethod
    def from_setup(cls, setupInfo) -> 'MoNaLISALimits':
        raw = dict(CONFIG_DEFAULTS)
        configured = getattr(setupInfo.scan, 'simpleMoNaLISA', None) or {}
        raw.update(configured)
        # Compatibility with the first visual prototype, where Z span was a
        # fixed setup fact.  It is now an editable plan value, but an existing
        # zScanLengthUm still makes a sensible initial value.
        if 'defaultZScanLengthUm' not in configured and 'zScanLengthUm' in configured:
            raw['defaultZScanLengthUm'] = configured['zScanLengthUm']

        axes: Dict[str, str] = {}
        for name, info in setupInfo.positioners.items():
            if not getattr(info, 'forScanning', False):
                continue
            labels = list(getattr(info, 'axes', None) or [])
            logical = str(labels[0] if labels else name).upper()
            if logical in ('X', 'Y', 'Z') and logical not in axes:
                axes[logical] = str(name)

        lasers = getattr(setupInfo, 'lasers', None) or {}
        detectors = getattr(setupInfo, 'detectors', None) or {}
        ttl = tuple(setupInfo.getTTLDevices())

        off_laser = raw.get('offLaser') or _first_named(lasers, 'OFF')
        on_laser = raw.get('onLaser') or _first_named(lasers, 'ON')
        readout_laser = raw.get('readoutLaser') or _first_named(lasers, 'EXC')
        readout_detector = raw.get('readoutDetector')
        if readout_detector is None:
            readout_detector = next(
                (str(name) for name, info in detectors.items()
                 if getattr(info, 'forAcquisition', False) and name in ttl),
                None,
            )

        scan_type = str(raw.get('defaultScanType') or 'XY').upper()
        if scan_type not in SCAN_TYPES:
            scan_type = 'XY'
        mode = str(raw.get('defaultMode') or 'resolft').lower()
        if mode not in MODES:
            mode = 'resolft'

        period = max(0.001, float(raw.get('ulensPeriodUm') or CONFIG_DEFAULTS['ulensPeriodUm']))

        designer = getattr(setupInfo.scan, 'scanDesignerParams', None) or {}
        return cls(
            axis_devices=axes,
            ttl_devices=ttl,
            sample_rate=float(setupInfo.scan.sampleRate),
            fov_um=_configured_tuple(raw.get('fovUm')),
            ulens_period_um=period,
            off_laser=str(off_laser) if off_laser else None,
            on_laser=str(on_laser) if on_laser else None,
            readout_laser=str(readout_laser) if readout_laser else None,
            readout_detector=str(readout_detector) if readout_detector else None,
            readout_time_s=max(0.0, float(raw.get('readoutTimeMs') or 0.0) * 1e-3),
            inter_pulse_delay_s=max(0.0, float(raw.get('interPulseDelayMs') or 0.0) * 1e-3),
            camera_readout_time_s=max(0.0, float(raw.get('cameraReadoutTimeMs') or 0.0) * 1e-3),
            return_time_s=max(0.0, float(designer.get('return_time', 0.0) or 0.0)),
            default_scan_type=scan_type,
            default_lateral_step_um=max(0.001, float(raw.get('defaultLateralStepUm') or 0.04)),
            default_z_scan_length_um=max(
                0.001, float(raw.get('defaultZScanLengthUm') or 1.0)
            ),
            default_z_step_um=max(0.001, float(raw.get('defaultZStepUm') or 0.15)),
            default_mode=mode,
            default_off_time_s=max(0.0, float(raw.get('defaultOffTimeMs') or 0.0) * 1e-3),
            default_on_time_s=max(0.0, float(raw.get('defaultOnTimeMs') or 0.0) * 1e-3),
        )

    def axis_device(self, logical_axis: str) -> str:
        try:
            return self.axis_devices[logical_axis.upper()]
        except KeyError as exc:
            raise PlanNotRepresentable(
                f'The SimpleMoNaLISA panel needs a scanning {logical_axis.upper()} axis.'
            ) from exc

    def missing_setup_items(self) -> Tuple[str, ...]:
        missing = []
        for axis in ('X', 'Y', 'Z'):
            if axis not in self.axis_devices:
                missing.append(f'{axis} scanning axis')
        for label, name in (
            ('OFF laser', self.off_laser),
            ('ON laser', self.on_laser),
            ('read-out laser', self.readout_laser),
        ):
            if not name:
                missing.append(label)
            elif name not in self.ttl_devices:
                missing.append(f'{label} {name!r} with a digital line')
        if self.readout_detector and self.readout_detector not in self.ttl_devices:
            missing.append(f'read-out detector {self.readout_detector!r} with a digital line')
        return tuple(missing)


@dataclass(frozen=True)
class MoNaLISAScanPlan:
    scan_type: str
    lateral_step_um: float
    z_length_um: float
    z_step_um: float
    mode: str
    off_time_s: float
    on_time_s: float

    @property
    def axes(self) -> Tuple[str, ...]:
        return tuple(self.scan_type)

    def dwell_s(self, limits: MoNaLISALimits) -> float:
        optical = self.on_time_s + limits.readout_time_s
        # One delay precedes the first pulse and another separates each
        # subsequent active pulse.
        gaps = 2
        if self.mode == 'resolft':
            optical += self.off_time_s
            gaps += 1
        return (
            optical
            + gaps * limits.inter_pulse_delay_s
            + limits.camera_readout_time_s
        )

    def pixels(self, limits: MoNaLISALimits) -> Tuple[int, ...]:
        result = []
        for axis in self.axes:
            step = self.z_step_um if axis == 'Z' else self.lateral_step_um
            length = self.z_length_um if axis == 'Z' else limits.ulens_period_um
            result.append(pixels_for_length_step(length, step))
        return tuple(result)

    def estimate_s(self, limits: MoNaLISALimits) -> float:
        pixels = list(self.pixels(limits))
        while len(pixels) < 3:
            pixels.append(1)
        fast, middle, slow = pixels[:3]
        lines = middle * slow
        return fast * lines * self.dwell_s(limits) + lines * limits.return_time_s

    def to_dict(self) -> dict:
        return {
            'scan_type': self.scan_type,
            'lateral_step_um': self.lateral_step_um,
            'z_length_um': self.z_length_um,
            'z_step_um': self.z_step_um,
            'mode': self.mode,
            'off_time_s': self.off_time_s,
            'on_time_s': self.on_time_s,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'MoNaLISAScanPlan':
        return cls(
            scan_type=str(data['scan_type']).upper(),
            lateral_step_um=float(data['lateral_step_um']),
            z_length_um=float(data.get('z_length_um', 1.0)),
            z_step_um=float(data['z_step_um']),
            mode=str(data['mode']).lower(),
            off_time_s=float(data['off_time_s']),
            on_time_s=float(data['on_time_s']),
        )


def normalize_plan(plan: MoNaLISAScanPlan, limits: MoNaLISALimits) -> MoNaLISAScanPlan:
    scan_type = str(plan.scan_type).upper()
    if scan_type not in SCAN_TYPES:
        scan_type = limits.default_scan_type
    mode = str(plan.mode).lower()
    if mode not in MODES:
        mode = limits.default_mode
    return dataclasses.replace(
        plan,
        scan_type=scan_type,
        mode=mode,
        lateral_step_um=max(0.001, _round_um(plan.lateral_step_um)),
        z_length_um=max(0.001, _round_um(plan.z_length_um)),
        z_step_um=max(0.001, _round_um(plan.z_step_um)),
        off_time_s=_snap_time_s(max(0.0, plan.off_time_s), limits.sample_rate),
        on_time_s=_snap_time_s(max(0.0, plan.on_time_s), limits.sample_rate),
    )


def default_plan(limits: MoNaLISALimits) -> MoNaLISAScanPlan:
    return normalize_plan(MoNaLISAScanPlan(
        scan_type=limits.default_scan_type,
        lateral_step_um=limits.default_lateral_step_um,
        z_length_um=limits.default_z_scan_length_um,
        z_step_um=limits.default_z_step_um,
        mode=limits.default_mode,
        off_time_s=limits.default_off_time_s,
        on_time_s=limits.default_on_time_s,
    ), limits)


def _append_window(windows: Dict[str, list], device: Optional[str], start: float, end: float):
    if not device or end <= start:
        return
    windows.setdefault(device, []).append((float(start), float(end)))


def plan_to_dicts(plan: MoNaLISAScanPlan, limits: MoNaLISALimits) -> Tuple[dict, dict]:
    plan = normalize_plan(plan, limits)
    missing = limits.missing_setup_items()
    if missing:
        raise PlanNotRepresentable('Setup is missing: ' + ', '.join(missing) + '.')

    logical_active = list(plan.axes)
    for axis in logical_active:
        limits.axis_device(axis)
    logical_all = logical_active + [axis for axis in ('X', 'Y', 'Z') if axis not in logical_active]
    device_order = [limits.axis_device(axis) for axis in logical_all]

    analog: Dict[str, Any] = {
        'target_device': [],
        'axis_length': [],
        'axis_step_size': [],
        'axis_centerpos': [],
        'axis_startpos': [],
        'scan_dim_target_device': [limits.axis_device(axis) for axis in logical_active]
                                  + ['None'] * (3 - len(logical_active)),
    }
    for axis, device in zip(logical_all, device_order):
        active = axis in logical_active
        length = plan.z_length_um if axis == 'Z' else limits.ulens_period_um
        analog['target_device'].append(device)
        analog['axis_length'].append(float(length) if active else 0.0)
        analog['axis_step_size'].append(
            float(plan.z_step_um if axis == 'Z' else plan.lateral_step_um) if active else 1.0
        )
        # Zero is a relative offset from the position snapshot with Beta's
        # default position_anchor='center'.
        analog['axis_centerpos'].append(0.0)
        analog['axis_startpos'].append([0.0])
    dwell = plan.dwell_s(limits)
    analog['sequence_time'] = dwell
    analog['phase_delay'] = 0.0
    analog['d3step_delay'] = 0.0

    windows: Dict[str, list] = {}
    # MoNaLISA sequence: initial delay -> ON -> delay -> [OFF -> delay ->] read-out.
    t = limits.inter_pulse_delay_s
    _append_window(windows, limits.on_laser, t, t + plan.on_time_s)
    t += plan.on_time_s + limits.inter_pulse_delay_s
    if plan.mode == 'resolft':
        _append_window(windows, limits.off_laser, t, t + plan.off_time_s)
        t += plan.off_time_s + limits.inter_pulse_delay_s
    _append_window(windows, limits.readout_laser, t, t + limits.readout_time_s)
    _append_window(windows, limits.readout_detector, t, t + limits.readout_time_s)

    targets = [name for name in limits.ttl_devices if name in windows]
    # Name every TTL device in the widget-state maps so switching modes also
    # clears pulses/enables left by the previous plan. target_device remains
    # only the devices actually armed for this scan.
    starts = {name: [[]] for name in limits.ttl_devices}
    ends = {name: [[]] for name in limits.ttl_devices}
    enabled = {name: [False] for name in limits.ttl_devices}
    for name in targets:
        starts[name] = [[start for start, _ in windows[name]]]
        ends[name] = [[end for _, end in windows[name]]]
        enabled[name] = [True]

    pixels = plan.pixels(limits)
    digital: Dict[str, Any] = {
        'target_device': targets,
        'n_linesteps': 1,
        'Nx': pixels[0] if pixels else 1,
        'Ny': pixels[1] if len(pixels) > 1 else 1,
        'linestep_enable': enabled,
        'pulse_starts_s': starts,
        'pulse_ends_s': ends,
        'sequence_time': dwell,
        'advanced_mode': True,
        'linestep_power_percent': {},
        'linestep_power_enabled': {},
        'intra_pixel_positioner_movement': False,
        'positioner_target_device': [],
        'positioner_linestep_enable': {},
        'positioner_movement_starts_s': {},
        'positioner_movement_ends_s': {},
        'positioner_step_size_um': {},
        'advanced_program_mode': 'timing',
        'advanced_sequence_rows': {},
        'line_program_devices_enabled': True,
        'advanced_device_lock_master': {},
        'advanced_device_lock_target': {},
    }
    return analog, digital


def _windows(digital: Mapping[str, Any], device: Optional[str]) -> Tuple[Tuple[float, float], ...]:
    """Return all timing windows for one device in the first line-step.

    One physical laser can legitimately have more than one semantic role in a
    MoNaLISA sequence (for example OFF and read-out can both be 488 nm).  The
    simple cloak therefore cannot assume one window per device; roles are
    identified from the ordered sequence below.
    """
    if not device:
        return ()
    enabled = (digital.get('linestep_enable') or {}).get(device)
    if enabled is not None and len(enabled) > 0 and not bool(enabled[0]):
        return ()
    starts = (digital.get('pulse_starts_s') or {}).get(device) or []
    ends = (digital.get('pulse_ends_s') or {}).get(device) or []
    if not starts or not ends:
        return ()
    starts0 = starts[0] if isinstance(starts[0], (list, tuple)) else starts
    ends0 = ends[0] if isinstance(ends[0], (list, tuple)) else ends
    if len(starts0) != len(ends0):
        raise PlanNotRepresentable(f'{device} has unmatched timing-window starts and ends.')
    result = tuple(sorted(
        (float(start), float(end))
        for start, end in zip(starts0, ends0)
        if float(end) > float(start)
    ))
    return result


def _matching_window(
    windows: Tuple[Tuple[float, float], ...],
    *,
    duration: Optional[float] = None,
    end_at: Optional[float] = None,
    prefer_latest: bool = False,
    tolerance: float,
) -> Optional[Tuple[float, float]]:
    matches = []
    for window in windows:
        start, end = window
        if duration is not None and not math.isclose(end - start, duration, abs_tol=tolerance):
            continue
        if end_at is not None and not math.isclose(end, end_at, abs_tol=tolerance):
            continue
        matches.append(window)
    if not matches:
        return None
    return matches[-1] if prefer_latest else matches[0]


def dicts_to_plan(analog: Mapping[str, Any], digital: Mapping[str, Any],
                  limits: MoNaLISALimits) -> MoNaLISAScanPlan:
    digital = digital or {}
    if int(digital.get('n_linesteps', 1)) != 1:
        raise PlanNotRepresentable('SimpleMoNaLISA represents one sequence per scan position.')
    if digital.get('advanced_program_mode') == 'sequence' and digital.get('advanced_sequence_rows'):
        raise PlanNotRepresentable('This scan uses Sequence Builder rows.')
    if digital.get('intra_pixel_positioner_movement'):
        raise PlanNotRepresentable('This scan moves a positioner within the pixel.')
    if any((digital.get('advanced_device_lock_master') or {}).values()):
        raise PlanNotRepresentable('This scan locks devices together in Advanced.')

    reverse_axes = {device: axis for axis, device in limits.axis_devices.items()}
    dims = [name for name in (analog.get('scan_dim_target_device') or ()) if name and name != 'None']
    try:
        logical = ''.join(reverse_axes[name] for name in dims)
    except KeyError as exc:
        raise PlanNotRepresentable(f'This scan uses an unknown scan axis {exc.args[0]!r}.') from exc
    if logical not in SCAN_TYPES:
        raise PlanNotRepresentable(
            f'This scan uses dimensions {logical or "none"}; choose XY, XZ, YZ or XYZ.'
        )

    devices = list(analog.get('target_device') or ())
    lengths = list(analog.get('axis_length') or ())
    steps = list(analog.get('axis_step_size') or ())
    by_device = {name: i for i, name in enumerate(devices)}
    lateral_steps = []
    z_length = limits.default_z_scan_length_um
    z_step = limits.default_z_step_um
    for axis in logical:
        device = limits.axis_device(axis)
        if device not in by_device:
            raise PlanNotRepresentable(f'This scan does not contain the configured {axis} axis.')
        index = by_device[device]
        if index >= len(lengths):
            raise PlanNotRepresentable(f'This scan has no length for the configured {axis} axis.')
        length = float(lengths[index])
        if axis == 'Z':
            if length <= 0:
                raise PlanNotRepresentable('Z scan length must be positive.')
            z_length = length
        elif not math.isclose(length, limits.ulens_period_um, abs_tol=1e-3):
            raise PlanNotRepresentable(
                f'{axis} scan length must stay at the uLens period '
                f'{limits.ulens_period_um:g} µm.'
            )
        step = float(steps[index])
        if axis == 'Z':
            z_step = step
        else:
            lateral_steps.append(step)
    if lateral_steps and any(not math.isclose(step, lateral_steps[0], abs_tol=1e-3)
                             for step in lateral_steps[1:]):
        raise PlanNotRepresentable('X and Y must use the same lateral step in SimpleMoNaLISA.')
    lateral = lateral_steps[0] if lateral_steps else limits.default_lateral_step_um

    targets = set(digital.get('target_device') or ())
    allowed = {name for name in (
        limits.off_laser, limits.on_laser, limits.readout_laser, limits.readout_detector
    ) if name}
    strangers = sorted(targets - allowed)
    if strangers:
        raise PlanNotRepresentable(
            'This scan fires devices outside the configured MoNaLISA sequence: '
            + ', '.join(strangers) + '.'
        )

    tolerance = 1.0 / limits.sample_rate + 1e-9
    by_ttl_device = {
        name: _windows(digital, name)
        for name in allowed
    }

    # Parse by sequence role rather than by "one window per device".  This is
    # what lets the same 488 laser act as both OFF and read-out without making
    # the simple view ambiguous after visiting Advanced.
    readout = _matching_window(
        by_ttl_device.get(limits.readout_laser, ()),
        duration=limits.readout_time_s,
        prefer_latest=True,
        tolerance=tolerance,
    )
    if readout is None:
        raise PlanNotRepresentable('This scan does not contain the configured read-out pulse.')

    # RESOLFT is ON -> OFF -> read-out; confocal omits the OFF pulse.
    # The setup-configured delay also precedes the first ON pulse.
    off = _matching_window(
        by_ttl_device.get(limits.off_laser, ()),
        end_at=readout[0] - limits.inter_pulse_delay_s,
        prefer_latest=True,
        tolerance=tolerance,
    )
    next_after_on = off[0] if off is not None else readout[0]
    on = _matching_window(
        by_ttl_device.get(limits.on_laser, ()),
        end_at=next_after_on - limits.inter_pulse_delay_s,
        prefer_latest=True,
        tolerance=tolerance,
    )
    if on is None:
        raise PlanNotRepresentable('This scan does not contain the configured ON sequence.')
    if not math.isclose(on[0], limits.inter_pulse_delay_s, abs_tol=tolerance):
        raise PlanNotRepresentable(
            'The first ON pulse must start after the setup-configured initial delay.'
        )

    camera = None
    if limits.readout_detector:
        camera = _matching_window(
            by_ttl_device.get(limits.readout_detector, ()),
            duration=limits.readout_time_s,
            prefer_latest=True,
            tolerance=tolerance,
        )
        if camera is None or not (
            math.isclose(camera[0], readout[0], abs_tol=tolerance)
            and math.isclose(camera[1], readout[1], abs_tol=tolerance)
        ):
            raise PlanNotRepresentable(
                'The configured camera pulse must overlap the read-out pulse.'
            )

    mode = 'resolft' if off is not None else 'confocal'
    off_time = max(0.0, off[1] - off[0]) if off else limits.default_off_time_s
    on_time = max(0.0, on[1] - on[0])
    expected_readout = limits.readout_time_s
    if not math.isclose(readout[1] - readout[0], expected_readout, abs_tol=1.0 / limits.sample_rate + 1e-9):
        raise PlanNotRepresentable(
            f'Read-out time is fixed by setup at {expected_readout * 1e3:g} ms.'
        )

    recognized = {}
    for device, window in (
        (limits.off_laser, off),
        (limits.on_laser, on),
        (limits.readout_laser, readout),
        (limits.readout_detector, camera),
    ):
        if device and window is not None:
            recognized.setdefault(device, []).append(window)
    for device, windows in by_ttl_device.items():
        known = recognized.get(device, ())
        extras = [
            window for window in windows
            if not any(
                math.isclose(window[0], expected[0], abs_tol=tolerance)
                and math.isclose(window[1], expected[1], abs_tol=tolerance)
                for expected in known
            )
        ]
        if extras:
            raise PlanNotRepresentable(
                f'{device} has extra timing windows that SimpleMoNaLISA cannot show.'
            )

    plan = MoNaLISAScanPlan(
        scan_type=logical,
        lateral_step_um=lateral,
        z_length_um=z_length,
        z_step_um=z_step,
        mode=mode,
        off_time_s=off_time,
        on_time_s=on_time,
    )
    normalized = normalize_plan(plan, limits)
    if not math.isclose(float(analog.get('sequence_time', normalized.dwell_s(limits))),
                        normalized.dwell_s(limits),
                        abs_tol=1.0 / limits.sample_rate + 1e-9):
        raise PlanNotRepresentable(
            'Pixel dwell no longer matches initial delay + ON + optional OFF + read-out + camera readout.'
        )
    return normalized


class MoNaLISAScanCloak(ScanCloak):
    """Simplified MoNaLISA acquisition over the Advanced scan panel."""

    def limits_from_setup(self, setupInfo) -> MoNaLISALimits:
        return MoNaLISALimits.from_setup(setupInfo)

    def to_backend(self, plan, limits):
        return plan_to_dicts(plan, limits)

    def from_backend(self, analog, digital, limits):
        return dicts_to_plan(analog, digital, limits)

    def normalize(self, plan):
        # The limits are needed for timing quantisation; controllers call the
        # explicit helper before storing plans, so this method is intentionally
        # a light canonicalisation for the generic contract.
        return dataclasses.replace(
            plan,
            scan_type=str(plan.scan_type).upper(),
            mode=str(plan.mode).lower(),
            lateral_step_um=max(0.001, _round_um(plan.lateral_step_um)),
            z_length_um=max(0.001, _round_um(plan.z_length_um)),
            z_step_um=max(0.001, _round_um(plan.z_step_um)),
        )

    def plan_to_dict(self, plan) -> dict:
        return plan.to_dict()

    def plan_from_dict(self, data):
        return MoNaLISAScanPlan.from_dict(data)


__all__ = [
    'CONFIG_DEFAULTS', 'MODES', 'SCAN_TYPES', 'MoNaLISALimits', 'MoNaLISAScanCloak',
    'MoNaLISAScanPlan', 'default_plan', 'dicts_to_plan', 'normalize_plan', 'plan_to_dicts',
]
