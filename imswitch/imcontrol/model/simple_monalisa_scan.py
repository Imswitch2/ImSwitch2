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
import re
import string
from dataclasses import dataclass, field
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

SCAN_TYPES = ('XY', 'YZ', 'XZ', 'XYZ')
FOV_VALUE_SOURCES = ('detector', 'manual')
ULENS_PERIOD_POLICIES = ('fixed', 'per_profile')

DEFAULT_RECORDING_NAMING: Dict[str, Any] = {
    'enabled': True,
    'template': '{mode}_{sequence}',
    'includePower': False,
    'includeScanInfo': False,
    'scanLabels': {
        'XY': '2D',
        'XZ': 'slice',
        'YZ': 'slice',
        'XYZ': '3D',
    },
}

RECORDING_NAMING_TEMPLATE_FIELDS = (
    'mode', 'mode_id', 'profile', 'profile_id', 'sequence',
)


class RecordingNamingTemplateError(ValueError):
    """Invalid user-configured SimpleMoNaLISA recording suffix template."""


@dataclass(frozen=True)
class MoNaLISARecordingNaming:
    enabled: bool = True
    template: str = '{mode}_{sequence}'
    include_power: bool = False
    include_scan_info: bool = False
    scan_labels: Mapping[str, str] = field(default_factory=lambda: dict(
        DEFAULT_RECORDING_NAMING['scanLabels']
    ))

    @classmethod
    def from_config(cls, data: Optional[Mapping[str, Any]] = None):
        raw = dict(DEFAULT_RECORDING_NAMING)
        configured = dict(data or {}) if isinstance(data, Mapping) else {}
        raw.update(configured)
        labels = dict(DEFAULT_RECORDING_NAMING['scanLabels'])
        configured_labels = configured.get('scanLabels')
        if isinstance(configured_labels, Mapping):
            labels.update({
                str(key).upper(): str(value)
                for key, value in configured_labels.items()
                if str(key).upper() in SCAN_TYPES
            })
        result = cls(
            enabled=bool(raw.get('enabled', True)),
            template=str(raw.get('template', '{mode}_{sequence}')),
            include_power=bool(raw.get('includePower', False)),
            include_scan_info=bool(raw.get('includeScanInfo', False)),
            scan_labels=labels,
        )
        return result

    def to_config(self) -> dict:
        return {
            'enabled': bool(self.enabled),
            'template': str(self.template),
            'includePower': bool(self.include_power),
            'includeScanInfo': bool(self.include_scan_info),
            'scanLabels': {
                scan_type: str(self.scan_labels.get(scan_type, ''))
                for scan_type in SCAN_TYPES
            },
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
    'recordingNaming': DEFAULT_RECORDING_NAMING,
}

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


def validate_recording_naming_template(template: str) -> None:
    """Validate the deliberately small recording-suffix template language."""
    text = str(template or '')
    try:
        parsed = tuple(string.Formatter().parse(text))
    except ValueError as exc:
        raise RecordingNamingTemplateError(str(exc)) from exc
    allowed = set(RECORDING_NAMING_TEMPLATE_FIELDS)
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in allowed:
            available = ', '.join(
                '{' + name + '}'
                for name in RECORDING_NAMING_TEMPLATE_FIELDS
            )
            raise RecordingNamingTemplateError(
                f'Unknown recording naming token {{{field_name}}}. '
                f'Available tokens: {available}.'
            )
        if format_spec or conversion:
            raise RecordingNamingTemplateError(
                f'Recording naming token {{{field_name}}} does not support '
                'format specifiers or conversions.'
            )


def sanitize_recording_suffix(value: str) -> str:
    """Return a readable Windows-safe filename suffix (without separators)."""
    text = str(value or '').strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', text)
    text = re.sub(r'\s+', '_', text)
    text = re.sub(r'_+', '_', text)
    return text.strip(' ._-')


def _compact_number(value: float, *, significant: int = 8) -> str:
    numeric = float(value)
    if not math.isfinite(numeric):
        return str(numeric)
    if math.isclose(numeric, round(numeric), rel_tol=0.0, abs_tol=1e-12):
        return str(int(round(numeric)))
    return f'{numeric:.{significant}g}'


def _duration_text_s(value_s: float) -> str:
    return f'{_compact_number(float(value_s) * 1e3)}ms'


def _step_naming_label(step) -> str:
    role = str(getattr(step, 'role', '') or '').strip().lower()
    semantic = {'on': 'ON', 'off': 'OFF', 'readout': 'RO'}
    if role in semantic:
        return semantic[role]
    label = str(getattr(step, 'label', '') or getattr(step, 'id', '') or 'STEP')
    return sanitize_recording_suffix(label) or 'STEP'


def _power_text(state: Mapping[str, Any]) -> str:
    value = state.get('value')
    if value is None:
        return ''
    units = str(state.get('value_units') or '').strip().replace(' ', '')
    return f'{_compact_number(float(value))}{units}'


def _step_power_suffix(active_devices, laser_states: Mapping[str, Mapping[str, Any]]) -> str:
    resolved = []
    seen_targets = set()
    for device in active_devices:
        state = laser_states.get(str(device))
        if not isinstance(state, Mapping):
            continue
        if not bool(state.get('active_for_scan', False)):
            continue
        text = _power_text(state)
        if not text:
            continue
        target = str(state.get('power_device') or device)
        if target in seen_targets:
            continue
        seen_targets.add(target)
        resolved.append((target, text))
    if not resolved:
        return ''
    if len(resolved) == 1:
        return resolved[0][1]
    return ''.join(
        f'_{sanitize_recording_suffix(target)}-{text}'
        for target, text in resolved
    )


def _scan_step_text_um(value_um: float) -> str:
    value_um = float(value_um)
    if abs(value_um) < 1.0:
        return f'{_compact_number(value_um * 1000.0)}nm'
    return f'{_compact_number(value_um)}um'


def render_scan_naming_descriptor(
    plan: 'MoNaLISAScanPlan', naming: MoNaLISARecordingNaming
) -> str:
    axes = tuple(str(axis).upper() for axis in plan.axes)
    steps = [float(plan.step_um_for_axis(axis)) for axis in axes]
    scan_type = ''.join(axes)
    label = str(naming.scan_labels.get(scan_type, scan_type) or '').strip()
    prefix = sanitize_recording_suffix(label)
    axis_text = ''.join(axis.lower() for axis in axes)
    isotropic = bool(steps) and all(
        math.isclose(step, steps[0], rel_tol=1e-9, abs_tol=1e-12)
        for step in steps[1:]
    )
    if isotropic:
        geometry = f'{axis_text}{_scan_step_text_um(steps[0])}'
    else:
        geometry = '_'.join(
            f'{axis.lower()}{_scan_step_text_um(step)}'
            for axis, step in zip(axes, steps)
        )
    return '_'.join(part for part in (prefix, geometry) if part)


def render_recording_suffix(
    plan: 'MoNaLISAScanPlan',
    limits: 'MoNaLISALimits',
    *,
    laser_states: Optional[Mapping[str, Mapping[str, Any]]] = None,
    naming: Optional[MoNaLISARecordingNaming] = None,
) -> str:
    """Render one setup-configured suffix from the effective Simple plan."""
    naming = naming or limits.recording_naming
    if not naming.enabled:
        return ''
    validate_recording_naming_template(naming.template)
    plan = normalize_plan(plan, limits)
    profile = limits.profile_store.profile(plan.profile_id)
    mode = profile.mode(plan.mode_id)
    compiled = compile_plan_sequence(plan, limits)
    durations = compiled.step_durations_s
    active_ids = set(compiled.active_step_ids)
    laser_states = dict(laser_states or {})

    sequence_parts = []
    for step in profile.steps:
        if step.id not in active_ids:
            continue
        part = f'{_step_naming_label(step)}{_duration_text_s(durations[step.id])}'
        if naming.include_power:
            part += _step_power_suffix(mode.devices_for_step(step), laser_states)
        sequence_parts.append(part)
    sequence = '_'.join(sequence_parts)

    values = {
        'mode': mode.label,
        'mode_id': mode.id,
        'profile': profile.label,
        'profile_id': profile.id,
        'sequence': sequence,
    }
    rendered = str(naming.template).format_map(values).strip()
    if naming.include_scan_info:
        scan_descriptor = render_scan_naming_descriptor(plan, naming)
        rendered = '_'.join(part for part in (rendered, scan_descriptor) if part)
    return sanitize_recording_suffix(rendered)


def example_recording_suffix(naming: MoNaLISARecordingNaming) -> str:
    """Live setup-editor preview without depending on a selected profile."""
    validate_recording_naming_template(naming.template)
    sequence = 'ON1ms_OFF2ms_RO1ms'
    if naming.include_power:
        sequence = 'ON1ms50mW_OFF2ms200mW_RO1ms25mW'
    values = {
        'mode': 'RESOLFT',
        'mode_id': 'resolft',
        'profile': 'Default',
        'profile_id': 'default',
        'sequence': sequence,
    }
    rendered = str(naming.template).format_map(values).strip()
    if naming.include_scan_info:
        label = str(naming.scan_labels.get('XY', '2D') or '').strip()
        descriptor = '_'.join(part for part in (label, 'xy25nm') if part)
        rendered = '_'.join(part for part in (rendered, descriptor) if part)
    return sanitize_recording_suffix(rendered)

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
    recording_naming: MoNaLISARecordingNaming

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
            recording_naming=MoNaLISARecordingNaming.from_config(
                raw.get('recordingNaming')
            ),
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

    def lateral_geometry_mode(self, profile_id: Optional[str] = None) -> str:
        profile = self.profile_store.profile(profile_id)
        return str(getattr(profile, 'lateral_geometry_mode', 'coupled') or 'coupled')

    def default_lateral_scan_length_um(self, profile_id: Optional[str] = None) -> float:
        profile = self.profile_store.profile(profile_id)
        if profile.scan_extent_source == 'manual':
            if profile.manual_scan_extent_um is None or profile.manual_scan_extent_um <= 0:
                raise PlanNotRepresentable(
                    f'Profile {profile.label!r} needs a positive manual scan extent.'
                )
            return float(profile.manual_scan_extent_um)
        return self.ulens_period_um(profile.id)

    def default_axis_scan_length_um(self, logical_axis: str, profile_id: Optional[str] = None) -> float:
        axis = str(logical_axis).upper()
        profile = self.profile_store.profile(profile_id)
        if axis == 'Z':
            profile_value = getattr(profile, 'default_z_scan_length_um', None)
            return float(
                profile_value if profile_value is not None
                else self.default_z_scan_length_um
            )
        if profile.scan_extent_source == 'ulens':
            return self.ulens_period_um(profile.id)
        if self.lateral_geometry_mode(profile.id) == 'independent':
            if axis == 'X' and getattr(profile, 'manual_scan_extent_x_um', None) is not None:
                return float(profile.manual_scan_extent_x_um)
            if axis == 'Y' and getattr(profile, 'manual_scan_extent_y_um', None) is not None:
                return float(profile.manual_scan_extent_y_um)
        if profile.manual_scan_extent_um is None or profile.manual_scan_extent_um <= 0:
            raise PlanNotRepresentable(
                f'Profile {profile.label!r} needs a positive manual scan extent.'
            )
        return float(profile.manual_scan_extent_um)

    def default_axis_step_um(self, logical_axis: str, profile_id: Optional[str] = None) -> float:
        axis = str(logical_axis).upper()
        profile = self.profile_store.profile(profile_id)
        if axis == 'Z':
            profile_value = getattr(profile, 'default_z_step_um', None)
            return float(
                profile_value if profile_value is not None else self.default_z_step_um
            )
        if axis == 'X' and getattr(profile, 'default_x_step_um', None):
            return float(profile.default_x_step_um)
        if axis == 'Y' and getattr(profile, 'default_y_step_um', None):
            return float(profile.default_y_step_um)
        if getattr(profile, 'default_lateral_step_um', None):
            return float(profile.default_lateral_step_um)
        return float(self.default_lateral_step_um)

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
    x_length_um: Optional[float] = None
    y_length_um: Optional[float] = None
    x_step_um: Optional[float] = None
    y_step_um: Optional[float] = None

    @property
    def axes(self) -> Tuple[str, ...]:
        return tuple(self.scan_type)

    def profile(self, limits: MoNaLISALimits):
        return limits.profile_store.profile(self.profile_id)

    def length_um_for_axis(self, logical_axis: str) -> float:
        axis = str(logical_axis).upper()
        if axis == 'Z':
            return float(self.z_length_um)
        if axis == 'X':
            return float(self.x_length_um if self.x_length_um is not None else self.lateral_length_um)
        if axis == 'Y':
            return float(self.y_length_um if self.y_length_um is not None else self.lateral_length_um)
        return float(self.lateral_length_um)

    def step_um_for_axis(self, logical_axis: str) -> float:
        axis = str(logical_axis).upper()
        if axis == 'Z':
            return float(self.z_step_um)
        if axis == 'X':
            return float(self.x_step_um if self.x_step_um is not None else self.lateral_step_um)
        if axis == 'Y':
            return float(self.y_step_um if self.y_step_um is not None else self.lateral_step_um)
        return float(self.lateral_step_um)

    def dwell_s(self, limits: MoNaLISALimits) -> float:
        compiled = compile_plan_sequence(self, limits)
        return compiled.end_time_s + limits.camera_readout_time_s(self.profile_id)

    def pixels(self, limits: MoNaLISALimits) -> Tuple[int, ...]:
        result = []
        for axis in self.axes:
            result.append(
                pixels_for_length_step(self.length_um_for_axis(axis), self.step_um_for_axis(axis))
            )
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
            'x_length_um': self.x_length_um,
            'y_length_um': self.y_length_um,
            'x_step_um': self.x_step_um,
            'y_step_um': self.y_step_um,
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
            x_length_um=(None if data.get('x_length_um') in (None, '') else float(data.get('x_length_um'))),
            y_length_um=(None if data.get('y_length_um') in (None, '') else float(data.get('y_length_um'))),
            x_step_um=(None if data.get('x_step_um') in (None, '') else float(data.get('x_step_um'))),
            y_step_um=(None if data.get('y_step_um') in (None, '') else float(data.get('y_step_um'))),
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

    geometry_mode = str(getattr(profile, 'lateral_geometry_mode', 'coupled') or 'coupled')
    if profile.scan_extent_source == 'ulens':
        x_length = y_length = limits.ulens_period_um(profile.id)
    else:
        x_length = max(0.001, _round_um(
            plan.x_length_um if plan.x_length_um is not None else plan.lateral_length_um
        ))
        y_length = max(0.001, _round_um(
            plan.y_length_um if plan.y_length_um is not None else plan.lateral_length_um
        ))
    x_step = max(0.001, _round_um(
        plan.x_step_um if plan.x_step_um is not None else plan.lateral_step_um
    ))
    y_step = max(0.001, _round_um(
        plan.y_step_um if plan.y_step_um is not None else plan.lateral_step_um
    ))
    if geometry_mode != 'independent':
        x_length = y_length = x_length
        x_step = y_step = x_step

    return dataclasses.replace(
        plan,
        scan_type=scan_type,
        lateral_length_um=x_length,
        lateral_step_um=x_step,
        x_length_um=x_length,
        y_length_um=y_length,
        x_step_um=x_step,
        y_step_um=y_step,
        profile_id=profile.id,
        mode_id=mode_id,
        step_durations_s=durations,
        z_length_um=max(0.001, _round_um(plan.z_length_um)),
        z_step_um=max(0.001, _round_um(plan.z_step_um)),
    )


def default_plan(
    limits: MoNaLISALimits, profile_id: Optional[str] = None
) -> MoNaLISAScanPlan:
    profile = limits.profile_store.profile(profile_id)
    return normalize_plan(MoNaLISAScanPlan(
        scan_type=limits.default_scan_type,
        lateral_length_um=limits.default_axis_scan_length_um('X', profile.id),
        lateral_step_um=limits.default_axis_step_um('X', profile.id),
        z_length_um=limits.default_axis_scan_length_um('Z', profile.id),
        z_step_um=limits.default_axis_step_um('Z', profile.id),
        profile_id=profile.id,
        mode_id=profile.default_mode,
        step_durations_s={step.id: step.default_duration_s for step in profile.steps},
        x_length_um=limits.default_axis_scan_length_um('X', profile.id),
        y_length_um=limits.default_axis_scan_length_um('Y', profile.id),
        x_step_um=limits.default_axis_step_um('X', profile.id),
        y_step_um=limits.default_axis_step_um('Y', profile.id),
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
    if profile.scan_extent_source == 'ulens':
        x_length = limits.default_axis_scan_length_um('X', profile.id)
        y_length = limits.default_axis_scan_length_um('Y', profile.id)
    else:
        x_length = plan.length_um_for_axis('X')
        y_length = plan.length_um_for_axis('Y')
    return normalize_plan(dataclasses.replace(
        plan,
        profile_id=profile.id,
        mode_id=mode_id,
        lateral_length_um=x_length,
        x_length_um=x_length,
        y_length_um=y_length,
        x_step_um=plan.step_um_for_axis('X'),
        y_step_um=plan.step_um_for_axis('Y'),
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
    # The lateral geometry follows the profile (its uLens period or manual
    # extent). The Z range and the scan type are the user's: a switch to
    # another profile keeps them, while an explicit reload or re-selection
    # (``preserve_if_same=False``) takes the profile's own Z defaults.
    if preserve_if_same:
        z_length, z_step = plan.z_length_um, plan.z_step_um
    else:
        z_length = limits.default_axis_scan_length_um('Z', profile.id)
        z_step = limits.default_axis_step_um('Z', profile.id)
    return normalize_plan(dataclasses.replace(
        plan,
        profile_id=profile.id,
        mode_id=mode_id,
        lateral_length_um=limits.default_axis_scan_length_um('X', profile.id),
        lateral_step_um=limits.default_axis_step_um('X', profile.id),
        x_length_um=limits.default_axis_scan_length_um('X', profile.id),
        y_length_um=limits.default_axis_scan_length_um('Y', profile.id),
        x_step_um=limits.default_axis_step_um('X', profile.id),
        y_step_um=limits.default_axis_step_um('Y', profile.id),
        z_length_um=z_length,
        z_step_um=z_step,
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
) -> Tuple[str, float, float, float, float, float, float, float, float]:
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
    # The plan has no centre: plan_to_dicts writes every axis at 0, and the
    # Beta designer moves every axis by its centre, scanned or not. A centre
    # the plan cannot hold is refused, so it stays in Advanced instead of
    # being reset to 0 by the next mirror (review 2026-10-17).
    for device, center in zip(devices, analog.get('axis_centerpos') or ()):
        if not math.isclose(float(center), 0.0, abs_tol=1e-3):
            raise PlanNotRepresentable(
                f'{reverse_axes.get(device, device)} is centred at {float(center):g} µm; a '
                'Simple MoNaLISA scan is centred at 0 µm, so the offset stays in Advanced.')
    lateral_lengths = {}
    lateral_steps = {}
    z_length = reference_plan.z_length_um
    z_step = reference_plan.z_step_um
    for axis in logical:
        device = limits.axis_device(axis)
        if device not in by_device:
            raise PlanNotRepresentable(f'This scan does not contain the configured {axis} axis.')
        index = by_device[device]
        if index >= len(lengths) or index >= len(steps):
            raise PlanNotRepresentable(f'This scan has incomplete geometry for the configured {axis} axis.')
        length = float(lengths[index])
        step = float(steps[index])
        if axis == 'Z':
            if length <= 0:
                raise PlanNotRepresentable('Z scan length must be positive.')
            z_length = length
            z_step = step
            continue
        if length <= 0:
            raise PlanNotRepresentable('Lateral scan length must be positive.')
        lateral_lengths[axis] = length
        lateral_steps[axis] = step

    profile = limits.profile_store.profile(reference_plan.profile_id)
    geometry_mode = str(getattr(profile, 'lateral_geometry_mode', 'coupled') or 'coupled')
    if geometry_mode != 'independent':
        steps_values = list(lateral_steps.values())
        if steps_values and any(not math.isclose(step, steps_values[0], abs_tol=1e-3)
                                 for step in steps_values[1:]):
            raise PlanNotRepresentable('X and Y must use the same lateral step in SimpleMoNaLISA.')
        length_values = list(lateral_lengths.values())
        if length_values and any(
            not math.isclose(length, length_values[0], abs_tol=1e-3)
            for length in length_values[1:]
        ):
            raise PlanNotRepresentable('X and Y must use the same scan length in SimpleMoNaLISA.')

    if profile.scan_extent_source == 'ulens':
        expected = limits.ulens_period_um(profile.id)
        for axis, length in lateral_lengths.items():
            if not math.isclose(length, expected, abs_tol=1e-3):
                raise PlanNotRepresentable(
                    f'{axis} scan length must stay at the profile uLens period {expected:g} µm.'
                )
        x_length = y_length = expected
    else:
        x_length = lateral_lengths.get('X', reference_plan.length_um_for_axis('X'))
        y_length = lateral_lengths.get('Y', reference_plan.length_um_for_axis('Y'))
    x_step = lateral_steps.get('X', reference_plan.step_um_for_axis('X'))
    y_step = lateral_steps.get('Y', reference_plan.step_um_for_axis('Y'))
    lateral_length = x_length
    lateral_step = x_step
    return logical, lateral_length, lateral_step, z_length, z_step, x_length, y_length, x_step, y_step


def plan_with_backend_geometry(
    plan: MoNaLISAScanPlan, analog: Mapping[str, Any], limits: MoNaLISALimits
) -> MoNaLISAScanPlan:
    logical, lateral_length, lateral, z_length, z_step, x_length, y_length, x_step, y_step = _parse_geometry(
        analog, limits, plan
    )
    return normalize_plan(dataclasses.replace(
        plan,
        scan_type=logical,
        lateral_length_um=lateral_length,
        lateral_step_um=lateral,
        x_length_um=x_length,
        y_length_um=y_length,
        x_step_um=x_step,
        y_step_um=y_step,
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
        analog['target_device'].append(device)
        # An axis the scan does not sweep is written as the Advanced panel
        # writes it back (length 1, step 1), so a saved Simple scan is an
        # exact round trip through that panel's serializer.
        analog['axis_length'].append(float(plan.length_um_for_axis(axis)) if active else 1.0)
        analog['axis_step_size'].append(
            float(plan.step_um_for_axis(axis)) if active else 1.0
        )
        analog['axis_centerpos'].append(0.0)
        analog['axis_startpos'].append([0.0])

    dwell = plan.dwell_s(limits)
    analog['sequence_time'] = dwell
    analog['phase_delay'] = 0.0

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
        # Full power on every device, no sequence rows and no device locks:
        # the values the Advanced panel's serializer writes back for a scan
        # that sets none of them, so the dicts compare equal after a mirror.
        'linestep_power_percent': {name: [100.0] for name in limits.ttl_devices},
        'linestep_power_enabled': {name: True for name in limits.ttl_devices},
        'intra_pixel_positioner_movement': False,
        'positioner_target_device': [],
        'positioner_linestep_enable': {},
        'positioner_movement_starts_s': {},
        'positioner_movement_ends_s': {},
        'positioner_step_size_um': {},
        'advanced_program_mode': 'timing',
        'advanced_sequence_rows': [[]],
        'line_program_devices_enabled': True,
        'advanced_device_lock_master': {name: False for name in limits.ttl_devices},
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
            x_length_um=(None if plan.x_length_um is None else max(0.001, _round_um(plan.x_length_um))),
            y_length_um=(None if plan.y_length_um is None else max(0.001, _round_um(plan.y_length_um))),
            x_step_um=(None if plan.x_step_um is None else max(0.001, _round_um(plan.x_step_um))),
            y_step_um=(None if plan.y_step_um is None else max(0.001, _round_um(plan.y_step_um))),
            z_length_um=max(0.001, _round_um(plan.z_length_um)),
            z_step_um=max(0.001, _round_um(plan.z_step_um)),
            step_durations_s=dict(plan.step_durations_s),
        )

    def plan_to_dict(self, plan) -> dict:
        return plan.to_dict()

    def plan_from_dict(self, data):
        return MoNaLISAScanPlan.from_dict(data)


__all__ = [
    'CONFIG_DEFAULTS', 'DEFAULT_RECORDING_NAMING',
    'RECORDING_NAMING_TEMPLATE_FIELDS', 'SCAN_TYPES',
    'ULENS_PERIOD_POLICIES', 'MoNaLISALimits', 'MoNaLISARecordingNaming',
    'MoNaLISAScanCloak', 'MoNaLISAScanPlan', 'RecordingNamingTemplateError',
    'compile_plan_sequence', 'default_plan', 'dicts_to_plan',
    'example_recording_suffix', 'normalize_plan', 'plan_to_dicts',
    'plan_with_backend_geometry', 'render_recording_suffix',
    'render_scan_naming_descriptor', 'sanitize_recording_suffix', 'select_mode',
    'select_profile', 'validate_recording_naming_template',
]
