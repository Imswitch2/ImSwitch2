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
