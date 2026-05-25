"""
EtSTED controller — thin subclass of :class:`EventTriggeredControllerBase`.

The shared event-triggered machinery (binary mask acquisition, pipeline
loading, transform fitting, slow-scan trigger, runtime state) lives in
the base class.  EtSTED-specific behaviour:

  * It surfaces a status string and a controls-armed visual lock via the
    EtSTED widget; these are no-ops in the base.
  * It validates scan parameters before arming.
  * It forwards ``self._setupInfo.etSTED`` as the second arg to
    :meth:`EtSTEDTransformService.apply` (the base passes ``None``).
"""

from __future__ import annotations

from imswitch.imcontrol.model.EventTriggeredSession import (
    EventRunMode as RunMode,
    EventScanInitiationMode as ScanInitiationMode,
)
from .EventTriggeredBaseController import (
    EventTriggeredControllerBase,
    EventTriggeredCoordTransformHelper,
)


# Re-exported for code that historically reached into this module.
__all__ = ['EtSTEDController', 'RunMode', 'ScanInitiationMode']


class EtSTEDController(EventTriggeredControllerBase):
    """Linked to :class:`EtSTEDWidget`."""

    LOGS_SUBFOLDER = 'logs_etsted'
    MODALITY_LABEL = 'etSTED'

    def _validate_pre_run(self) -> None:
        self._triggeredScanRunner.validate_scan_parameters(
            self._analogParameterDict,
            self._digitalParameterDict,
            self._positionersScan,
        )

    def _transform_apply_extra_arg(self):
        return getattr(self._setupInfo, 'etSTED', None)


# Public aliases used by other modules / tests.
EtSTEDCoordTransformHelper = EventTriggeredCoordTransformHelper


# Copyright (C) 2020-2021 ImSwitch developers
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
