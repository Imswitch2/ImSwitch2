"""Matching calibrations to lasers, and recorded calibration identities to the
calibration in force. Pure functions, no Qt, no hardware.

The store is global, so a record found there may belong to another rig that
happens to name its laser the same. :func:`match_to_laser` grades a record
against one laser of the current setup:

``match``        same name, wavelength, drive unit (and manager / identifier
                 where both are known) -- offered for activation.
``name_differs`` everything physical agrees but the laser is named
                 differently -- a warning and an explicit confirmation
                 ("made for '488 (EXC)', applying to 'Ex488'").
``other_device`` the wavelength, manager class or identifier differs -- a
                 strong warning; "use anyway" stays possible (two rigs may
                 share a laser model).
``incompatible`` the drive units differ or the record is not a laser power
                 calibration -- blocked: its numbers would mean something
                 else.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional

from .record import KIND_LASER_POWER, Calibration

MATCH = 'match'
NAME_DIFFERS = 'name_differs'
OTHER_DEVICE = 'other_device'
INCOMPATIBLE = 'incompatible'

#: Wavelengths closer than this are the same line (rounding, nominal vs measured).
WAVELENGTH_TOLERANCE_NM = 2.0


@dataclass(frozen=True)
class LaserDescriptor:
    """What the current setup knows about one laser."""
    name: str
    wavelengthNm: Optional[float]
    driveUnit: str
    managerClass: str = ''
    identifier: str = ''
    supportsCalibration: bool = True


@dataclass(frozen=True)
class LaserMatch:
    level: str
    messages: List[str] = field(default_factory=list)

    @property
    def offered(self) -> bool:
        """Listed as this laser's own calibration."""
        return self.level == MATCH

    @property
    def needsConfirmation(self) -> bool:
        return self.level in (NAME_DIFFERS, OTHER_DEVICE)

    @property
    def blocked(self) -> bool:
        return self.level == INCOMPATIBLE

    def text(self) -> str:
        return ' '.join(self.messages)


def match_to_laser(record: Calibration, laser: LaserDescriptor) -> LaserMatch:
    if record.kind != KIND_LASER_POWER:
        return LaserMatch(INCOMPATIBLE, [f'It is a {record.kind!r} calibration, not a laser '
                                         f'power calibration.'])
    if not laser.supportsCalibration:
        return LaserMatch(INCOMPATIBLE, [f'{laser.name} runs on raw units only: its manager '
                                         f'({laser.managerClass}) does not apply calibrations.'])
    unit = (record.input.unit or '').strip()
    if unit and laser.driveUnit and unit != laser.driveUnit:
        return LaserMatch(INCOMPATIBLE, [
            f'Its drive is in {unit!r}, but {laser.name} is driven in {laser.driveUnit!r}: '
            f'the numbers would mean something else.'])
    strong: List[str] = []
    wl = record.device.wavelengthNm
    if wl is not None and laser.wavelengthNm is not None:
        if math.fabs(float(wl) - float(laser.wavelengthNm)) > WAVELENGTH_TOLERANCE_NM:
            strong.append(f'It was measured at {wl:g} nm; {laser.name} is a '
                          f'{float(laser.wavelengthNm):g} nm laser.')
    if record.device.managerClass and laser.managerClass \
            and record.device.managerClass != laser.managerClass:
        strong.append(f'It was made for a {record.device.managerClass}; {laser.name} is a '
                      f'{laser.managerClass}.')
    if record.device.identifier and laser.identifier \
            and record.device.identifier != laser.identifier:
        strong.append(f'It was made for device {record.device.identifier!r}; {laser.name} is '
                      f'{laser.identifier!r}.')
    name = record.device.name
    name_note = []
    if name and name != laser.name:
        name_note.append(f"Made for '{name}', applying to '{laser.name}'.")
    elif not name:
        name_note.append('It does not say which laser it was made for.')
    if strong:
        return LaserMatch(OTHER_DEVICE, name_note + strong)
    if name_note:
        return LaserMatch(NAME_DIFFERS, name_note)
    return LaserMatch(MATCH, [])


# -- recorded identity vs the calibration in force --------------------------

SAME = 'same'
DIFFERENT = 'different'
UNKNOWN = 'unknown'


@dataclass(frozen=True)
class IdentityComparison:
    status: str
    message: str


def compare_calibration_identity(recorded: Any, active: Optional[Mapping[str, Any]], *,
                                 recorded_known: bool = True) -> IdentityComparison:
    """Compare a recorded calibration identity ``{id, contentHash,
    driveValue}`` (or ``None`` = recorded on raw units) with the one in force
    now (``None`` = raw units).

    ``recorded_known=False`` means the source predates calibration
    provenance (no slot at all): the answer is ``unknown``. Identities with
    the same content hash are the same calibration even under different ids
    (a legacy file and its imported copy)."""
    if not recorded_known:
        return IdentityComparison(UNKNOWN, 'The saved value does not record which calibration '
                                           'it was set under.')
    if recorded is not None and not isinstance(recorded, Mapping):
        return IdentityComparison(UNKNOWN, f'Unreadable calibration record: {recorded!r}.')
    if recorded is None and active is None:
        return IdentityComparison(SAME, 'Saved and applied on raw units.')
    if recorded is None:
        return IdentityComparison(DIFFERENT, f'Saved on raw units; calibration '
                                             f'{active.get("id")} is in force now.')
    if active is None:
        return IdentityComparison(DIFFERENT, f'Saved under calibration {recorded.get("id")}; '
                                             f'the laser runs on raw units now.')
    if recorded.get('contentHash') and recorded.get('contentHash') == active.get('contentHash'):
        if recorded.get('id') == active.get('id'):
            return IdentityComparison(SAME, f'Saved under the calibration in force '
                                            f'({active.get("id")}).')
        return IdentityComparison(SAME, f'Saved under {recorded.get("id")}, which has the same '
                                        f'content as {active.get("id")} in force now.')
    if recorded.get('id') == active.get('id'):
        return IdentityComparison(DIFFERENT, f'Calibration {active.get("id")} has changed '
                                             f'since the value was saved.')
    return IdentityComparison(DIFFERENT, f'Saved under calibration {recorded.get("id")}; '
                                         f'{active.get("id")} is in force now.')


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
