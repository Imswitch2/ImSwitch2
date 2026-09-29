"""Scan cloaks: a simpler panel over a full scan panel (docs/simple-point-scan-plan.md §10).

A cloak shows a microscope's scan in the few terms its users think in and
drives an existing scan panel, its *backend*, with them. The backend's own
controller runs every scan and its own widget is one switch away, so the
cloak adds no scan of its own. Its model, a :class:`ScanCloak`, translates
between the cloak's plan and the backend's two scan dicts, and says why when
the backend holds a scan the cloak cannot show.

Qt-free, so a cloak's translation is tested without a GUI.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping, Tuple

#: Every cloak panel, by its ``scan.scanWidgetType``, and the backend panel
#: type it drives. The scan manager, the controller and widget classes and
#: the config editor's options follow from here (plan §10.5).
SCAN_CLOAKS = {
    'SimplePointScan': 'Advanced',
}


def scan_backend_type(scanWidgetType):
    """The panel type that runs a ``scanWidgetType``'s scans: its backend
    for a cloak, the type itself otherwise."""
    return SCAN_CLOAKS.get(scanWidgetType, scanWidgetType)


class PlanNotRepresentable(ValueError):
    """A scan the panel cannot represent; the message says what and why."""


class ScanCloak(ABC):
    """Translation between a cloak's plan and its backend's scan dicts.

    The contract (plan §10.7): for every plan ``P`` the cloak makes,
    ``from_backend(*to_backend(P)) == normalize(P)`` -- also when the dicts
    pass through the backend's widget on the way, which is what the switch
    to the backend panel and back does.
    """

    @abstractmethod
    def limits_from_setup(self, setupInfo) -> Any:
        """What the setup lets this cloak do (axes, lasers, speeds)."""

    @abstractmethod
    def to_backend(self, plan, limits) -> Tuple[dict, dict]:
        """``(analogParameterDict, digitalParameterDict)`` for ``plan``."""

    @abstractmethod
    def from_backend(self, analog: Mapping[str, Any], digital: Mapping[str, Any], limits):
        """The plan the backend's dicts hold.

        Raises :class:`PlanNotRepresentable` for a scan the cloak cannot
        show, with a message a user can act on.
        """

    @abstractmethod
    def normalize(self, plan):
        """``plan`` on the grid the backend's widget stores."""

    @abstractmethod
    def plan_to_dict(self, plan) -> dict:
        """``plan`` for a saved state."""

    @abstractmethod
    def plan_from_dict(self, data: Mapping[str, Any]):
        """The plan :meth:`plan_to_dict` saved."""

    def scanned_positioners(self, analog: Mapping[str, Any]) -> list:
        """The backend's ``positionersScan`` for ``analog`` (Advanced's form)."""
        return list(analog.get('scan_dim_target_device', ()))


__all__ = [
    'PlanNotRepresentable',
    'SCAN_CLOAKS',
    'ScanCloak',
    'scan_backend_type',
]


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
