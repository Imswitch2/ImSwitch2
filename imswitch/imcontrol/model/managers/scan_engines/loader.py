"""Build a setup's scan engines (docs/design/plans/
scan-engines-and-expert-scan.md §5.0).

The NI-DAQ engine is always there, built from the ``nidaq`` section, unless
the setup names an engine ``nidaq`` itself. Every ``scanEngines`` entry is
resolved through the device-plugin registry (kind ``scan_engine``), so a
board joins by its plugin. Channel ownership must be unambiguous: a channel
two engines claim is a setup error, raised when the setup loads.

An engine whose ``triggerInput`` is wired to a line of the engine the panel
runs on follows it (§5.4, M6): the panel then runs on a composite of the two,
which is the leader alone for every program that uses only its devices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional

from imswitch.imcontrol.model.plugins.registry import get_default_registry
from imswitch.imcontrol.model.scan_program.engine import (
    ScanEngine, SetupError, assign_owners, setup_channels,
)
from .NidaqScanEngine import NidaqScanEngine

NIDAQ = 'nidaq'


@dataclass
class ScanEngines:
    engines: Dict[str, ScanEngine]
    selected: str
    owners: Dict[str, Optional[str]] = field(default_factory=dict)
    #: The selected engine with the engines that follow it, when any do.
    composite: Optional[ScanEngine] = None

    @property
    def engine(self) -> ScanEngine:
        return self.composite or self.engines[self.selected]

    def owner_of(self, channel) -> Optional[str]:
        return self.owners.get(channel)

    def finalize(self):
        """Close every engine that holds a link (a step board's port)."""
        complete = True
        for engine in self.engines.values():
            finalize = getattr(engine, 'finalize', None)
            if callable(finalize) and finalize() is False:
                complete = False
        return complete


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
        engines[NIDAQ] = NidaqScanEngine(None, NIDAQ, nidaqManager=nidaqManager,
                                         setupInfo=setupInfo)
    for name, info in declared.items():
        engines[name] = _engine_class(info.managerName, registry)(info, name, **lowLevel)

    owners = assign_owners(list(engines.values()), setup_channels(setupInfo))

    scan = getattr(setupInfo, 'scan', None)
    selected = getattr(scan, 'engine', None)
    selected = selected if isinstance(selected, str) and selected else NIDAQ
    if selected not in engines:
        raise SetupError(f'scan.engine names {selected!r}; the setup has the scan engines '
                         f'{", ".join(sorted(engines))}.')
    followers = _followers(declared, engines, selected)
    composite = None
    if followers:
        from .CompositeScanEngine import CompositeScanEngine
        composite = CompositeScanEngine(engines[selected], followers)
    return ScanEngines(engines, selected, owners, composite)


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


__all__ = ['NIDAQ', 'ScanEngines', 'build_scan_engines']


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
