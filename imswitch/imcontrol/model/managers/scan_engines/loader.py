"""Build a setup's scan engines (docs/design/plans/
scan-engines-and-expert-scan.md §5.0).

The NI-DAQ engine is always there, built from the ``nidaq`` section, unless
the setup names an engine ``nidaq`` itself. Every ``scanEngines`` entry is
resolved through the device-plugin registry (kind ``scan_engine``), so a
board joins by its plugin. Channel ownership must be unambiguous: a channel
two engines claim is a setup error, raised when the setup loads.
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

    @property
    def engine(self) -> ScanEngine:
        return self.engines[self.selected]

    def owner_of(self, channel) -> Optional[str]:
        return self.owners.get(channel)


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
    return ScanEngines(engines, selected, owners)


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
