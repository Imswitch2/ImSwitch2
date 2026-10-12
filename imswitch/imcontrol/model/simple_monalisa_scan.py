"""Simplified MoNaLISA acquisition over the Advanced scan backend.

The scan geometry is setup constrained, while illumination is described by a
selected :class:`MoNaLISAProfileDefinition` and one of its modes.  Profiles are
ordered sequences of ON/OFF/READOUT steps; roles are semantic metadata and may
repeat.  The current plan stores live durations for every profile step, even
when a mode temporarily disables some of them.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from .scan_cloak import PlanNotRepresentable, ScanCloak
from .scan_parameters import pixels_for_length_step
from .monalisa_profiles import (
    MoNaLISAProfileError,
    MoNaLISAProfileStore,
    MoNaLISASequenceNotCompatible,
    compile_profile_sequence,
    parse_profile_sequence,
)


CONFIG_DEFAULTS: Dict[str, Any] = {
    'fovUm': (40.0, 40.0),
    'ulensPeriodUm': 0.72,
    'defaultZScanLengthUm': 1.0,
    'defaultScanType': 'XY',
    'defaultLateralStepUm': 0.04,
    'defaultZStepUm': 0.15,
    'interPulseDelayMs': 0.1,
    'cameraReadoutTimeMs': 6.0,
}

SCAN_TYPES = ('XY', 'YZ', 'XZ', 'XYZ')


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



def _round_um(value: float) -> float:
    return round(float(value), 3)


def _snap_time_s(value_s: float, sample_rate: float) -> float:
    samples = max(0, round(float(value_s) * float(sample_rate)))
    return samples / float(sample_rate)


@dataclass(frozen=True)
class MoNaLISALimits:
    """Fixed microscope/setup facts used by the simplified MoNaLISA panel."""

    axis_devices: Mapping[str, str]
    ttl_devices: Tuple[str, ...]
    sample_rate: float
    fov_um: Tuple[float, float]
    ulens_period_um: float
    profile_store: MoNaLISAProfileStore
    inter_pulse_delay_s: float
    camera_readout_time_s: float
    return_time_s: float
    default_scan_type: str
    default_lateral_step_um: float
    default_z_scan_length_um: float
    default_z_step_um: float

    @classmethod
    def from_setup(cls, setupInfo) -> 'MoNaLISALimits':
        raw = dict(CONFIG_DEFAULTS)
        configured = getattr(setupInfo.scan, 'simpleMoNaLISA', None) or {}
        raw.update(configured)
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

        ttl = tuple(setupInfo.getTTLDevices())
        profile_store = MoNaLISAProfileStore.from_config(configured)

        scan_type = str(raw.get('defaultScanType') or 'XY').upper()
        if scan_type not in SCAN_TYPES:
            scan_type = 'XY'
        period = max(0.001, float(raw.get('ulensPeriodUm') or CONFIG_DEFAULTS['ulensPeriodUm']))
        designer = getattr(setupInfo.scan, 'scanDesignerParams', None) or {}
        return cls(
            axis_devices=axes,
            ttl_devices=ttl,
            sample_rate=float(setupInfo.scan.sampleRate),
            fov_um=_configured_tuple(raw.get('fovUm')),
            ulens_period_um=period,
            profile_store=profile_store,
            inter_pulse_delay_s=max(0.0, float(raw.get('interPulseDelayMs') or 0.0) * 1e-3),
            camera_readout_time_s=max(0.0, float(raw.get('cameraReadoutTimeMs') or 0.0) * 1e-3),
            return_time_s=max(0.0, float(designer.get('return_time', 0.0) or 0.0)),
            default_scan_type=scan_type,
            default_lateral_step_um=max(0.001, float(raw.get('defaultLateralStepUm') or 0.04)),
            default_z_scan_length_um=max(0.001, float(raw.get('defaultZScanLengthUm') or 1.0)),
            default_z_step_um=max(0.001, float(raw.get('defaultZStepUm') or 0.15)),
        )

    def axis_device(self, logical_axis: str) -> str:
        try:
            return self.axis_devices[logical_axis.upper()]
        except KeyError as exc:
            raise PlanNotRepresentable(
                f'The SimpleMoNaLISA panel needs a scanning {logical_axis.upper()} axis.'
            ) from exc

    def missing_setup_items(self, profile_id: Optional[str] = None) -> Tuple[str, ...]:
        missing = []
        for axis in ('X', 'Y', 'Z'):
            if axis not in self.axis_devices:
                missing.append(f'{axis} scanning axis')
        selected = profile_id or self.profile_store.default_profile
        if selected:
            missing.extend(self.profile_store.missing_devices(
                self.ttl_devices, profile_id=selected
            ))
        return tuple(missing)


@dataclass(frozen=True)
class MoNaLISAScanPlan:
    scan_type: str
    lateral_step_um: float
    z_length_um: float
    z_step_um: float
    profile_id: str
    mode_id: str
    step_durations_s: Mapping[str, float]

    @property
    def axes(self) -> Tuple[str, ...]:
        return tuple(self.scan_type)

    def profile(self, limits: MoNaLISALimits):
        return limits.profile_store.profile(self.profile_id)

    def dwell_s(self, limits: MoNaLISALimits) -> float:
        compiled = compile_plan_sequence(self, limits)
        return compiled.end_time_s + limits.camera_readout_time_s

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
            'profile_id': self.profile_id,
            'mode_id': self.mode_id,
            'step_durations_s': dict(self.step_durations_s),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'MoNaLISAScanPlan':
        return cls(
            scan_type=str(data['scan_type']).upper(),
            lateral_step_um=float(data['lateral_step_um']),
            z_length_um=float(data.get('z_length_um', 1.0)),
            z_step_um=float(data['z_step_um']),
            profile_id=str(data['profile_id']),
            mode_id=str(data['mode_id']),
            step_durations_s={
                str(key): float(value)
                for key, value in (data.get('step_durations_s') or {}).items()
            },
        )


def normalize_plan(plan: MoNaLISAScanPlan, limits: MoNaLISALimits) -> MoNaLISAScanPlan:
    scan_type = str(plan.scan_type).upper()
    if scan_type not in SCAN_TYPES:
        scan_type = limits.default_scan_type
    try:
        profile = limits.profile_store.profile(plan.profile_id)
    except MoNaLISAProfileError as exc:
        raise PlanNotRepresentable(str(exc)) from exc
    mode_id = str(plan.mode_id)
    try:
        profile.mode(mode_id)
    except MoNaLISAProfileError as exc:
        raise PlanNotRepresentable(str(exc)) from exc

    durations = {}
    supplied = dict(plan.step_durations_s or {})
    for step in profile.steps:
        requested = supplied.get(step.id, step.default_duration_s)
        if not step.duration_editable:
            requested = step.default_duration_s
        duration = _snap_time_s(max(0.0, float(requested)), limits.sample_rate)
        if duration <= 0:
            raise PlanNotRepresentable(
                f'Step {step.label!r} must have a positive duration.'
            )
        durations[step.id] = duration

    return dataclasses.replace(
        plan,
        scan_type=scan_type,
        profile_id=profile.id,
        mode_id=mode_id,
        step_durations_s=durations,
        lateral_step_um=max(0.001, _round_um(plan.lateral_step_um)),
        z_length_um=max(0.001, _round_um(plan.z_length_um)),
        z_step_um=max(0.001, _round_um(plan.z_step_um)),
    )


def default_plan(limits: MoNaLISALimits) -> MoNaLISAScanPlan:
    profile = limits.profile_store.profile()
    return normalize_plan(MoNaLISAScanPlan(
        scan_type=limits.default_scan_type,
        lateral_step_um=limits.default_lateral_step_um,
        z_length_um=limits.default_z_scan_length_um,
        z_step_um=limits.default_z_step_um,
        profile_id=profile.id,
        mode_id=profile.default_mode,
        step_durations_s={step.id: step.default_duration_s for step in profile.steps},
    ), limits)


def adapt_plan_to_profile_definition(
    plan: MoNaLISAScanPlan,
    profile,
    limits: MoNaLISALimits,
) -> MoNaLISAScanPlan:
    """Adapt the live plan after an explicit edit of its active profile.

    Geometry is preserved. Durations are preserved for unchanged editable step
    ids, new steps use profile defaults, and fixed-duration steps always adopt
    their new profile default. The current mode is kept when it still exists.
    """
    mode_id = plan.mode_id if plan.mode_id in profile.modes else profile.default_mode
    durations = {}
    current = dict(plan.step_durations_s or {})
    for step in profile.steps:
        if step.duration_editable and step.id in current:
            durations[step.id] = current[step.id]
        else:
            durations[step.id] = step.default_duration_s
    return normalize_plan(dataclasses.replace(
        plan,
        profile_id=profile.id,
        mode_id=mode_id,
        step_durations_s=durations,
    ), limits)


def select_profile(
    plan: MoNaLISAScanPlan,
    profile_id: str,
    limits: MoNaLISALimits,
    *,
    preserve_if_same: bool = True,
) -> MoNaLISAScanPlan:
    profile = limits.profile_store.profile(profile_id)
    if preserve_if_same and profile.id == plan.profile_id:
        durations = dict(plan.step_durations_s)
        mode_id = plan.mode_id if plan.mode_id in profile.modes else profile.default_mode
    else:
        durations = {step.id: step.default_duration_s for step in profile.steps}
        mode_id = profile.default_mode
    return normalize_plan(dataclasses.replace(
        plan,
        profile_id=profile.id,
        mode_id=mode_id,
        step_durations_s=durations,
    ), limits)


def select_mode(
    plan: MoNaLISAScanPlan, mode_id: str, limits: MoNaLISALimits
) -> MoNaLISAScanPlan:
    profile = limits.profile_store.profile(plan.profile_id)
    profile.mode(mode_id)
    return normalize_plan(dataclasses.replace(plan, mode_id=str(mode_id)), limits)


def compile_plan_sequence(plan: MoNaLISAScanPlan, limits: MoNaLISALimits):
    plan = normalize_plan(plan, limits)
    try:
        return compile_profile_sequence(
            limits.profile_store.profile(plan.profile_id),
            plan.mode_id,
            plan.step_durations_s,
            inter_pulse_delay_s=limits.inter_pulse_delay_s,
            sample_rate=limits.sample_rate,
        )
    except MoNaLISAProfileError as exc:
        raise PlanNotRepresentable(str(exc)) from exc


def _parse_geometry(
    analog: Mapping[str, Any], limits: MoNaLISALimits
) -> Tuple[str, float, float, float]:
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
        if index >= len(lengths) or index >= len(steps):
            raise PlanNotRepresentable(f'This scan has incomplete geometry for the configured {axis} axis.')
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
    return logical, lateral, z_length, z_step


def plan_with_backend_geometry(
    plan: MoNaLISAScanPlan, analog: Mapping[str, Any], limits: MoNaLISALimits
) -> MoNaLISAScanPlan:
    logical, lateral, z_length, z_step = _parse_geometry(analog, limits)
    return normalize_plan(dataclasses.replace(
        plan,
        scan_type=logical,
        lateral_step_um=lateral,
        z_length_um=z_length,
        z_step_um=z_step,
    ), limits)


def plan_to_dicts(plan: MoNaLISAScanPlan, limits: MoNaLISALimits) -> Tuple[dict, dict]:
    plan = normalize_plan(plan, limits)
    missing = limits.missing_setup_items(plan.profile_id)
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
        analog['axis_centerpos'].append(0.0)
        analog['axis_startpos'].append([0.0])

    dwell = plan.dwell_s(limits)
    analog['sequence_time'] = dwell
    analog['phase_delay'] = 0.0
    analog['d3step_delay'] = 0.0

    compiled = compile_plan_sequence(plan, limits)
    windows = compiled.windows_by_device
    targets = [name for name in limits.ttl_devices if name in windows]
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


def dicts_to_plan(
    analog: Mapping[str, Any],
    digital: Mapping[str, Any],
    limits: MoNaLISALimits,
    reference_plan: Optional[MoNaLISAScanPlan] = None,
) -> MoNaLISAScanPlan:
    """Parse Advanced against exactly the profile/mode in ``reference_plan``.

    No other profile or mode is searched.  This is the model-level invariant
    that prevents Advanced edits from silently changing the Simple binding.
    """
    digital = digital or {}
    if int(digital.get('n_linesteps', 1)) != 1:
        raise PlanNotRepresentable('SimpleMoNaLISA represents one sequence per scan position.')
    if digital.get('advanced_program_mode') == 'sequence' and digital.get('advanced_sequence_rows'):
        raise PlanNotRepresentable('This scan uses Sequence Builder rows.')
    if digital.get('intra_pixel_positioner_movement'):
        raise PlanNotRepresentable('This scan moves a positioner within the pixel.')
    if any((digital.get('advanced_device_lock_master') or {}).values()):
        raise PlanNotRepresentable('This scan locks devices together in Advanced.')

    reference = normalize_plan(reference_plan or default_plan(limits), limits)
    geometry = plan_with_backend_geometry(reference, analog, limits)
    profile = limits.profile_store.profile(reference.profile_id)
    try:
        parsed = parse_profile_sequence(
            digital,
            profile,
            reference.mode_id,
            inter_pulse_delay_s=limits.inter_pulse_delay_s,
            sample_rate=limits.sample_rate,
        )
    except MoNaLISASequenceNotCompatible as exc:
        raise PlanNotRepresentable(str(exc)) from exc

    durations = dict(reference.step_durations_s)
    durations.update(parsed.step_durations_s)
    plan = normalize_plan(dataclasses.replace(
        geometry,
        step_durations_s=durations,
    ), limits)
    expected_dwell = plan.dwell_s(limits)
    if not math.isclose(
        float(analog.get('sequence_time', expected_dwell)),
        expected_dwell,
        abs_tol=1.0 / limits.sample_rate + 1e-9,
    ):
        raise PlanNotRepresentable(
            'Pixel dwell no longer matches the selected profile/mode sequence and camera readout.'
        )
    return plan


class MoNaLISAScanCloak(ScanCloak):
    """Simplified MoNaLISA acquisition over the Advanced scan panel."""

    def limits_from_setup(self, setupInfo) -> MoNaLISALimits:
        return MoNaLISALimits.from_setup(setupInfo)

    def to_backend(self, plan, limits):
        return plan_to_dicts(plan, limits)

    def from_backend(self, analog, digital, limits):
        # Generic cloak contract: round-trip the setup's default binding.  The
        # controller uses ``dicts_to_plan(..., reference_plan=...)`` so an
        # explicit user selection is never inferred from Advanced.
        return dicts_to_plan(analog, digital, limits, default_plan(limits))

    def normalize(self, plan):
        return dataclasses.replace(
            plan,
            scan_type=str(plan.scan_type).upper(),
            profile_id=str(plan.profile_id),
            mode_id=str(plan.mode_id),
            lateral_step_um=max(0.001, _round_um(plan.lateral_step_um)),
            z_length_um=max(0.001, _round_um(plan.z_length_um)),
            z_step_um=max(0.001, _round_um(plan.z_step_um)),
            step_durations_s=dict(plan.step_durations_s),
        )

    def plan_to_dict(self, plan) -> dict:
        return plan.to_dict()

    def plan_from_dict(self, data):
        return MoNaLISAScanPlan.from_dict(data)


__all__ = [
    'CONFIG_DEFAULTS', 'SCAN_TYPES', 'MoNaLISALimits', 'MoNaLISAScanCloak',
    'MoNaLISAScanPlan', 'compile_plan_sequence', 'default_plan', 'dicts_to_plan',
    'normalize_plan', 'plan_to_dicts', 'plan_with_backend_geometry', 'select_mode',
    'select_profile',
]
