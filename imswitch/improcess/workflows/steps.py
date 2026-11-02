"""The workflow schema, as ImProcess sees it.

The schema itself lives in :mod:`imswitch.imcommon.model.workflow.steps`
(shared with the editor and, later, ImControl's pipeline designer); this
module re-exports it under the path ImProcess always used, and keeps
ImProcess's two host checks in :func:`validate` even when no registry is
given: the headless contract (:func:`~imswitch.improcess.model.plugin_contract.contract_problem`)
and the save formats ImProcess can write
(:func:`~imswitch.improcess.model.save_protocol.normalize_format`).
"""

from __future__ import annotations

from imswitch.imcommon.model.workflow.steps import *  # noqa: F401,F403
from imswitch.imcommon.model.workflow.steps import (  # noqa: F401 - names tests and tools import
    _BUILTIN_KINDS,
    _ID_PATTERN,
    _REF_PATTERN,
    _STEP_REGISTRY,
    _STEP_RUNNERS,
    _STEP_TYPES,
    _block_style_dumper,
    _check_id,
    _defaults,
    _kind_issues,
    _port_ok,
    _unknown_params,
    declared_kinds,
)
from imswitch.imcommon.model.workflow.steps import __all__ as _ALL
from imswitch.imcommon.model.workflow.steps import validate as _validate


def validate(workflow, registry=None, *, contract_problem=None, normalize_format=None):
    """:func:`imswitch.imcommon.model.workflow.steps.validate` with ImProcess's
    headless contract and save formats, whether or not ``registry`` has them."""
    if contract_problem is None:
        from imswitch.improcess.model.plugin_contract import contract_problem
    if normalize_format is None:
        from imswitch.improcess.model.save_protocol import normalize_format
    return _validate(workflow, registry, contract_problem=contract_problem, normalize_format=normalize_format)


__all__ = list(_ALL)


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
