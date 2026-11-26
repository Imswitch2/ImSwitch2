"""Build a setup's scan engines (docs/design/plans/
scan-engines-and-expert-scan.md §5.0).

The NI-DAQ engine is always there, built from the ``nidaq`` section, unless
the setup names an engine ``nidaq`` itself. Every ``scanEngines`` entry is
resolved through the device-plugin registry (kind ``scan_engine``), so a
board joins by its plugin. Channel ownership must be unambiguous: a channel
two engines claim is a setup error, raised when the setup loads.

An engine whose ``triggerInput`` is wired to a line of ``scan.engine`` follows
it (§5.4, M6): the two run one program together as a composite.

Which engine runs an Expert program is decided by its devices (§7.10,
``ScanEngines.for_program``): every channel is one engine's, so a program
whose devices are all on one engine runs there; one whose devices are on
the leader and its followers runs on the composite; any other spread is
refused with which device is on which engine. A program with no device on
an engine runs on ``scan.engine``. The panels that build sampled waveforms
(Advanced, SimplePointScan) run on ``scan.engine``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple

from imswitch.imcontrol.model.plugins.registry import get_default_registry
from imswitch.imcontrol.model.scan_program.engine import (
    ScanEngine, SetupError, assign_owners, setup_channels,
)
from .NidaqScanEngine import NidaqScanEngine
from .UnavailableScanEngine import UnavailableScanEngine

_log = logging.getLogger('imswitch')

NIDAQ = 'nidaq'


@dataclass
class ScanEngines:
    engines: Dict[str, ScanEngine]
    selected: str
    owners: Dict[str, Optional[str]] = field(default_factory=dict)
    #: The selected engine with the engines that follow it, when any do.
    composite: Optional[ScanEngine] = None
    #: ``{engine: reason}`` for every engine that could not be built; each is
    #: in ``engines`` as an UnavailableScanEngine that refuses its scans.
    failures: Dict[str, str] = field(default_factory=dict)

    @property
    def engine(self) -> ScanEngine:
        return self.composite or self.engines[self.selected]

    def owner_of(self, channel) -> Optional[str]:
        return self.owners.get(channel)

    def for_program(self, program, channels) -> 'EngineChoice':
        """Where ``program`` runs: on the engine its devices' channels are
        on, on the leader with its followers when they spread over those, on
        ``scan.engine`` when none is on an engine; else refused."""
        by_engine: Dict[str, list] = {}
        for device, used in sorted(program_channels(program).items()):
            entry = channels.get(device)
            if entry is None:
                continue
            lines = [entry.analog if 'analog' in used else None,
                     entry.digital if 'digital' in used else None]
            for owner in sorted({self.owners.get(c) for c in lines if c is not None}
                                - {None}):
                by_engine.setdefault(owner, []).append(device)
        devices = {name: tuple(found) for name, found in by_engine.items()}
        if not devices:
            return EngineChoice(self.engines[self.selected], devices)
        if len(devices) == 1:
            return EngineChoice(self.engines[next(iter(devices))], devices)
        if self.composite is not None and set(devices) <= {self.selected,
                                                           *self.composite.followers}:
            return EngineChoice(self.composite, devices)
        spread = '; '.join(f'{", ".join(found)} on {name}'
                           for name, found in sorted(devices.items()))
        return EngineChoice(None, devices, (
            f'Its devices are on engines that do not run together: {spread}. Engines run '
            'one program together only as scan.engine leading the engines wired to follow '
            'it (triggerInput).'))

    def finalize(self):
        """Close every engine that holds a link (a step board's port)."""
        complete = True
        for engine in self.engines.values():
            finalize = getattr(engine, 'finalize', None)
            if callable(finalize) and finalize() is False:
                complete = False
        return complete


@dataclass(frozen=True)
class EngineChoice:
    """Where a program runs (§7.10): ``engine``, or ``None`` with the
    ``refusal``; ``devices`` says which of its devices are on which engine."""
    engine: Optional[ScanEngine]
    devices: Dict[str, Tuple[str, ...]]
    refusal: str = ''


def program_devices(program) -> set:
    """Every device a program moves or switches."""
    return set(program_channels(program))


def program_channels(program) -> Dict[str, set]:
    """Each device a program uses and which of its channels it uses: a
    drive, a trajectory, a move or a power level take the analog one; a
    pulse, an exposure or a marker the digital one. A laser with both is on
    the engine of the channel the program actually switches."""
    if program is None:
        return {}
    used: Dict[str, set] = {}
    moved = (program.trajectory.devices if program.trajectory is not None
             else [drive.device for drive in program.drives])
    for device in moved:
        used.setdefault(device, set()).add('analog')
    for event in program.timeline:
        kind = 'analog' if event.kind in ('level', 'offset') else 'digital'
        used.setdefault(event.device, set()).add(kind)
    return used


def _claimed_channels(info, setupInfo):
    """The channels an engine that did not start would have driven, as far as
    the setup says: its ``channels``, else -- for an engine on a pulse generator
    -- every integer digital line of the setup (the generators' lines)."""
    props = dict(getattr(info, 'managerProperties', None) or {})
    if props.get('channels') is not None:
        return [str(c) for c in props['channels']]
    if 'pulseGenerator' in props:
        return [entry.digital for entry in setup_channels(setupInfo).values()
                if entry.digital is not None and str(entry.digital).isdigit()]
    return []


def nidaq_only_scan_engines(setupInfo, *, nidaqManager, lowLevelManagers: Mapping = None,
                            reason: str = '') -> 'ScanEngines':
    """The engines of a setup whose engine section could not be built at all:
    the NI-DAQ alone, with the reason kept in ``failures`` under ``scanEngines``."""
    lowLevel = dict(lowLevelManagers or {})
    engine = NidaqScanEngine(
        None, NIDAQ, nidaqManager=nidaqManager, setupInfo=setupInfo,
        laserPowerCalibration=lowLevel.get('laserPowerCalibration'))
    return ScanEngines({NIDAQ: engine}, NIDAQ, {}, None, {'scanEngines': reason})


def _engine_class(managerName, registry):
    cls = registry.load_manager_class('scan_engine', managerName)
    if cls is None:
        raise SetupError(registry.format_resolution_error('scan_engine', managerName))
    return cls


def build_scan_engines(setupInfo, *, nidaqManager, lowLevelManagers: Mapping = None,
                       registry=None) -> ScanEngines:
    registry = registry or get_default_registry()
    lowLevel = dict(lowLevelManagers or {})
    lowLevel.setdefault('nidaqManager', nidaqManager)
    lowLevel['setupInfo'] = setupInfo
    engines: Dict[str, ScanEngine] = {}
    declared = getattr(setupInfo, 'scanEngines', None)
    declared = dict(declared) if isinstance(declared, Mapping) else {}
    if NIDAQ not in declared:
        engines[NIDAQ] = NidaqScanEngine(
            None, NIDAQ, nidaqManager=nidaqManager, setupInfo=setupInfo,
            laserPowerCalibration=lowLevel.get('laserPowerCalibration'))
    failures: Dict[str, str] = {}
    for name, info in declared.items():
        try:
            engines[name] = _engine_class(info.managerName, registry)(info, name, **lowLevel)
        except Exception as error:      # a board that is not there must not stop startup
            failures[name] = f'{type(error).__name__}: {error}'
            engines[name] = UnavailableScanEngine(
                name, failures[name], _claimed_channels(info, setupInfo))
            _log.error('Scan engine %s did not start and is left unavailable: %s',
                       name, failures[name], exc_info=error)

    owners = assign_owners(list(engines.values()), setup_channels(setupInfo))

    scan = getattr(setupInfo, 'scan', None)
    selected = getattr(scan, 'engine', None)
    selected = selected if isinstance(selected, str) and selected else NIDAQ
    if selected not in engines:
        raise SetupError(f'scan.engine names {selected!r}; the setup has the scan engines '
                         f'{", ".join(sorted(engines))}.')
    followers = {name: pair for name, pair in _followers(declared, engines, selected).items()
                 if name not in failures}
    if selected in failures:
        followers = {}
    composite = None
    if followers:
        from .CompositeScanEngine import CompositeScanEngine
        composite = CompositeScanEngine(engines[selected], followers)
    return ScanEngines(engines, selected, owners, composite, failures)


def _followers(declared, engines, selected):
    """``{name: (engine, line)}`` for every engine wired to follow the
    selected one; a wire to any other engine's line is a setup error."""
    out = {}
    for name, info in declared.items():
        line = getattr(info, 'triggerInput', None)
        if not line:
            continue
        drivers = [other for other, engine in engines.items() if engine.owns(str(line))]
        if name == selected:
            raise SetupError(f'{name} is what the scan panel runs on (scan.engine), so it '
                             f'leads; its triggerInput {line!r} would have it follow.')
        if drivers != [selected]:
            driver = drivers[0] if drivers else 'no engine'
            raise SetupError(f'{name}\'s trigger input is wired to {line}, which {driver} '
                             f'drives; only {selected}, which the scan panel runs on, leads.')
        out[name] = (engines[name], str(line))
    return out


__all__ = ['NIDAQ', 'nidaq_only_scan_engines', 'EngineChoice', 'ScanEngines', 'build_scan_engines', 'program_channels',
           'program_devices']


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
