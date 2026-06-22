"""
EtMonalisa controller — thin subclass of :class:`EventTriggeredControllerBase`.

EtMonalisa diverges from EtSTED in three ways:

  * It physically switches a Leica microscope-stand between fluorescence
    and confocal modes around each event scan.
  * It emits :attr:`sigInitiateEtMonalisa` so other UI consumers can
    track the active state.
  * Otherwise the workflow is identical.  In particular, it now uses
    :class:`EtSTEDTransformService` for the third-order polynomial
    coordinate transform — which fixes the previous broken calibration
    round-trip where ``calibrationFinish`` only wrote a FOV-centre
    ``.txt`` that ``loadTransform`` could not read back.
"""

from __future__ import annotations

import time

from qtpy import QtCore

from imswitch.imcontrol.model.EventTriggeredSession import (
    EventRunMode as RunMode,
    EventScanInitiationMode as ScanInitiationMode,
)
from .EventTriggeredBaseController import (
    EventTriggeredControllerBase,
    EventTriggeredCoordTransformHelper,
)


__all__ = ['EtMonalisaController', 'RunMode', 'ScanInitiationMode']


class EtMonalisaController(EventTriggeredControllerBase):
    """Linked to :class:`EtMonalisaWidget`."""

    LOGS_SUBFOLDER = 'logs_etmonalisa'
    MODALITY_LABEL = 'etMonalisa'

    SMART_MODE_WORKFLOW = 'EtMonalisa'

    # ── Hook overrides ──────────────────────────────────────────────────── #
    #
    # ``LeicaStandController`` is now a ``StatefulComponentMixin`` setup-mode
    # component (``LeicaStand``) that carries the FLUO/CS stand mode as state.
    # The direct stand-manager commands below are the **legacy / flag-off path**:
    # they only run when smart-mode switching is disabled for this workflow.
    #
    # With smart-mode switching **on**, the base already applies the
    # ``scouting``/``event`` roles (which drive setup modes), and FLUO/CS belongs
    # in those scouting/event setup modes — so switching the stand directly here
    # too would double-actuate. The authored scouting/event modes for EtMonalisa
    # must therefore include ``LeicaStand`` (just as Snouty's must include
    # ``FlipMirror``).
    #
    # The ``sigInitiateEtMonalisa`` emissions are UI state, not hardware, and stay
    # unconditional in both paths.

    def _pre_arm_hook(self) -> None:
        self._commChannel.sigInitiateEtMonalisa.emit(True)
        if (
            self._state.runMode == RunMode.Experiment
            and not self._smartModeSwitchingEnabled()
        ):
            self._switchStandToFastMode()

    def _post_stop_hook(self, *, reset_params: bool) -> None:
        self._commChannel.sigInitiateEtMonalisa.emit(False)

    def _on_pause_modality_hook(self) -> None:
        if (
            self._state.runMode == RunMode.Experiment
            and not self._smartModeSwitchingEnabled()
        ):
            self._switchStandToSlowMode()

    def _on_resume_modality_hook(self) -> None:
        if (
            self._state.runMode == RunMode.Experiment
            and not self._smartModeSwitchingEnabled()
        ):
            self._switchStandToFastMode()
            self._sleepPumpingEvents(0.1)  # let LEDs settle before next event

    # ── Stand-mode helpers ──────────────────────────────────────────────── #

    def _switchStandToFastMode(self) -> None:
        self._master.standManager.setFLUO()
        self._sleepPumpingEvents(1.0)
        self._master.standManager.setILshutter(1)

    def _switchStandToSlowMode(self) -> None:
        self._master.standManager.setCS()
        self._sleepPumpingEvents(1.0)

    @staticmethod
    def _sleepPumpingEvents(seconds: float) -> None:
        """Block for ``seconds`` while keeping the Qt event loop responsive."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            QtCore.QCoreApplication.processEvents(
                QtCore.QEventLoop.AllEvents,
                int(min(remaining, 0.05) * 1000),
            )
            time.sleep(min(remaining, 0.01))


# Public alias retained for code that imports the helper by its old name.
EtMonalisaCoordTransformHelper = EventTriggeredCoordTransformHelper


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
