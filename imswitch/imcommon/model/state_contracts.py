"""Model-layer state-application contracts.

These declarative types are shared by the model-side persistence registry
(:mod:`imswitch.imcontrol.model.WidgetStatePersistence`) and by the controller
layer. They live in the model package so that model code does not have to import
from the controller package (which previously forced lazy in-function imports to
avoid a circular dependency). The controller package re-exports
:class:`ComponentStateApplyMode` from ``controller.basecontrollers`` for
backwards compatibility, so existing controller imports keep working unchanged.
"""

from enum import Enum


class ComponentStateApplyMode(Enum):
    """Distinguishes passive UI restore from active hardware application."""

    STARTUP_RESTORE = "startup_restore"
    SETUP_MODE_APPLY = "setup_mode_apply"


class RestoreWarning(str):
    """A component-restore warning that also records how serious it is.

    ``applyComponentState`` returns ``list[str]``, and every consumer logs,
    joins or formats those strings. Subclassing ``str`` keeps all of that
    working while letting a UI caller tell apart

    - *informational* warnings ("that detector isn't part of this setup"),
      which are routine whenever a saved state predates a setup-file change,
      from
    - *critical* ones, meaning the hardware did not take a setting, so the
      instrument is not in the state the GUI implies.

    Only the latter deserve to interrupt the operator: a dialog that also
    fires for the routine cases is dismissed on reflex, which is exactly how
    a genuinely wrong trigger mode gets acquired with.

    A plain ``str`` counts as critical, so a controller still on the old
    contract cannot silently downgrade its warnings by staying there.
    """

    def __new__(cls, text: str, *, critical: bool = True) -> 'RestoreWarning':
        warning = super().__new__(cls, text)
        warning.critical = critical
        return warning

    def reworded(self, text: str) -> 'RestoreWarning':
        """A copy carrying ``text`` but this warning's severity.

        Used where a warning is re-prefixed on its way to the UI; plain
        formatting would produce a bare ``str`` and lose the severity.
        """
        return RestoreWarning(text, critical=self.critical)


def isCriticalRestoreWarning(warning) -> bool:
    """Whether ``warning`` means the hardware did not take a setting."""
    return bool(getattr(warning, 'critical', True))


def rewordRestoreWarning(warning, text: str):
    """Return ``text`` carrying ``warning``'s severity where it has one."""
    reword = getattr(warning, 'reworded', None)
    return reword(text) if callable(reword) else text


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
