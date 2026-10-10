"""The scan plan behind the SimplePointScan panel.

Pure and Qt-free (docs/simple-point-scan-plan.md §5.1). The panel edits a
:class:`SimpleScanPlan`; :func:`plan_to_dicts` turns it into the two parameter
dicts the Advanced scan path executes, and :func:`dicts_to_plan` turns such
dicts back into a plan -- within the representable subset only (plan D4):
anything the panel cannot show is refused with the reason, never normalized.

:class:`ScanLimits` derives everything the panel offers from the setup file:
the scanners' ranges and speed limits, the sample rate, which lasers have a
digital gate and which of those an analog channel. :func:`plan_overview`
chooses the overview's field, pixel count and dwell together (plan D6).

:class:`PointScanCloak` is the same translation as the panel's
:class:`~imswitch.imcontrol.model.scan_cloak.ScanCloak` over the Advanced
scan panel (plan §10).
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from .scan_cloak import PlanNotRepresentable, ScanCloak
from .scan_parameters import pixels_for_length_step
from .signaldesigners.GalvoScanDesigner import (
    fast_axis_speed_limit,
    is_smooth_scan_axis,
)

#: ``scan.simplePointScan`` keys and their defaults (plan §5.6).
CONFIG_DEFAULTS: Dict[str, Any] = {
    'objectiveNA': None,
    'nyquistPixelSizeUm': None,
    'overviewAxes': None,
    'overviewFieldUm': 60.0,
    'overviewMinFieldUm': 2.0,
    'overviewMinPixels': 64,
    'overviewMaxPixels': 512,
    'overviewFrameTimeS': 1.0,
    'minSamplesPerPixel': 2,
    'maxDwellMs': 10.0,
}

#: Field an axis without a configured voltage range offers (mock axes).
_UNBOUNDED_FIELD_UM = 200.0


# ---------------------------------------------------------------------------
# What the setup offers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AxisLimits:
    """One scanning positioner as the panel sees it."""

    name: str
    label: str
    smooth: bool
    range_um: Optional[Tuple[float, float]]
    speed_limit: Optional[float]  # µm/µs when swept as the fast axis

    @property
    def centre_um(self) -> float:
        if self.range_um is None:
            return 0.0
        return (self.range_um[0] + self.range_um[1]) / 2.0

    @property
    def width_um(self) -> Optional[float]:
        if self.range_um is None:
            return None
        return self.range_um[1] - self.range_um[0]


@dataclass(frozen=True)
class LaserGate:
    """A laser the scan can gate (it has a digital line)."""

    name: str
    wavelength_nm: Optional[float]
    power_capable: bool  # its own analogChannel: per-channel power (plan D5)


@dataclass(frozen=True)
class ScanLimits:
    axes: Tuple[AxisLimits, ...]
    gates: Tuple[LaserGate, ...]
    ttl_devices: Tuple[str, ...]
    sample_rate: float
    config: Mapping[str, Any]

    @classmethod
    def from_setup(cls, setupInfo) -> 'ScanLimits':
        axes = []
        for name, info in setupInfo.positioners.items():
            if not info.forScanning:
                continue
            props = dict(info.managerProperties or {})
            conversion = float(props.get('conversionFactor', 1) or 1)
            low, high = props.get('minVolt'), props.get('maxVolt')
            span = None
            if low is not None and high is not None:
                ends = (float(low) * conversion, float(high) * conversion)
                span = (min(ends), max(ends))
            labels = list(getattr(info, 'axes', None) or [])
            axes.append(AxisLimits(
                name=name,
                label=str(labels[0]) if labels else name,
                smooth=is_smooth_scan_axis(name, props),
                range_um=span,
                speed_limit=fast_axis_speed_limit(name, props),
            ))
        ttl = setupInfo.getTTLDevices()
        gates = []
        for name, info in (getattr(setupInfo, 'lasers', None) or {}).items():
            if name not in ttl:
                continue
            analog = getattr(info, 'analogChannel', None)
            gates.append(LaserGate(
                name=name,
                wavelength_nm=getattr(info, 'wavelength', None),
                power_capable=analog not in (None, 'None'),
            ))
        config = dict(CONFIG_DEFAULTS)
        config.update(getattr(setupInfo.scan, 'simplePointScan', None) or {})
        return cls(
            axes=tuple(axes),
            gates=tuple(gates),
            ttl_devices=tuple(ttl),
            sample_rate=float(setupInfo.scan.sampleRate),
            config=config,
        )

    def axis(self, name: str) -> AxisLimits:
        for axis in self.axes:
            if axis.name == name:
                return axis
        raise KeyError(name)

    def gate(self, name: str) -> LaserGate:
        for gate in self.gates:
            if gate.name == name:
                return gate
        raise KeyError(name)

    @property
    def axis_names(self) -> Tuple[str, ...]:
        return tuple(axis.name for axis in self.axes)

    @property
    def overview_axes(self) -> Tuple[str, ...]:
        configured = self.config.get('overviewAxes')
        if configured:
            return tuple(configured)[:2]
        return self.axis_names[:2]

    @property
    def max_dwell_s(self) -> float:
        return float(self.config['maxDwellMs']) * 1e-3

    def min_dwell_s(self, fast_axis: str, step_um: float) -> float:
        """Shortest dwell: enough samples per pixel, and no faster a sweep
        than the fast axis allows (the designer refuses anything faster)."""
        floor = int(self.config['minSamplesPerPixel']) / self.sample_rate
        limit = self.axis(fast_axis).speed_limit
        if limit:
            floor = max(floor, abs(float(step_um)) / limit * 1e-6)
        return snap_dwell_s(floor, self.sample_rate, minimum=floor)

    def overview_reach_um(self) -> float:
        """The largest overview the scanners' ranges allow: the narrowest
        overview axis, from its voltage range (the turnaround needs some of
        it, which the planner accounts for)."""
        widths = [self.axis(name).width_um for name in self.overview_axes]
        widths = [width for width in widths if width is not None]
        return min(widths) if widths else _UNBOUNDED_FIELD_UM

    def overview_size_range_um(self) -> Tuple[float, float]:
        """(smallest, largest) overview the panel offers."""
        reach = self.overview_reach_um()
        smallest = float(self.config.get('overviewMinFieldUm') or 2.0)
        return min(smallest, reach), reach

    def overview_default_field_um(self) -> float:
        """The overview's size until the panel's slider says otherwise:
        ``overviewFieldUm`` (60 µm unless the setup sets it), within reach."""
        low, high = self.overview_size_range_um()
        configured = self.config.get('overviewFieldUm') or CONFIG_DEFAULTS['overviewFieldUm']
        return min(max(float(configured), low), high)

    def nyquist_um(self, wavelengths_nm: Sequence[float]) -> Tuple[Optional[float], str]:
        """The finest useful pixel, and where the number comes from."""
        configured = self.config.get('nyquistPixelSizeUm')
        if configured:
            return float(configured), 'scan.simplePointScan.nyquistPixelSizeUm'
        numerical_aperture = self.config.get('objectiveNA')
        wavelengths = [float(w) for w in wavelengths_nm if w]
        if numerical_aperture and wavelengths:
            shortest = min(wavelengths)
            return (
                shortest / 1000.0 / (8.0 * float(numerical_aperture)),
                f'{shortest:g} nm / (8 x NA {float(numerical_aperture):g})',
            )
        return None, 'no objectiveNA or nyquistPixelSizeUm in scan.simplePointScan'


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AxisRegion:
    center_um: float
    length_um: float
    step_um: float

    @property
    def pixels(self) -> int:
        return pixels_for_length_step(self.length_um, self.step_um)


@dataclass(frozen=True)
class SimpleScanPlan:
    """One acquisition as the panel shows it.

    ``dims`` are the scanned positioners, fast axis first. ``channels`` are
    the line passes: each a tuple of lasers fired together. ``park`` holds the
    centre of every scanning positioner that is not scanned. Channel power
    serializes as Advanced's own ``advanced_mode`` with empty pulse windows
    (plan D5), keyed per gate.
    """

    dims: Tuple[str, ...]
    regions: Mapping[str, AxisRegion]
    dwell_s: float
    channels: Tuple[Tuple[str, ...], ...]
    park: Mapping[str, float] = field(default_factory=dict)
    channel_power_on: bool = False
    channel_power: Mapping[str, Tuple[float, ...]] = field(default_factory=dict)
    channel_power_enabled: Mapping[str, bool] = field(default_factory=dict)
    phase_delay_us: float = 0.0
    d3step_delay_us: float = 0.0
    frames: int = 1

    def pixels(self, dim: str) -> int:
        return self.regions[dim].pixels

    def lasers(self) -> Tuple[str, ...]:
        seen = []
        for lane in self.channels:
            for laser in lane:
                if laser not in seen:
                    seen.append(laser)
        return tuple(seen)

    def to_dict(self) -> dict:
        return {
            'dims': list(self.dims),
            'regions': {
                name: [r.center_um, r.length_um, r.step_um]
                for name, r in self.regions.items()
            },
            'dwell_s': self.dwell_s,
            'channels': [list(lane) for lane in self.channels],
            'park': dict(self.park),
            'channel_power_on': self.channel_power_on,
            'channel_power': {k: list(v) for k, v in self.channel_power.items()},
            'channel_power_enabled': dict(self.channel_power_enabled),
            'phase_delay_us': self.phase_delay_us,
            'd3step_delay_us': self.d3step_delay_us,
            'frames': self.frames,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'SimpleScanPlan':
        return cls(
            dims=tuple(data['dims']),
            regions={
                name: AxisRegion(float(c), float(length), float(step))
                for name, (c, length, step) in data['regions'].items()
            },
            dwell_s=float(data['dwell_s']),
            channels=tuple(tuple(lane) for lane in data['channels']),
            park={k: float(v) for k, v in (data.get('park') or {}).items()},
            channel_power_on=bool(data.get('channel_power_on', False)),
            channel_power={
                k: tuple(float(x) for x in v)
                for k, v in (data.get('channel_power') or {}).items()
            },
            channel_power_enabled={
                k: bool(v) for k, v in (data.get('channel_power_enabled') or {}).items()
            },
            phase_delay_us=float(data.get('phase_delay_us', 0.0)),
            d3step_delay_us=float(data.get('d3step_delay_us', 0.0)),
            frames=int(data.get('frames', 1)),
        )


def plan_to_dicts(plan: SimpleScanPlan, limits: ScanLimits) -> Tuple[dict, dict]:
    """The analog and digital dicts the Advanced scan path executes.

    Shaped like ``AdvancedScanParameterSerializer``'s output: scanned axes
    first, every other scanning positioner as a one-step dummy at its park
    centre; line steps are the channels.
    """
    names = list(limits.axis_names)
    dims = list(plan.dims)
    analog: Dict[str, Any] = {
        'target_device': [], 'axis_length': [], 'axis_step_size': [],
        'axis_centerpos': [], 'axis_startpos': [],
        'scan_dim_target_device': dims + ['None'] * (len(names) - len(dims)),
    }

    def add(name, length, step, centre):
        analog['target_device'].append(name)
        analog['axis_length'].append(float(length))
        analog['axis_step_size'].append(float(step))
        analog['axis_centerpos'].append(float(centre))
        analog['axis_startpos'].append([float(centre)])

    for name in dims:
        region = plan.regions[name]
        add(name, region.length_um, region.step_um, region.center_um)
    for name in names:
        if name not in dims:
            add(name, 1.0, 1.0, plan.park.get(name, 0.0))
    analog['sequence_time'] = float(plan.dwell_s)
    analog['phase_delay'] = plan.phase_delay_us
    analog['d3step_delay'] = plan.d3step_delay_us

    steps = len(plan.channels)
    fired = plan.lasers()
    targets = [name for name in limits.ttl_devices if name in fired]
    power_capable = [gate.name for gate in limits.gates if gate.power_capable]
    digital: Dict[str, Any] = {
        'target_device': targets,
        'n_linesteps': steps,
        'Nx': plan.pixels(dims[0]) if dims else 1,
        'Ny': plan.pixels(dims[1]) if len(dims) > 1 else 1,
        # Every TTL device, not only the fired ones: Advanced loads enables
        # and windows only for the devices a scan names and leaves the others
        # as they were, so a laser ticked there before would fire too. The
        # TTL designer, and what gets armed, read ``target_device`` only.
        'linestep_enable': {
            name: [name in lane for lane in plan.channels] for name in limits.ttl_devices
        },
        'pulse_starts_s': {name: [[] for _ in range(steps)] for name in limits.ttl_devices},
        'pulse_ends_s': {name: [[] for _ in range(steps)] for name in limits.ttl_devices},
        'sequence_time': float(plan.dwell_s),
        'advanced_mode': bool(plan.channel_power_on),
        'linestep_power_percent': {
            name: _per_step(plan.channel_power.get(name), steps)
            for name in power_capable
        },
        'linestep_power_enabled': {
            name: bool(plan.channel_power_enabled.get(name, True))
            for name in power_capable
        },
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


def _per_step(values, steps) -> list:
    values = [float(v) for v in (values or ())][:steps]
    return values + [values[-1] if values else 100.0] * (steps - len(values))


def dicts_to_plan(analog: Mapping[str, Any], digital: Mapping[str, Any],
                  limits: ScanLimits) -> SimpleScanPlan:
    """The plan for saved Advanced-style dicts, or :class:`PlanNotRepresentable`.

    Lengths, steps and centres are kept verbatim (plan D4): a length that is
    not a whole number of steps stays as it is.
    """
    digital = digital or {}
    windows = [
        step
        for key in ('pulse_starts_s', 'pulse_ends_s')
        for per_device in (digital.get(key) or {}).values()
        for step in per_device
        if step
    ]
    if digital.get('advanced_mode') and windows:
        raise PlanNotRepresentable(
            'This scan uses timing windows within the pixel; open it in the '
            'Advanced scan panel.'
        )
    rows = digital.get('advanced_sequence_rows') or {}
    if digital.get('advanced_program_mode') == 'sequence' and any(
            rows.values() if isinstance(rows, Mapping) else rows):
        raise PlanNotRepresentable(
            'This scan uses Sequence Builder rows; open it in the Advanced '
            'scan panel.'
        )
    if digital.get('intra_pixel_positioner_movement'):
        raise PlanNotRepresentable(
            'This scan moves a positioner within the pixel; open it in the '
            'Advanced scan panel.'
        )
    if any((digital.get('advanced_device_lock_master') or {}).values()):
        raise PlanNotRepresentable(
            'This scan locks devices to each other; open it in the Advanced '
            'scan panel.'
        )

    gates = {gate.name for gate in limits.gates}
    targets = list(digital.get('target_device') or [])
    others = [name for name in targets if name not in gates]
    if others:
        raise PlanNotRepresentable(
            f'This scan fires {", ".join(others)}, which the point-scan panel '
            'does not drive (only lasers); open it in the Advanced scan panel.'
        )
    steps = max(1, int(digital.get('n_linesteps', 1)))
    enable = digital.get('linestep_enable') or {}
    lanes = []
    for step in range(steps):
        lane = tuple(
            name for name in targets
            if step < len(enable.get(name) or ()) and enable[name][step]
        )
        if not lane:
            raise PlanNotRepresentable(
                f'Line pass {step + 1} of this scan fires no laser, which the '
                'point-scan panel cannot show; open it in the Advanced scan panel.'
            )
        lanes.append(lane)

    names = list(analog['target_device'])
    dims = [name for name in analog.get('scan_dim_target_device', []) if name and name != 'None']
    unknown = [name for name in names if name not in limits.axis_names]
    if unknown:
        raise PlanNotRepresentable(
            f'This scan drives {", ".join(unknown)}, which this setup does not '
            'scan with.'
        )
    regions = {}
    park = {}
    for index, name in enumerate(names):
        centre = float(analog['axis_centerpos'][index])
        if name in dims:
            regions[name] = AxisRegion(
                centre,
                float(analog['axis_length'][index]),
                float(analog['axis_step_size'][index]),
            )
        else:
            park[name] = centre

    power = {
        name: tuple(float(v) for v in values)
        for name, values in (digital.get('linestep_power_percent') or {}).items()
        if name in gates and limits.gate(name).power_capable
    }
    enabled = {
        name: bool(value)
        for name, value in (digital.get('linestep_power_enabled') or {}).items()
        if name in power
    }
    power, enabled = _canonical_power(power, enabled, len(lanes))
    return SimpleScanPlan(
        dims=tuple(dims),
        regions=regions,
        dwell_s=float(analog['sequence_time']),
        channels=tuple(lanes),
        park=park,
        channel_power_on=bool(digital.get('advanced_mode', False)),
        channel_power=power,
        channel_power_enabled=enabled,
        phase_delay_us=float(analog.get('phase_delay', 0.0) or 0.0),
        d3step_delay_us=float(analog.get('d3step_delay', 0.0) or 0.0),
    )


def power_refusal(plan: SimpleScanPlan, limits: ScanLimits) -> str:
    """Why the plan's channel power cannot be built, or ``''`` (plan D5)."""
    gates = {gate.name for gate in limits.gates}
    strangers = [name for name in plan.lasers() if name not in gates]
    if strangers:
        return (
            f'{", ".join(strangers)} cannot be fired by the scan: it has no '
            'digital line.'
        )
    if not plan.channel_power_on:
        return ''
    for name, values in plan.channel_power.items():
        if name not in gates or not limits.gate(name).power_capable:
            return (
                f'Channel power for {name}: its power has no analog channel, '
                'so it cannot change between channels. Set it in the Laser '
                'panel.'
            )
        outside = [v for v in values if not 0.0 <= float(v) <= 100.0]
        if outside:
            return f'Channel power for {name} must be 0-100 %, not {outside[0]:g} %.'
    return ''


# ---------------------------------------------------------------------------
# Snapping and slider mappings
# ---------------------------------------------------------------------------

def snap_dwell_s(dwell_s: float, sample_rate: float, *, minimum: float = 0.0) -> float:
    """``dwell_s`` as a whole number of samples, never below ``minimum``."""
    samples = max(1, round(float(dwell_s) * sample_rate))
    floor = math.ceil(float(minimum) * sample_rate - 1e-9)
    return max(samples, floor, 1) / sample_rate


#: Positions, lengths and steps are kept to 1 nm and delays to whole µs,
#: the precision the Advanced panel stores them with -- finer values would
#: make the same file build a different scan there (plan D4).
_UM_DECIMALS = 3


def quantize_um(value_um: float) -> float:
    return round(float(value_um), _UM_DECIMALS)


def quantize_step_um(step_um: float) -> float:
    return max(10.0 ** -_UM_DECIMALS, quantize_um(step_um))


def snap_length_um(length_um: float, step_um: float) -> float:
    """A length Simple creates: a whole number of steps (plan D4)."""
    step = quantize_step_um(step_um)
    return quantize_um(max(1, round(float(length_um) / step)) * step)


def normalize_plan(plan: 'SimpleScanPlan') -> 'SimpleScanPlan':
    """``plan`` on the grid the Advanced panel stores: 1 nm positions and
    steps, whole-µs delays. A length that already is on that grid is kept as
    it is, even when it is not a whole number of steps (an import)."""
    regions = {}
    for name, region in plan.regions.items():
        step = quantize_step_um(region.step_um)
        length = quantize_um(region.length_um)
        if step != region.step_um:
            length = snap_length_um(region.length_um, step)
        regions[name] = AxisRegion(quantize_um(region.center_um), length, step)
    normalized = dataclasses.replace(
        plan,
        regions=regions,
        park={name: quantize_um(value) for name, value in plan.park.items()},
        phase_delay_us=float(round(plan.phase_delay_us)),
        d3step_delay_us=float(round(plan.d3step_delay_us)),
    )
    power, enabled = _canonical_power(
        plan.channel_power, plan.channel_power_enabled, len(plan.channels))
    return dataclasses.replace(normalized, channel_power=power, channel_power_enabled=enabled)


def _canonical_power(power, enabled, steps):
    """Channel power as the dicts store it -- one value per channel -- and
    only for the lasers set off the defaults (full power, power control on).
    The dicts name every power-capable laser; a plan names only those."""
    enabled = {name: False for name, value in enabled.items() if not value}
    power = {
        name: tuple(_per_step(power.get(name), steps))
        for name in list(power) + [name for name in enabled if name not in power]
    }
    power = {
        name: values for name, values in power.items()
        if name in enabled or any(v != 100.0 for v in values)
    }
    return power, enabled


def log_value(position: float, low: float, high: float) -> float:
    """Slider ``position`` in [0, 1] to a value between ``low`` and ``high``."""
    position = min(1.0, max(0.0, float(position)))
    return math.exp(math.log(low) + position * (math.log(high) - math.log(low)))


def log_position(value: float, low: float, high: float) -> float:
    """The inverse of :func:`log_value`, clamped to [0, 1]."""
    if high == low:
        return 0.0
    position = (math.log(value) - math.log(low)) / (math.log(high) - math.log(low))
    return min(1.0, max(0.0, position))


# ---------------------------------------------------------------------------
# Overview planning (plan D6)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OverviewPlan:
    axes: Tuple[str, ...]
    centres_um: Tuple[float, ...]
    field_um: float
    pixels: int
    step_um: float
    dwell_s: float
    estimate_s: float
    met: bool
    note: str = ''


def plan_overview(
    limits: ScanLimits,
    *,
    estimate: Callable[[float, int, float, float], float],
    fits: Callable[[float, int, float, float], bool],
    budget_s: float,
    field_um: Optional[float] = None,
) -> OverviewPlan:
    """Pixel count and dwell for an overview of ``field_um`` (the panel's
    size; the setup's default when None).

    ``estimate(field, pixels, step, dwell)`` is the frame time and
    ``fits(field, pixels, step, dwell)`` whether the waveform stays inside
    the scanners' voltage ranges, turnaround included. Dwell is always the
    shortest allowed for the step. For a given field, fitting improves with
    more pixels (slower sweep, less overshoot) while time worsens, so the
    feasible pixel counts form an interval; the largest count within budget
    is taken.

    The size is the user's. It is kept when no pixel count meets the budget
    (the fastest feasible overview, with a note giving its time), and only
    reduced when the scanners cannot reach it: then to the largest size that
    fits, with a note saying so.
    """
    axes = limits.overview_axes
    centres = tuple(limits.axis(name).centre_um for name in axes)
    fast = axes[0]
    low = int(limits.config['overviewMinPixels'])
    high = max(low, int(limits.config['overviewMaxPixels']))
    smallest, reach = limits.overview_size_range_um()
    requested = float(field_um if field_um is not None
                      else limits.overview_default_field_um())
    requested = min(max(requested, smallest), reach)

    def sampling(field_, pixels):
        step = quantize_step_um(field_ / pixels)
        return step, limits.min_dwell_s(fast, step)

    def fitsAt(field_, pixels):
        return fits(field_, pixels, *sampling(field_, pixels))

    def timeAt(field_, pixels):
        return estimate(field_, pixels, *sampling(field_, pixels))

    def smallestFitting(field_):
        if not fitsAt(field_, high):
            return None
        lo, hi = low, high
        while lo < hi:
            mid = (lo + hi) // 2
            if fitsAt(field_, mid):
                hi = mid
            else:
                lo = mid + 1
        return lo

    def largestInBudget(field_):
        if timeAt(field_, low) > budget_s:
            return None
        lo, hi = low, high
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if timeAt(field_, mid) <= budget_s:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def result(field_, pixels, met, note=''):
        step, dwell = sampling(field_, pixels)
        return OverviewPlan(axes, centres, quantize_um(pixels * step), pixels,
                            step, dwell, timeAt(field_, pixels), met, note)

    def planAt(field_, note=''):
        """The overview at this size, or None when it does not fit."""
        fitting = smallestFitting(field_)
        if fitting is None:
            return None
        inBudget = largestInBudget(field_)
        if inBudget is not None and fitting <= inBudget:
            return result(field_, inBudget, True, note)
        slowest = result(field_, fitting, False)
        return dataclasses.replace(slowest, note=' '.join(filter(None, [
            note,
            f'At this size a frame takes about {slowest.estimate_s:.2g} s, more than '
            f'the {budget_s:.3g} s aimed for; a smaller overview is faster.',
        ])))

    overview = planAt(requested)
    if overview is not None:
        return overview

    # The scanners cannot reach the size asked for: the largest that fits.
    if smallestFitting(smallest) is None:
        return result(
            smallest, high, False,
            f'No overview fits the scanners\' voltage range down to '
            f'{smallest:.3g} µm; check the scan axes\' vel_max, acc_max and '
            f'volt limits.'
        )
    lo, hi = smallest, requested
    for _ in range(10):
        mid = (lo + hi) / 2.0
        if smallestFitting(mid) is None:
            hi = mid
        else:
            lo = mid
    return planAt(lo, note=(
        f'{requested:.3g} µm is beyond what the scanners reach with the turnaround; '
        f'the overview is {lo:.3g} µm.'
    ))


class PointScanCloak(ScanCloak):
    """The point-scan panel's plan over the Advanced scan panel."""

    def limits_from_setup(self, setupInfo) -> ScanLimits:
        return ScanLimits.from_setup(setupInfo)

    def to_backend(self, plan, limits):
        return plan_to_dicts(plan, limits)

    def from_backend(self, analog, digital, limits):
        return dicts_to_plan(analog, digital, limits)

    def normalize(self, plan):
        return normalize_plan(plan)

    def plan_to_dict(self, plan) -> dict:
        return plan.to_dict()

    def plan_from_dict(self, data):
        return SimpleScanPlan.from_dict(data)


__all__ = [
    'AxisLimits', 'AxisRegion', 'CONFIG_DEFAULTS', 'LaserGate', 'OverviewPlan',
    'PlanNotRepresentable', 'PointScanCloak', 'ScanLimits', 'SimpleScanPlan', 'dicts_to_plan',
    'log_position', 'log_value', 'plan_overview', 'plan_to_dicts',
    'normalize_plan', 'power_refusal', 'quantize_step_um', 'quantize_um',
    'snap_dwell_s', 'snap_length_um',
]
