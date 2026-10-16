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


DEFAULT_SLIDER_RANGES: Dict[str, Tuple[float, float]] = {
    'pixelSizeNm': (5.0, 1000.0),
    'lateralScanLengthUm': (0.05, 10.0),
    'zStepNm': (5.0, 1000.0),
    'zScanLengthUm': (0.1, 10.0),
    'stepDurationMs': (0.01, 20.0),
}


CONFIG_DEFAULTS: Dict[str, Any] = {
    'fovSource': 'detector',
    'fovUm': (40.0, 40.0),
    'ulensPeriodPolicy': 'fixed',
    'ulensPeriodUm': 0.72,
    'defaultZScanLengthUm': 1.0,
    'defaultScanType': 'XY',
    'defaultLateralStepUm': 0.04,
    'defaultZStepUm': 0.15,
    'interPulseDelayMs': 0.1,
    'readoutPulseTimeMs': 1.0,
    'defaultCameraReadoutTimeMs': 3.0,
    'cameraReadoutSafetyMarginMs': 0.1,
    'cameraReadoutToleranceMs': 0.1,
    'cameraReadoutUnoptimizedThresholdMs': 0.3,
    'sliderRanges': DEFAULT_SLIDER_RANGES,
}

SCAN_TYPES = ('XY', 'YZ', 'XZ', 'XYZ')
FOV_VALUE_SOURCES = ('detector', 'manual')
ULENS_PERIOD_POLICIES = ('fixed', 'per_profile')


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




def _configured_slider_ranges(value) -> Dict[str, Tuple[float, float]]:
    configured = value if isinstance(value, Mapping) else {}
    result: Dict[str, Tuple[float, float]] = {}
    for key, default in DEFAULT_SLIDER_RANGES.items():
        raw = configured.get(key, default)
        try:
            low, high = tuple(float(v) for v in raw[:2])
        except Exception:
            low, high = default
        if not math.isfinite(low) or not math.isfinite(high) or high <= low:
            low, high = default
        result[key] = (float(low), float(high))
    return result

def _round_um(value: float) -> float:
    return round(float(value), 3)


def _snap_time_s(value_s: float, sample_rate: float) -> float:
    samples = max(0, round(float(value_s) * float(sample_rate)))
    return samples / float(sample_rate)


@dataclass(frozen=True)
class ULensPeriodResolution:
    """Effective uLens period together with the configuration that supplied it.

    SimpleMoNaLISA deliberately consumes this small resolver contract rather than
    knowing how a period is produced.  Today periods come from setup/profile
    configuration; an SLM-backed provider can later return the same contract.
    """

    value_um: float
    source_kind: str
    source_id: str


@dataclass(frozen=True)
class MoNaLISALimits:
    """Fixed microscope/setup facts used by the simplified MoNaLISA panel."""

    axis_devices: Mapping[str, str]
    ttl_devices: Tuple[str, ...]
    detector_devices: Tuple[str, ...]
    device_wavelengths_nm: Mapping[str, Optional[float]]
    sample_rate: float
    fov_source: str
    manual_fov_um: Tuple[float, float]
    fov_um: Tuple[float, float]
    ulens_period_policy: str
    fixed_ulens_period_um: float
    profile_store: MoNaLISAProfileStore
    inter_pulse_delay_s: float
    readout_pulse_time_s: float
    default_camera_readout_time_s: float
    camera_readout_safety_margin_s: float
    camera_readout_tolerance_s: float
    camera_readout_unoptimized_threshold_s: float
    return_time_s: float
    default_scan_type: str
    default_lateral_step_um: float
    default_z_scan_length_um: float
    default_z_step_um: float
    slider_ranges: Mapping[str, Tuple[float, float]]

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
        detector_devices = tuple(str(name) for name in getattr(setupInfo, 'detectors', {}))
        wavelengths = {
            str(name): (
                None if getattr(info, 'wavelength', None) is None
                else float(getattr(info, 'wavelength'))
            )
            for name, info in getattr(setupInfo, 'lasers', {}).items()
        }
        profile_store = MoNaLISAProfileStore.from_config(configured)

        scan_type = str(raw.get('defaultScanType') or 'XY').upper()
        if scan_type not in SCAN_TYPES:
            scan_type = 'XY'
        fov_source = str(raw.get('fovSource') or 'detector').lower()
        if fov_source not in FOV_VALUE_SOURCES:
            fov_source = 'detector'
        period_policy = str(raw.get('ulensPeriodPolicy') or 'fixed').lower()
        if period_policy not in ULENS_PERIOD_POLICIES:
            period_policy = 'fixed'
        period = max(0.001, float(raw.get('ulensPeriodUm') or CONFIG_DEFAULTS['ulensPeriodUm']))
        designer = getattr(setupInfo.scan, 'scanDesignerParams', None) or {}
        manual_fov = _configured_tuple(raw.get('fovUm'))
        # ``cameraReadoutTimeMs`` was the pre-profile setup value. Keep it as
        # a compatibility fallback while new setups persist the clearer
        # ``defaultCameraReadoutTimeMs`` name.
        configured_default_readout = configured.get(
            'defaultCameraReadoutTimeMs',
            configured.get('cameraReadoutTimeMs', raw.get('defaultCameraReadoutTimeMs')),
        )
        default_readout = max(0.0, float(configured_default_readout or 0.0) * 1e-3)
        return cls(
            axis_devices=axes,
            ttl_devices=ttl,
            detector_devices=detector_devices,
            device_wavelengths_nm=wavelengths,
            sample_rate=float(setupInfo.scan.sampleRate),
            fov_source=fov_source,
            manual_fov_um=manual_fov,
            fov_um=manual_fov,
            ulens_period_policy=period_policy,
            fixed_ulens_period_um=period,
            profile_store=profile_store,
            inter_pulse_delay_s=max(0.0, float(raw.get('interPulseDelayMs') or 0.0) * 1e-3),
            readout_pulse_time_s=max(
                1e-3,
                float(raw.get('readoutPulseTimeMs') or CONFIG_DEFAULTS['readoutPulseTimeMs'])
                * 1e-3,
            ),
            default_camera_readout_time_s=default_readout,
            camera_readout_safety_margin_s=max(
                0.0, float(raw.get('cameraReadoutSafetyMarginMs') or 0.0) * 1e-3
            ),
            camera_readout_tolerance_s=max(
                0.0, float(raw.get('cameraReadoutToleranceMs') or 0.0) * 1e-3
            ),
            camera_readout_unoptimized_threshold_s=max(
                0.0,
                float(raw.get('cameraReadoutUnoptimizedThresholdMs') or 0.0) * 1e-3,
            ),
            return_time_s=max(0.0, float(designer.get('return_time', 0.0) or 0.0)),
            default_scan_type=scan_type,
            default_lateral_step_um=max(0.001, float(raw.get('defaultLateralStepUm') or 0.04)),
            default_z_scan_length_um=max(0.001, float(raw.get('defaultZScanLengthUm') or 1.0)),
            default_z_step_um=max(0.001, float(raw.get('defaultZStepUm') or 0.15)),
            slider_ranges=_configured_slider_ranges(raw.get('sliderRanges')),
        )

    def resolve_ulens_period(
        self, profile_id: Optional[str] = None
    ) -> ULensPeriodResolution:
        """Resolve the current effective uLens period and its provenance.

        ``ulens_period_policy`` remains the ownership/configuration policy.  The
        returned provenance is intentionally a separate seam so a future SLM
        bridge can provide ``source_kind='slm'`` without teaching the scan plan
        or save/load code about SLM internals.
        """
        if self.ulens_period_policy == 'fixed':
            return ULensPeriodResolution(
                value_um=float(self.fixed_ulens_period_um),
                source_kind='setup',
                source_id='simpleMoNaLISA.ulensPeriodUm',
            )
        profile = self.profile_store.profile(profile_id)
        if profile.ulens_period_um is None or profile.ulens_period_um <= 0:
            raise PlanNotRepresentable(
                f'Profile {profile.label!r} needs a uLens period because this setup '
                'uses per-profile uLens periods.'
            )
        return ULensPeriodResolution(
            value_um=float(profile.ulens_period_um),
            source_kind='profile',
            source_id=f'simpleMoNaLISA.profiles.{profile.id}.ulensPeriodUm',
        )

    def ulens_period_um(self, profile_id: Optional[str] = None) -> float:
        return self.resolve_ulens_period(profile_id).value_um


    def camera_readout_time_s(self, profile_id: Optional[str] = None) -> float:
        """Return the effective camera gap for a profile.

        Profiles may override the setup default. The detector's currently
        reported timing is intentionally *not* part of this resolution; it is
        diagnostic information only and can be copied explicitly with Auto.
        """
        profile = self.profile_store.profile(profile_id)
        if profile.camera_readout_time_s is not None:
            return max(0.0, float(profile.camera_readout_time_s))
        return max(0.0, float(self.default_camera_readout_time_s))

    def camera_readout_source(self, profile_id: Optional[str] = None) -> str:
        profile = self.profile_store.profile(profile_id)
        return 'profile' if profile.camera_readout_time_s is not None else 'setup default'

    def default_lateral_scan_length_um(self, profile_id: Optional[str] = None) -> float:
        profile = self.profile_store.profile(profile_id)
        if profile.scan_extent_source == 'manual':
            if profile.manual_scan_extent_um is None or profile.manual_scan_extent_um <= 0:
                raise PlanNotRepresentable(
                    f'Profile {profile.label!r} needs a positive manual scan extent.'
                )
            return float(profile.manual_scan_extent_um)
        return self.ulens_period_um(profile.id)

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
            if self.ulens_period_policy == 'per_profile':
                try:
                    self.ulens_period_um(selected)
                except PlanNotRepresentable as exc:
                    missing.append(str(exc))
        return tuple(missing)


@dataclass(frozen=True)
class MoNaLISAScanPlan:
    scan_type: str
    lateral_length_um: float
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
        return compiled.end_time_s + limits.camera_readout_time_s(self.profile_id)

    def pixels(self, limits: MoNaLISALimits) -> Tuple[int, ...]:
        result = []
        for axis in self.axes:
            step = self.z_step_um if axis == 'Z' else self.lateral_step_um
            length = self.z_length_um if axis == 'Z' else self.lateral_length_um
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
            'lateral_length_um': self.lateral_length_um,
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
            lateral_length_um=float(data.get('lateral_length_um', 0.72)),
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
        if step.role == 'readout':
            requested = limits.readout_pulse_time_s
        else:
            requested = supplied.get(step.id, step.default_duration_s)
        if step.role != 'readout' and not step.duration_editable:
            requested = step.default_duration_s
        duration = _snap_time_s(max(0.0, float(requested)), limits.sample_rate)
        if duration <= 0:
            raise PlanNotRepresentable(
                f'Step {step.label!r} must have a positive duration.'
            )
        durations[step.id] = duration

    if profile.scan_extent_source == 'ulens':
        lateral_length = limits.ulens_period_um(profile.id)
    else:
        lateral_length = max(0.001, _round_um(plan.lateral_length_um))

    return dataclasses.replace(
        plan,
        scan_type=scan_type,
        lateral_length_um=lateral_length,
        profile_id=profile.id,
        mode_id=mode_id,
        step_durations_s=durations,
        lateral_step_um=max(0.001, _round_um(plan.lateral_step_um)),
        z_length_um=max(0.001, _round_um(plan.z_length_um)),
        z_step_um=max(0.001, _round_um(plan.z_step_um)),
    )


def default_plan(
    limits: MoNaLISALimits, profile_id: Optional[str] = None
) -> MoNaLISAScanPlan:
    profile = limits.profile_store.profile(profile_id)
    return normalize_plan(MoNaLISAScanPlan(
        scan_type=limits.default_scan_type,
        lateral_length_um=limits.default_lateral_scan_length_um(profile.id),
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
    lateral_length = (
        limits.default_lateral_scan_length_um(profile.id)
        if profile.scan_extent_source == 'ulens'
        else plan.lateral_length_um
    )
    return normalize_plan(dataclasses.replace(
        plan,
        profile_id=profile.id,
        mode_id=mode_id,
        lateral_length_um=lateral_length,
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
        lateral_length_um=limits.default_lateral_scan_length_um(profile.id),
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
    analog: Mapping[str, Any], limits: MoNaLISALimits, reference_plan: MoNaLISAScanPlan
) -> Tuple[str, float, float, float, float]:
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
    lateral_lengths = []
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
        else:
            if length <= 0:
                raise PlanNotRepresentable('Lateral scan length must be positive.')
            lateral_lengths.append(length)
        step = float(steps[index])
        if axis == 'Z':
            z_step = step
        else:
            lateral_steps.append(step)
    if lateral_steps and any(not math.isclose(step, lateral_steps[0], abs_tol=1e-3)
                             for step in lateral_steps[1:]):
        raise PlanNotRepresentable('X and Y must use the same lateral step in SimpleMoNaLISA.')
    if lateral_lengths and any(
        not math.isclose(length, lateral_lengths[0], abs_tol=1e-3)
        for length in lateral_lengths[1:]
    ):
        raise PlanNotRepresentable('X and Y must use the same scan length in SimpleMoNaLISA.')
    profile = limits.profile_store.profile(reference_plan.profile_id)
    lateral_length = lateral_lengths[0] if lateral_lengths else reference_plan.lateral_length_um
    if profile.scan_extent_source == 'ulens':
        expected = limits.ulens_period_um(profile.id)
        if not math.isclose(lateral_length, expected, abs_tol=1e-3):
            raise PlanNotRepresentable(
                f'Lateral scan length must stay at the profile uLens period {expected:g} µm.'
            )
        lateral_length = expected
    lateral = lateral_steps[0] if lateral_steps else limits.default_lateral_step_um
    return logical, lateral_length, lateral, z_length, z_step


def plan_with_backend_geometry(
    plan: MoNaLISAScanPlan, analog: Mapping[str, Any], limits: MoNaLISALimits
) -> MoNaLISAScanPlan:
    logical, lateral_length, lateral, z_length, z_step = _parse_geometry(
        analog, limits, plan
    )
    return normalize_plan(dataclasses.replace(
        plan,
        scan_type=logical,
        lateral_length_um=lateral_length,
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
        length = plan.z_length_um if axis == 'Z' else plan.lateral_length_um
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
            lateral_length_um=max(0.001, _round_um(plan.lateral_length_um)),
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
    'CONFIG_DEFAULTS', 'DETECTOR_VALUE_SOURCES', 'SCAN_TYPES',
    'ULENS_PERIOD_POLICIES', 'MoNaLISALimits', 'MoNaLISAScanCloak',
    'MoNaLISAScanPlan', 'compile_plan_sequence', 'default_plan', 'dicts_to_plan',
    'normalize_plan', 'plan_to_dicts', 'plan_with_backend_geometry', 'select_mode',
    'select_profile',
]
