"""A device's preparation after a barrier, from its setup entry
(docs/design/plans/scan-engines-and-expert-scan.md §2.4).

The ``preparation`` key is the time, in seconds, from the engine's output
transition that requests the device's state (the line's edge, or the
command) until the device is *ready*: it delivers that state as specified (a
shutter fully open, a laser emitting at its set level). It has three values,
never inferred from absence:

- ``"notRequired"``: ready at the transition;
- ``{"durationS": <seconds>}``: a known duration;
- ``"unknown"``: required, but not known -- a program that needs the device
  after a barrier is refused.

An absent key is not zero. In a setup with a ``scanEngines`` section (the new
format), an absent key on a device kind that can need preparation reads as
``"unknown"``. In a setup without one, the legacy rule ``legacy-preparation-v1``
applies: every device is ``"notRequired"``, which is what ImSwitch does
today, and the rule names itself as the source.

Qt-free.
"""

from __future__ import annotations

from typing import Mapping

from .interpreter import DeviceFacts

LEGACY_RULE = 'legacy-preparation-v1'

#: Device kinds whose state takes time to become ready after it is requested.
#: Positioners are not among them: their preparation is their Motion.
KINDS_THAT_CAN_NEED_PREPARATION = ('laser', 'shutter')


class PreparationError(ValueError):
    pass


def resolve_preparation(name: str, kind: str, entry: Mapping, *,
                        setup_has_scan_engines: bool) -> DeviceFacts:
    """The device's preparation, with where the value came from."""
    value = entry.get('preparation') if isinstance(entry, Mapping) else None
    if value is None:
        if not setup_has_scan_engines:
            return DeviceFacts('notRequired', f'{LEGACY_RULE} (setup without scanEngines)')
        if kind in KINDS_THAT_CAN_NEED_PREPARATION:
            return DeviceFacts('unknown', f'{name}: no preparation key (a {kind} can need one)')
        return DeviceFacts('notRequired', f'{name}: a {kind} needs no preparation')
    if value == 'notRequired':
        return DeviceFacts('notRequired', f'{name}.preparation')
    if value == 'unknown':
        return DeviceFacts('unknown', f'{name}.preparation')
    if isinstance(value, Mapping) and set(value) == {'durationS'}:
        duration = value['durationS']
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
            return DeviceFacts(float(duration), f'{name}.preparation.durationS')
    raise PreparationError(
        f'{name}.preparation must be "notRequired", "unknown" or {{"durationS": seconds}}, '
        f'not {value!r}.'
    )


__all__ = ['KINDS_THAT_CAN_NEED_PREPARATION', 'LEGACY_RULE', 'PreparationError',
           'resolve_preparation']


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
