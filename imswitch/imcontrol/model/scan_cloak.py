"""Scan cloaks: simpler frontends over full scan panels.

A cloak shows a microscope's scan in the few terms its users think in and
translates that plan to an existing scan panel, its *backend*.  The backend is
still selected explicitly by ``scan.scanWidgetType``; ``scan.cloak`` only
selects the optional simpler frontend placed over it.

The registry in this module is intentionally Qt-free.  It describes which
backend scan widget types a cloak is compatible with and capabilities of the
cloak itself.  Widget/controller classes still follow the normal ImSwitch
``ScanWidget<Name>`` / ``ScanController<Name>`` naming convention.

Legacy setups that used a cloak name directly as ``scanWidgetType`` are
accepted while configs migrate to the explicit ``scanWidgetType + cloak``
form.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class ScanCloakSpec:
    """Registration metadata for one scan cloak.

    ``compatible_scan_widget_types`` names the real scan backends the cloak
    can drive.  ``has_overview`` is descriptive capability metadata: an
    overview, when present, remains implemented by that cloak rather than by
    the generic cloak framework.
    """

    compatible_scan_widget_types: tuple[str, ...]
    has_overview: bool = False


#: Optional scan frontends.  The relationship belongs here rather than in
#: ``scanWidgetType``: a setup selects a real backend, then optionally one of
#: the cloaks registered as compatible with it.
SCAN_CLOAKS = {
    'SimplePointScan': ScanCloakSpec(
        compatible_scan_widget_types=('Advanced',),
        has_overview=True,
    ),
    'SimpleMoNaLISA': ScanCloakSpec(
        compatible_scan_widget_types=('Advanced',),
        has_overview=False,
    ),
}


@dataclass(frozen=True)
class ResolvedScanFrontend:
    """The backend and frontend selected by a scan setup."""

    backend_type: str
    frontend_type: str
    cloak_type: Optional[str] = None
    cloak_spec: Optional[ScanCloakSpec] = None
    legacy_cloak_config: bool = False


def resolve_scan_frontend(scanWidgetType: str, cloak: Optional[str] = None) -> ResolvedScanFrontend:
    """Resolve a setup's real scan backend and optional cloak frontend.

    New setups use for example ``scanWidgetType='Advanced'`` and
    ``cloak='SimplePointScan'``.  For compatibility, the old
    ``scanWidgetType='SimplePointScan'`` spelling is also resolved to the one
    backend that cloak supports.
    """
    backend_type = str(scanWidgetType)
    cloak_type = cloak or None
    legacy = False

    if cloak_type is None and backend_type in SCAN_CLOAKS:
        # Pre-refactor setup: the cloak occupied scanWidgetType.  This is only
        # unambiguous while that cloak supports exactly one backend.
        cloak_type = backend_type
        spec = SCAN_CLOAKS[cloak_type]
        if len(spec.compatible_scan_widget_types) != 1:
            raise ValueError(
                f'Legacy scanWidgetType {cloak_type!r} is ambiguous; set the real '
                'scanWidgetType and scan.cloak explicitly.'
            )
        backend_type = spec.compatible_scan_widget_types[0]
        legacy = True
    elif cloak_type is not None:
        try:
            spec = SCAN_CLOAKS[cloak_type]
        except KeyError as error:
            raise ValueError(
                f'Unknown scan cloak {cloak_type!r}; choose one of '
                f'{sorted(SCAN_CLOAKS)}.'
            ) from error
        if backend_type not in spec.compatible_scan_widget_types:
            allowed = ', '.join(spec.compatible_scan_widget_types)
            raise ValueError(
                f'Scan cloak {cloak_type!r} cannot cloak scanWidgetType '
                f'{backend_type!r}; compatible scan widget types: {allowed}.'
            )
    else:
        spec = None

    return ResolvedScanFrontend(
        backend_type=backend_type,
        frontend_type=cloak_type or backend_type,
        cloak_type=cloak_type,
        cloak_spec=spec,
        legacy_cloak_config=legacy,
    )


def resolve_scan_info(scanInfo) -> ResolvedScanFrontend:
    """Resolve a ``ScanInfo`` without coupling this module to SetupInfo."""
    return resolve_scan_frontend(
        scanInfo.scanWidgetType,
        getattr(scanInfo, 'cloak', None),
    )


class PlanNotRepresentable(ValueError):
    """A scan the panel cannot represent; the message says what and why."""


class ScanCloak(ABC):
    """Translation between a cloak's plan and its backend's scan dicts.

    For every plan ``P`` the cloak makes,
    ``from_backend(*to_backend(P)) == normalize(P)`` -- also when the dicts
    pass through the backend's widget on the way.
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
    'ResolvedScanFrontend',
    'SCAN_CLOAKS',
    'ScanCloak',
    'ScanCloakSpec',
    'resolve_scan_frontend',
    'resolve_scan_info',
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
