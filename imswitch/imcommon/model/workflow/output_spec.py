"""Which output ports a processor run will produce, and how to ask for it.

Moved here from ``imswitch.improcess.processors.base`` (which re-exports
both names) so that the workflow schema can validate port references
without importing a plugin base class, or Qt.
"""

from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass

from imswitch.imcommon.model.kinds import UNKNOWN_KIND


@dataclass(frozen=True)
class OutputSpec:
    """Which output ports a processor run will produce.

    ``ports`` names them when they are known before the run (``("out",)``,
    ``("signal", "background")``); ``pattern`` is a regular expression for
    ports that depend on the data (a channel split yields ``C0, C1, …`` for
    as many channels as the input has). A workflow validated before running
    accepts a reference to a port that matches either; after the run the
    ports actually produced are checked against the spec.
    """

    ports: tuple[str, ...] | None = ("out",)
    pattern: str | None = None
    #: What every port carries (a ``PORT_KINDS`` value): declared, so an editor
    #: can colour a port and a workflow can be checked before it runs.
    #: ``"unknown"`` when the plugin does not say.
    kind: str = UNKNOWN_KIND

    def matches(self, port: str) -> bool:
        if self.ports is not None and port in self.ports:
            return True
        if self.pattern is not None:
            import re

            return re.fullmatch(self.pattern, str(port)) is not None
        return False

    def describe(self) -> str:
        parts = []
        if self.ports:
            parts.append(", ".join(self.ports))
        if self.pattern:
            parts.append(f"/{self.pattern}/")
        return " or ".join(parts) or "(none)"


@functools.lru_cache(maxsize=None)
def _takes_input_specs(method) -> bool:
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "input_specs" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def call_output_spec(plugin, params: dict | None = None, input_specs=None):
    """``plugin.output_spec(params, input_specs)``, or without ``input_specs``
    for an override that does not take it.

    The documented signature for a drop-in was ``output_spec(params)``; one
    written to it must keep working wherever the framework now passes the
    inputs' kinds -- in ``validate()`` an unexpected argument would raise and
    take every caller with it.
    """
    method = getattr(plugin, "output_spec")
    function = getattr(method, "__func__", method)
    if input_specs is not None and _takes_input_specs(function):
        return method(params, input_specs=input_specs)
    return method(params)


__all__ = ["OutputSpec", "call_output_spec"]


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
