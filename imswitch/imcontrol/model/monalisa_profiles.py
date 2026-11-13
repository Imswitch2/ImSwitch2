"""Data-driven MoNaLISA illumination profiles.

A profile is an ordered sequence of domain-specific illumination steps.  Every
step has one semantic MoNaLISA role (ON, OFF, READOUT or CUSTOM), but roles may
repeat and do not define the sequence structure.  The compiler/parser operates
on the ordered steps and the devices enabled by the selected mode, which makes
profiles flexible enough for synchronised multi-laser phases, multi-colour
sequences and alignment pulses without requiring profile-specific Python code.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


MONALISA_STEP_ROLES = ('on', 'off', 'readout', 'custom')
MONALISA_SCAN_EXTENT_SOURCES = ('ulens', 'manual')
MONALISA_LATERAL_GEOMETRY_MODES = ('coupled', 'independent')


MONALISA_BINDING_BOUND = 'bound'
MONALISA_BINDING_INCOMPATIBLE = 'incompatible'
MONALISA_BINDING_NONE = 'none'


def profile_id_from_label(label: str, existing_ids: Iterable[str] = ()) -> str:
    """Return a stable config-safe id for a newly created profile.

    Existing profile ids are never regenerated from labels; this helper is only
    for new/duplicated profiles so renaming a profile does not change identity.
    """
    text = ''.join(
        char.lower() if char.isalnum() else '_'
        for char in str(label).strip()
    )
    base = '_'.join(part for part in text.split('_') if part) or 'profile'
    existing = {str(value) for value in existing_ids}
    if base not in existing:
        return base
    index = 2
    while f'{base}_{index}' in existing:
        index += 1
    return f'{base}_{index}'


def step_id_for_role(role: str, existing_ids: Iterable[str] = ()) -> str:
    """Generate a stable id for a newly added step inside one profile."""
    role = str(role).strip().lower()
    base = role if role in MONALISA_STEP_ROLES else 'step'
    existing = {str(value) for value in existing_ids}
    if base not in existing:
        return base
    index = 2
    while f'{base}_{index}' in existing:
        index += 1
    return f'{base}_{index}'


def mode_id_from_label(label: str, existing_ids: Iterable[str] = ()) -> str:
    """Generate a stable id for a newly added mode inside one profile."""
    return profile_id_from_label(label or 'mode', existing_ids)


def profile_fingerprint(profile: "MoNaLISAProfileDefinition") -> str:
    """Stable fingerprint of the profile structure currently bound in Simple."""
    payload = json.dumps(profile.to_config(), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class MoNaLISAProfileBinding:
    profile_id: Optional[str]
    mode_id: Optional[str]
    profile_fingerprint: Optional[str]
    status: str = MONALISA_BINDING_NONE
    reason: Optional[str] = None

    @classmethod
    def unbound(cls):
        return cls(
            profile_id=None,
            mode_id=None,
            profile_fingerprint=None,
            status=MONALISA_BINDING_NONE,
            reason=None,
        )

    @classmethod
    def bound(cls, profile: "MoNaLISAProfileDefinition", mode_id: str):
        return cls(
            profile_id=profile.id,
            mode_id=str(mode_id),
            profile_fingerprint=profile_fingerprint(profile),
            status=MONALISA_BINDING_BOUND,
            reason=None,
        )

    def incompatible(self, reason: str):
        return MoNaLISAProfileBinding(
            profile_id=self.profile_id,
            mode_id=self.mode_id,
            profile_fingerprint=self.profile_fingerprint,
            status=MONALISA_BINDING_INCOMPATIBLE,
            reason=str(reason),
        )

    @property
    def is_bound(self) -> bool:
        return self.status == MONALISA_BINDING_BOUND


class MoNaLISAProfileError(ValueError):
    """Invalid profile definition or profile-store configuration."""


class MoNaLISASequenceNotCompatible(ValueError):
    """Backend TTL timing cannot be represented by a selected profile/mode."""


def _snap_time_s(value_s: float, sample_rate: float) -> float:
    samples = max(0, round(float(value_s) * float(sample_rate)))
    return samples / float(sample_rate)


def _unique_strings(values: Iterable[Any]) -> Tuple[str, ...]:
    result = []
    seen = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


@dataclass(frozen=True)
class MoNaLISASequenceStepDefinition:
    id: str
    label: str
    role: str
    ttl_devices: Tuple[str, ...]
    default_duration_s: float
    duration_editable: bool = True

    @classmethod
    def from_config(cls, step_id: str, data: Mapping[str, Any]):
        return cls(
            id=str(step_id).strip(),
            label=str(data.get('label') or step_id).strip(),
            role=str(data.get('role') or '').strip().lower(),
            ttl_devices=_unique_strings(data.get('ttlDevices') or ()),
            default_duration_s=max(0.0, float(data.get('defaultDurationMs', 0.0)) * 1e-3),
            duration_editable=bool(data.get('durationEditable', True)),
        )

    def to_config(self) -> dict:
        return {
            'label': self.label,
            'role': self.role,
            'ttlDevices': list(self.ttl_devices),
            'defaultDurationMs': self.default_duration_s * 1e3,
            'durationEditable': self.duration_editable,
        }


@dataclass(frozen=True)
class MoNaLISAModeDefinition:
    id: str
    label: str
    enabled_steps: Tuple[str, ...]
    enabled_devices: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def from_config(cls, mode_id: str, data: Mapping[str, Any]):
        raw_enabled_devices = data.get('enabledDevices') or {}
        if not isinstance(raw_enabled_devices, Mapping):
            raise MoNaLISAProfileError(
                f'Mode {mode_id!r} enabledDevices must be an object.'
            )
        return cls(
            id=str(mode_id).strip(),
            label=str(data.get('label') or mode_id).strip(),
            enabled_steps=_unique_strings(data.get('enabledSteps') or ()),
            enabled_devices={
                str(step_id): _unique_strings(devices or ())
                for step_id, devices in raw_enabled_devices.items()
            },
        )

    def devices_for_step(
        self, step: MoNaLISASequenceStepDefinition
    ) -> Tuple[str, ...]:
        """Return devices active for ``step`` in this mode.

        ``enabledSteps`` remains the whole-step shorthand used by all existing
        profiles.  ``enabledDevices`` is only an override for enabled steps and
        allows alignment modes to keep one timing step while toggling devices
        inside it.
        """
        if step.id not in self.enabled_steps:
            return ()
        override = self.enabled_devices.get(step.id)
        return tuple(step.ttl_devices if override is None else override)

    def to_config(self) -> dict:
        data = {
            'label': self.label,
            'enabledSteps': list(self.enabled_steps),
        }
        if self.enabled_devices:
            data['enabledDevices'] = {
                step_id: list(devices)
                for step_id, devices in self.enabled_devices.items()
            }
        return data


@dataclass(frozen=True)
class MoNaLISAProfileDefinition:
    id: str
    label: str
    steps: Tuple[MoNaLISASequenceStepDefinition, ...]
    modes: Mapping[str, MoNaLISAModeDefinition]
    default_mode: str
    ulens_period_um: Optional[float] = None
    scan_extent_source: str = 'ulens'
    manual_scan_extent_um: Optional[float] = None
    lateral_geometry_mode: str = 'coupled'
    default_lateral_step_um: Optional[float] = None
    default_x_step_um: Optional[float] = None
    default_y_step_um: Optional[float] = None
    manual_scan_extent_x_um: Optional[float] = None
    manual_scan_extent_y_um: Optional[float] = None
    default_z_step_um: Optional[float] = None
    default_z_scan_length_um: Optional[float] = None
    camera_readout_time_s: Optional[float] = None

    @classmethod
    def from_config(cls, profile_id: str, data: Mapping[str, Any]):
        raw_steps = data.get('steps') or ()
        steps = []
        for index, raw_step in enumerate(raw_steps):
            if not isinstance(raw_step, Mapping):
                raise MoNaLISAProfileError(
                    f'Profile {profile_id!r} step {index + 1} must be an object.'
                )
            step_id = raw_step.get('id')
            if not step_id:
                raise MoNaLISAProfileError(
                    f'Profile {profile_id!r} step {index + 1} needs an id.'
                )
            steps.append(MoNaLISASequenceStepDefinition.from_config(step_id, raw_step))

        raw_modes = data.get('modes') or {}
        if not isinstance(raw_modes, Mapping):
            raise MoNaLISAProfileError(f'Profile {profile_id!r} modes must be an object.')
        modes = {
            str(mode_id): MoNaLISAModeDefinition.from_config(str(mode_id), mode_data or {})
            for mode_id, mode_data in raw_modes.items()
        }
        default_mode = str(data.get('defaultMode') or (next(iter(modes), ''))).strip()
        raw_ulens = data.get('ulensPeriodUm')
        ulens_period_um = None if raw_ulens in (None, '') else float(raw_ulens)
        scan_extent_source = str(data.get('scanExtentSource') or 'ulens').strip().lower()
        raw_extent = data.get('scanExtentUm')
        manual_scan_extent_um = None if raw_extent in (None, '') else float(raw_extent)
        lateral_geometry_mode = str(data.get('lateralGeometryMode') or 'coupled').strip().lower()
        raw_default_step = data.get('defaultLateralStepUm')
        default_lateral_step_um = None if raw_default_step in (None, '') else float(raw_default_step)
        raw_default_x_step = data.get('defaultXStepUm')
        default_x_step_um = None if raw_default_x_step in (None, '') else float(raw_default_x_step)
        raw_default_y_step = data.get('defaultYStepUm')
        default_y_step_um = None if raw_default_y_step in (None, '') else float(raw_default_y_step)
        raw_extent_x = data.get('scanExtentXUm')
        manual_scan_extent_x_um = (
            manual_scan_extent_um if raw_extent_x in (None, '') else float(raw_extent_x)
        )
        raw_extent_y = data.get('scanExtentYUm')
        manual_scan_extent_y_um = (
            manual_scan_extent_um if raw_extent_y in (None, '') else float(raw_extent_y)
        )
        raw_default_z_step = data.get('defaultZStepUm')
        default_z_step_um = (
            None if raw_default_z_step in (None, '') else float(raw_default_z_step)
        )
        raw_default_z_length = data.get('defaultZScanLengthUm')
        default_z_scan_length_um = (
            None if raw_default_z_length in (None, '') else float(raw_default_z_length)
        )
        raw_camera_readout = data.get('cameraReadoutTimeMs')
        camera_readout_time_s = (
            None if raw_camera_readout in (None, '')
            else float(raw_camera_readout) * 1e-3
        )
        profile = cls(
            id=str(profile_id).strip(),
            label=str(data.get('label') or profile_id).strip(),
            steps=tuple(steps),
            modes=modes,
            default_mode=default_mode,
            ulens_period_um=ulens_period_um,
            scan_extent_source=scan_extent_source,
            manual_scan_extent_um=manual_scan_extent_um,
            lateral_geometry_mode=lateral_geometry_mode,
            default_lateral_step_um=default_lateral_step_um,
            default_x_step_um=default_x_step_um,
            default_y_step_um=default_y_step_um,
            manual_scan_extent_x_um=manual_scan_extent_x_um,
            manual_scan_extent_y_um=manual_scan_extent_y_um,
            default_z_step_um=default_z_step_um,
            default_z_scan_length_um=default_z_scan_length_um,
            camera_readout_time_s=camera_readout_time_s,
        )
        errors = validate_profile_definition(profile)
        if errors:
            raise MoNaLISAProfileError('; '.join(errors))
        return profile

    def step(self, step_id: str) -> MoNaLISASequenceStepDefinition:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    def mode(self, mode_id: Optional[str] = None) -> MoNaLISAModeDefinition:
        key = str(mode_id or self.default_mode)
        try:
            return self.modes[key]
        except KeyError as exc:
            raise MoNaLISAProfileError(
                f'Profile {self.id!r} has no mode {key!r}.'
            ) from exc

    def enabled_steps(self, mode_id: Optional[str] = None) -> Tuple[MoNaLISASequenceStepDefinition, ...]:
        enabled = set(self.mode(mode_id).enabled_steps)
        return tuple(step for step in self.steps if step.id in enabled)

    def enabled_step_devices(
        self, mode_id: Optional[str] = None
    ) -> Tuple[Tuple[MoNaLISASequenceStepDefinition, Tuple[str, ...]], ...]:
        mode = self.mode(mode_id)
        return tuple(
            (step, mode.devices_for_step(step))
            for step in self.steps
            if step.id in mode.enabled_steps
        )

    def steps_with_role(self, role: str) -> Tuple[MoNaLISASequenceStepDefinition, ...]:
        role = str(role).lower()
        return tuple(step for step in self.steps if step.role == role)

    def to_config(self) -> dict:
        result = {
            'label': self.label,
            'defaultMode': self.default_mode,
            'steps': [
                {'id': step.id, **step.to_config()}
                for step in self.steps
            ],
            'modes': {
                mode_id: mode.to_config()
                for mode_id, mode in self.modes.items()
            },
        }
        if self.ulens_period_um is not None:
            result['ulensPeriodUm'] = self.ulens_period_um
        result['scanExtentSource'] = self.scan_extent_source
        result['lateralGeometryMode'] = self.lateral_geometry_mode
        if self.default_lateral_step_um is not None:
            result['defaultLateralStepUm'] = self.default_lateral_step_um
        if self.default_x_step_um is not None:
            result['defaultXStepUm'] = self.default_x_step_um
        if self.default_y_step_um is not None:
            result['defaultYStepUm'] = self.default_y_step_um
        if self.default_z_step_um is not None:
            result['defaultZStepUm'] = self.default_z_step_um
        if self.default_z_scan_length_um is not None:
            result['defaultZScanLengthUm'] = self.default_z_scan_length_um
        if self.scan_extent_source == 'manual':
            if self.lateral_geometry_mode == 'independent':
                if self.manual_scan_extent_x_um is not None:
                    result['scanExtentXUm'] = self.manual_scan_extent_x_um
                if self.manual_scan_extent_y_um is not None:
                    result['scanExtentYUm'] = self.manual_scan_extent_y_um
                if (self.manual_scan_extent_x_um is not None and self.manual_scan_extent_y_um is not None
                        and self.manual_scan_extent_x_um == self.manual_scan_extent_y_um):
                    result['scanExtentUm'] = self.manual_scan_extent_x_um
            elif self.manual_scan_extent_um is not None:
                result['scanExtentUm'] = self.manual_scan_extent_um
        if self.camera_readout_time_s is not None:
            result['cameraReadoutTimeMs'] = self.camera_readout_time_s * 1e3
        return result


@dataclass(frozen=True)
class MoNaLISAProfileStore:
    profiles: Mapping[str, MoNaLISAProfileDefinition]
    default_profile: str

    @classmethod
    def from_config(cls, configured: Mapping[str, Any]) -> 'MoNaLISAProfileStore':
        """Parse only explicit profile definitions.

        An empty profile set is a valid setup state: SimpleMoNaLISA stays
        disabled until the user creates a profile from the profile menu.
        No legacy ON/OFF/readout mapping is inferred or migrated here.
        """
        raw_profiles = configured.get('profiles', {})
        if raw_profiles is None:
            raw_profiles = {}
        if not isinstance(raw_profiles, Mapping):
            raise MoNaLISAProfileError('simpleMoNaLISA.profiles must be an object.')
        profiles = {
            str(profile_id): MoNaLISAProfileDefinition.from_config(str(profile_id), raw or {})
            for profile_id, raw in raw_profiles.items()
        }
        if not profiles:
            return cls(profiles={}, default_profile='')

        default_profile = str(
            configured.get('defaultProfile') or next(iter(profiles))
        )
        if default_profile not in profiles:
            raise MoNaLISAProfileError(
                f'Default MoNaLISA profile {default_profile!r} does not exist.'
            )
        return cls(profiles=profiles, default_profile=default_profile)

    def profile(self, profile_id: Optional[str] = None) -> MoNaLISAProfileDefinition:
        key = str(profile_id or self.default_profile)
        try:
            return self.profiles[key]
        except KeyError as exc:
            raise MoNaLISAProfileError(f'No MoNaLISA profile {key!r}.') from exc

    def with_profile(
        self, profile: MoNaLISAProfileDefinition, *, make_default: bool = False
    ) -> 'MoNaLISAProfileStore':
        """Return a store with ``profile`` added or atomically replaced."""
        errors = validate_profile_definition(profile)
        if errors:
            raise MoNaLISAProfileError('; '.join(errors))
        profiles = dict(self.profiles)
        profiles[profile.id] = profile
        default_profile = profile.id if make_default else self.default_profile
        if default_profile not in profiles:
            default_profile = profile.id
        return MoNaLISAProfileStore(profiles=profiles, default_profile=default_profile)

    def without_profile(self, profile_id: str) -> 'MoNaLISAProfileStore':
        """Return a store without one profile; an empty store is valid."""
        profile_id = str(profile_id)
        if profile_id not in self.profiles:
            raise MoNaLISAProfileError(f'No MoNaLISA profile {profile_id!r}.')
        profiles = dict(self.profiles)
        del profiles[profile_id]
        default_profile = self.default_profile
        if not profiles:
            default_profile = ''
        elif default_profile == profile_id:
            default_profile = next(iter(profiles))
        return MoNaLISAProfileStore(profiles=profiles, default_profile=default_profile)

    def relabel_profile(self, profile_id: str, label: str) -> 'MoNaLISAProfileStore':
        profile = self.profile(profile_id)
        label = str(label).strip()
        if not label:
            raise MoNaLISAProfileError('Profile label cannot be empty.')
        return self.with_profile(replace(profile, label=label))

    def missing_devices(
        self, available_devices: Iterable[str], *, profile_id: Optional[str] = None
    ) -> Tuple[str, ...]:
        available = set(str(name) for name in available_devices)
        missing = []
        profiles = (self.profile(profile_id),) if profile_id is not None else self.profiles.values()
        for profile in profiles:
            for step in profile.steps:
                if not step.ttl_devices:
                    missing.append(
                        f'profile {profile.label!r}, step {step.label!r}: no TTL device configured'
                    )
                for device in step.ttl_devices:
                    if device not in available:
                        missing.append(
                            f'profile {profile.label!r}, step {step.label!r}: TTL device {device!r}'
                        )
        return tuple(dict.fromkeys(missing))

    def to_config(self) -> dict:
        result = {
            'profiles': {
                profile_id: profile.to_config()
                for profile_id, profile in self.profiles.items()
            },
        }
        if self.default_profile:
            result['defaultProfile'] = self.default_profile
        return result


LEGACY_MONALISA_PROFILE_KEYS = (
    'onLaser', 'offLaser', 'readoutLaser', 'readoutDetector',
    'defaultMode', 'defaultOnTimeMs', 'defaultOffTimeMs', 'readoutTimeMs',
)


def config_with_profile_store(
    configured: Optional[Mapping[str, Any]], store: MoNaLISAProfileStore
) -> dict:
    """Merge a runtime store into ``simpleMoNaLISA`` setup configuration.

    Setup-level microscope facts are preserved. Removed pre-profile mapping
    keys are dropped so a profile save also cleans an older setup file.
    """
    result = dict(configured or {})
    for key in LEGACY_MONALISA_PROFILE_KEYS:
        result.pop(key, None)
    result.pop('defaultProfile', None)
    result.update(store.to_config())
    return result


def validate_profile_definition(profile: MoNaLISAProfileDefinition) -> Tuple[str, ...]:
    errors = []
    if not profile.id:
        errors.append('Profile id cannot be empty')
    if not profile.steps:
        errors.append(f'Profile {profile.id!r} needs at least one sequence step')
    if profile.ulens_period_um is not None and profile.ulens_period_um <= 0:
        errors.append(f'Profile {profile.id!r} uLens period must be positive')
    if profile.camera_readout_time_s is not None and profile.camera_readout_time_s < 0:
        errors.append(f'Profile {profile.id!r} camera readout time cannot be negative')
    if profile.scan_extent_source not in MONALISA_SCAN_EXTENT_SOURCES:
        errors.append(
            f'Profile {profile.id!r} scan extent source must be one of '
            + ', '.join(MONALISA_SCAN_EXTENT_SOURCES)
        )
    if profile.lateral_geometry_mode not in MONALISA_LATERAL_GEOMETRY_MODES:
        errors.append(
            f'Profile {profile.id!r} lateral geometry mode must be one of '
            + ', '.join(MONALISA_LATERAL_GEOMETRY_MODES)
        )
    for label, value in (
        ('default lateral step', profile.default_lateral_step_um),
        ('default X step', profile.default_x_step_um),
        ('default Y step', profile.default_y_step_um),
        ('default Z step', profile.default_z_step_um),
        ('default Z scan length', profile.default_z_scan_length_um),
    ):
        if value is not None and value <= 0:
            errors.append(f'Profile {profile.id!r} {label} must be positive')
    if profile.scan_extent_source == 'manual':
        if profile.lateral_geometry_mode == 'independent':
            x_extent = profile.manual_scan_extent_x_um
            y_extent = profile.manual_scan_extent_y_um
            if x_extent is None and y_extent is None and profile.manual_scan_extent_um is not None:
                x_extent = y_extent = profile.manual_scan_extent_um
            if x_extent is None or x_extent <= 0:
                errors.append(
                    f'Profile {profile.id!r} manual X scan extent must be positive'
                )
            if y_extent is None or y_extent <= 0:
                errors.append(
                    f'Profile {profile.id!r} manual Y scan extent must be positive'
                )
        elif profile.manual_scan_extent_um is None or profile.manual_scan_extent_um <= 0:
            errors.append(
                f'Profile {profile.id!r} manual scan extent must be positive'
            )

    step_ids = [step.id for step in profile.steps]
    duplicates = sorted({step_id for step_id in step_ids if step_ids.count(step_id) > 1})
    if duplicates:
        errors.append('Duplicate step ids: ' + ', '.join(duplicates))

    for step in profile.steps:
        if not step.id:
            errors.append('Step id cannot be empty')
        if step.role not in MONALISA_STEP_ROLES:
            errors.append(
                f'Step {step.id!r} role must be one of {", ".join(MONALISA_STEP_ROLES)}'
            )
        if not step.ttl_devices:
            errors.append(f'Step {step.id!r} needs at least one TTL device')
        if step.default_duration_s <= 0:
            errors.append(f'Step {step.id!r} duration must be positive')

    if not profile.modes:
        errors.append(f'Profile {profile.id!r} needs at least one mode')
    if profile.default_mode not in profile.modes:
        errors.append(
            f'Profile {profile.id!r} default mode {profile.default_mode!r} does not exist'
        )
    known_steps = set(step_ids)
    for mode_id, mode in profile.modes.items():
        if not mode.enabled_steps:
            errors.append(f'Mode {mode_id!r} needs at least one enabled step')
            continue
        unknown = [step_id for step_id in mode.enabled_steps if step_id not in known_steps]
        if unknown:
            errors.append(
                f'Mode {mode_id!r} references unknown steps: {", ".join(unknown)}'
            )
        unknown_overrides = [
            step_id for step_id in mode.enabled_devices if step_id not in known_steps
        ]
        if unknown_overrides:
            errors.append(
                f'Mode {mode_id!r} has device selections for unknown steps: '
                + ', '.join(unknown_overrides)
            )
        disabled_overrides = [
            step_id for step_id in mode.enabled_devices
            if step_id not in mode.enabled_steps
        ]
        if disabled_overrides:
            errors.append(
                f'Mode {mode_id!r} has device selections for disabled steps: '
                + ', '.join(disabled_overrides)
            )
        for step_id, devices in mode.enabled_devices.items():
            if step_id not in known_steps:
                continue
            step = profile.step(step_id)
            if not devices:
                errors.append(
                    f'Mode {mode_id!r} step {step_id!r} needs at least one enabled device'
                )
                continue
            invalid_devices = [
                device for device in devices if device not in step.ttl_devices
            ]
            if invalid_devices:
                errors.append(
                    f'Mode {mode_id!r} step {step_id!r} enables devices not in the step: '
                    + ', '.join(invalid_devices)
                )
    return tuple(errors)


def starter_monalisa_profile(
    profile_id: str, label: str, available_devices: Iterable[str]
) -> MoNaLISAProfileDefinition:
    """Create a minimal valid profile for the live editor's New action.

    It intentionally starts with one ON step and one mode rather than guessing
    a microscope-specific illumination recipe. The user can change the role
    (including CUSTOM) and add arbitrary steps before saving the dialog.
    """
    devices = _unique_strings(available_devices)
    if not devices:
        raise MoNaLISAProfileError(
            'A MoNaLISA profile needs at least one configured TTL device.'
        )
    return MoNaLISAProfileDefinition(
        id=str(profile_id),
        label=str(label).strip() or str(profile_id),
        steps=(MoNaLISASequenceStepDefinition(
            id='on', label='ON', role='on', ttl_devices=(devices[0],),
            default_duration_s=1e-3, duration_editable=True,
        ),),
        modes={'default': MoNaLISAModeDefinition(
            id='default', label='Default', enabled_steps=('on',), enabled_devices={}
        )},
        default_mode='default',
        ulens_period_um=None,
        scan_extent_source='ulens',
        manual_scan_extent_um=None,
        lateral_geometry_mode='coupled',
        default_lateral_step_um=None,
        default_x_step_um=None,
        default_y_step_um=None,
        manual_scan_extent_x_um=None,
        manual_scan_extent_y_um=None,
        default_z_step_um=None,
        default_z_scan_length_um=None,
        camera_readout_time_s=None,
    )



@dataclass(frozen=True)
class CompiledMoNaLISASequence:
    active_step_ids: Tuple[str, ...]
    step_durations_s: Mapping[str, float]
    step_windows_s: Mapping[str, Tuple[float, float]]
    windows_by_device: Mapping[str, Tuple[Tuple[float, float], ...]]
    end_time_s: float


@dataclass(frozen=True)
class ParsedMoNaLISASequence:
    active_step_ids: Tuple[str, ...]
    step_durations_s: Mapping[str, float]
    step_windows_s: Mapping[str, Tuple[float, float]]
    end_time_s: float


def compile_profile_sequence(
    profile: MoNaLISAProfileDefinition,
    mode_id: str,
    step_durations_s: Mapping[str, float],
    *,
    inter_pulse_delay_s: float,
    sample_rate: float,
) -> CompiledMoNaLISASequence:
    """Compile one profile/mode into device timing windows.

    Modes enable a subset of the profile's ordered steps; enabledSteps does not
    reorder them. Multiple devices on one step receive the same window. An
    enabledDevices override can restrict a step to only some of its devices.
    """

    mode = profile.mode(mode_id)
    active = profile.enabled_step_devices(mode.id)
    if not active:
        raise MoNaLISAProfileError(f'Mode {mode.id!r} has no active steps.')

    delay = _snap_time_s(max(0.0, inter_pulse_delay_s), sample_rate)
    t = delay
    windows: Dict[str, list] = {}
    durations: Dict[str, float] = {}
    step_windows: Dict[str, Tuple[float, float]] = {}

    for index, (step, active_devices) in enumerate(active):
        if not active_devices:
            raise MoNaLISAProfileError(
                f'Active step {step.label!r} has no enabled TTL devices.'
            )
        requested = step_durations_s.get(step.id, step.default_duration_s)
        # A readout step plays the setup's readout pulse time (the plan
        # carries it), whatever the profile says about editing; any other
        # fixed step plays its profile default.
        fixed = not step.duration_editable and step.role != 'readout'
        duration = step.default_duration_s if fixed else float(requested)
        duration = _snap_time_s(max(0.0, duration), sample_rate)
        if duration <= 0:
            raise MoNaLISAProfileError(
                f'Active step {step.label!r} must have a positive duration.'
            )
        start = t
        end = start + duration
        durations[step.id] = duration
        step_windows[step.id] = (start, end)
        for device in active_devices:
            windows.setdefault(device, []).append((start, end))
        t = end
        if index + 1 < len(active):
            t += delay

    return CompiledMoNaLISASequence(
        active_step_ids=tuple(step.id for step, _devices in active),
        step_durations_s=durations,
        step_windows_s=step_windows,
        windows_by_device={name: tuple(values) for name, values in windows.items()},
        end_time_s=t,
    )


def _device_windows(
    digital: Mapping[str, Any], device: str
) -> Tuple[Tuple[float, float], ...]:
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
        raise MoNaLISASequenceNotCompatible(
            f'{device} has unmatched timing-window starts and ends.'
        )
    return tuple(sorted(
        (float(start), float(end))
        for start, end in zip(starts0, ends0)
        if float(end) > float(start)
    ))


def parse_profile_sequence(
    digital: Mapping[str, Any],
    profile: MoNaLISAProfileDefinition,
    mode_id: str,
    *,
    inter_pulse_delay_s: float,
    sample_rate: float,
) -> ParsedMoNaLISASequence:
    """Parse Advanced timing against exactly one selected profile/mode.

    This function never searches other profiles or modes.  That property is
    what later lets the Simple/Advanced binding remain explicit and predictable.
    """

    active = profile.enabled_step_devices(mode_id)
    if not active:
        raise MoNaLISASequenceNotCompatible(f'Mode {mode_id!r} has no active steps.')

    tolerance = 1.0 / float(sample_rate) + 1e-9
    delay = _snap_time_s(max(0.0, inter_pulse_delay_s), sample_rate)

    all_devices = set(digital.get('target_device') or ())
    all_devices.update((digital.get('pulse_starts_s') or {}).keys())
    actual_windows = {
        str(device): _device_windows(digital, str(device))
        for device in all_devices
    }
    actual_windows = {name: windows for name, windows in actual_windows.items() if windows}

    expected_devices = {
        device for _step, devices in active for device in devices
    }
    strangers = sorted(set(actual_windows) - expected_devices)
    if strangers:
        raise MoNaLISASequenceNotCompatible(
            'Sequence fires devices outside the selected profile/mode: '
            + ', '.join(strangers) + '.'
        )

    positions = {device: 0 for device in expected_devices}
    durations: Dict[str, float] = {}
    step_windows: Dict[str, Tuple[float, float]] = {}
    expected_start = delay

    for step, active_devices in active:
        if not active_devices:
            raise MoNaLISASequenceNotCompatible(
                f'Step {step.label!r} has no enabled TTL devices.'
            )
        chosen = []
        for device in active_devices:
            windows = actual_windows.get(device, ())
            index = positions.get(device, 0)
            if index >= len(windows):
                raise MoNaLISASequenceNotCompatible(
                    f'Step {step.label!r} is missing TTL activity on {device!r}.'
                )
            window = windows[index]
            positions[device] = index + 1
            chosen.append(window)

        start, end = chosen[0]
        if not all(
            abs(other[0] - start) <= tolerance and abs(other[1] - end) <= tolerance
            for other in chosen[1:]
        ):
            raise MoNaLISASequenceNotCompatible(
                f'Devices in step {step.label!r} are not synchronized.'
            )
        if abs(start - expected_start) > tolerance:
            raise MoNaLISASequenceNotCompatible(
                f'Step {step.label!r} does not start at the profile-defined time.'
            )
        duration = end - start
        if duration <= 0:
            raise MoNaLISASequenceNotCompatible(
                f'Step {step.label!r} has a non-positive duration.'
            )
        if not step.duration_editable and step.role != 'readout':
            fixed = _snap_time_s(step.default_duration_s, sample_rate)
            if abs(duration - fixed) > tolerance:
                raise MoNaLISASequenceNotCompatible(
                    f'Step {step.label!r} duration is fixed at {fixed * 1e3:g} ms.'
                )
        durations[step.id] = _snap_time_s(duration, sample_rate)
        step_windows[step.id] = (start, end)
        expected_start = end + delay

    for device, windows in actual_windows.items():
        consumed = positions.get(device, 0)
        if consumed != len(windows):
            raise MoNaLISASequenceNotCompatible(
                f'{device} has extra timing windows outside the selected profile/mode.'
            )

    end_time = max(window[1] for window in step_windows.values())
    return ParsedMoNaLISASequence(
        active_step_ids=tuple(step.id for step, _devices in active),
        step_durations_s=durations,
        step_windows_s=step_windows,
        end_time_s=end_time,
    )


__all__ = [
    'MONALISA_STEP_ROLES',
    'CompiledMoNaLISASequence',
    'MoNaLISAModeDefinition',
    'MoNaLISAProfileDefinition',
    'MoNaLISAProfileError',
    'MoNaLISAProfileStore',
    'MoNaLISASequenceNotCompatible',
    'MoNaLISASequenceStepDefinition',
    'ParsedMoNaLISASequence',
    'compile_profile_sequence',
    'parse_profile_sequence',
    'validate_profile_definition',
]
