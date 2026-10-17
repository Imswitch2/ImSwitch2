import time

from qtpy import QtCore
from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model import getWidgetStatePersistence
from imswitch.imcontrol.controller.basecontrollers import (
    ComponentStateApplyMode,
    ImConWidgetController,
    SetupModeApplyPriority,
    StatefulComponentMixin,
)


class LeicaStandController(StatefulComponentMixin, ImConWidgetController):
    """Click-driven controller for LeicaStandWidget.

    Setup-mode application intentionally reuses the same high-level stand
    transitions as normal widget interaction.  A saved FLUO/CS state therefore
    restores the same cube, port, diaphragm and shutter behavior instead of
    using a second setup-mode-only hardware sequence.
    """

    # StatefulComponentMixin attributes
    componentName = 'LeicaStand'
    stateSchemaVersion = 1
    legacyStateNames = ()
    setupModeCategory = 'microscope_stand'
    setupModeApplyPriority = SetupModeApplyPriority.MICROSCOPE_STAND
    setupModeHardwareCritical = True

    FLUO_SHUTTER_DELAY_MS = 800

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self, tryInheritParent=True)

        self._manager = None
        self._last_selected_fluo_cube_name = None
        self._current_mode = "FLUO"
        self._current_port_side = "Left"

        stand_manager = getattr(self._master, "standManager", None)

        if stand_manager is None:
            self._widget.setConnected(False)
            return

        if getattr(stand_manager, "mocker", False):
            self._widget.setConnected(False)
            return

        # Use the public StandManager capability facade. The concrete Leica
        # driver remains behind StandManager and can share its hardware layer
        # with the Leica Z positioner without controller coupling.
        self._manager = stand_manager

        self._cube_slot_to_name = self._manager.getAvailableCubes()
        self._cube_name_to_slot = {
            name: slot for slot, name in self._cube_slot_to_name.items()
        }

        self._connect_widget_signals()
        self._init_widget()

        # Register with unified state persistence (only reached when a usable
        # manager is present — mock/disconnected paths return early above).
        getWidgetStatePersistence().register('LeicaStand', self)

    # ── Setup-mode component interface ──────────────────────────────────── #

    def getComponentState(self) -> dict:
        """Snapshot the stand state needed to reproduce normal widget behavior."""
        return {
            "mode": self._current_mode,
            "lastSelectedFluoCubeName": self._last_selected_fluo_cube_name,
            "portSide": self._current_port_side,
        }

    def applyComponentState(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
    ) -> list[str]:
        """Restore stand state from a snapshot.

        STARTUP_RESTORE:
        - Does NOT actuate the stand.
        - Restores harmless remembered cube/port selections.
        - Warns if the saved physical mode differs from the tracked mode.

        SETUP_MODE_APPLY:
        - Restores saved FLUO cube and port selections when present.
        - Uses the same high-level FLUO/CS transition implementation as widget
          interaction, with fail-fast manager calls so hardware errors are
          surfaced as setup-mode warnings.

        Returns:
            List of warning strings (empty if fully successful).
        """
        if not isinstance(state, dict):
            return ["Saved Leica stand state is not a dictionary."]

        warnings = []
        mode = state.get("mode")
        cube_name = state.get("lastSelectedFluoCubeName")
        port_side = state.get("portSide")

        valid_cube = None
        if cube_name is not None:
            cube_name_to_slot = getattr(self, "_cube_name_to_slot", {})
            if cube_name not in cube_name_to_slot:
                warnings.append(
                    f'Leica fluorescence cube "{cube_name}" is not available.'
                )
            else:
                valid_cube = cube_name

        valid_port = None
        if port_side is not None:
            if port_side not in ("Left", "Right"):
                warnings.append(
                    f'Unknown Leica stand port side "{port_side}".'
                )
            else:
                valid_port = port_side

        if mode not in ("FLUO", "CS"):
            warnings.append(
                f'Unknown Leica stand mode "{mode}"; stand not switched.'
            )
            return warnings

        if applyMode == ComponentStateApplyMode.STARTUP_RESTORE:
            if valid_cube is not None:
                self._last_selected_fluo_cube_name = valid_cube
                self._widget.setCurrentCube(valid_cube)

            if valid_port is not None:
                self._current_port_side = valid_port
                self._widget.setCurrentPortSide(valid_port)

            if mode != self._current_mode:
                warnings.append(
                    f'Leica stand not switched at startup: '
                    f'current mode={self._current_mode}, saved mode={mode}.'
                )
            return warnings

        if self._manager is None:
            return warnings + ["Leica stand is not available."]

        if not self._manager.isConnected():
            self._widget.setConnected(False)
            return warnings + ["Leica stand is not connected."]

        # Restore the selected FLUO cube before the mode transition because the
        # FLUO transition itself consumes this selection.
        if valid_cube is not None:
            self._last_selected_fluo_cube_name = valid_cube
            self._widget.setCurrentCube(valid_cube)

        try:
            if mode == "FLUO":
                self._performFluoModeTransition(
                    raiseOnError=True,
                    waitForShutter=True,
                )
            else:  # mode == "CS"
                self._performCSModeTransition(raiseOnError=True)

            # Port side is an independent saved selection.  Apply it after the
            # mode transition because CS mode itself normalizes the port to Left.
            if valid_port is not None:
                self.setPortSideByName(valid_port, raiseOnError=True)
        except Exception as e:
            self.__logger.error(f"Failed to switch Leica stand to {mode}: {e}")
            self._widget.setConnected(self._manager.isConnected())
            warnings.append(f"Failed to switch Leica stand to {mode}: {e}")
            return warnings

        self._widget.setConnected(self._manager.isConnected())
        return warnings

    def describeComponentState(self, state: dict) -> list[str]:
        """Human-readable summary of a saved stand state."""
        state = state or {}
        mode = state.get("mode")
        lines = [f"Stand: {mode}" if mode in ("FLUO", "CS") else "Stand: unknown"]

        cube_name = state.get("lastSelectedFluoCubeName")
        if cube_name:
            lines.append(f"Fluorescence cube: {cube_name}")

        port_side = state.get("portSide")
        if port_side:
            lines.append(f"Port: {port_side}")

        return lines

    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None,
    ) -> list:
        """No configurable hazards for stand actuation (same as FlipMirror)."""
        return []

    @staticmethod
    def _sleepPumpingEvents(seconds: float) -> None:
        """Block briefly while keeping the Qt event loop responsive."""
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

    def toggleMode(self):
        if self._manager is None or not self._manager.isConnected():
            self._widget.setConnected(False)
            return

        if self._current_mode == "FLUO":
            self.setCSMode()
        else:
            self.setFluoMode()

    def _connect_widget_signals(self):
        self._widget.sigModeChanged.connect(self.setMode)
        self._widget.sigCubeChanged.connect(self.setCubeByName)
        self._widget.sigPortSideChanged.connect(self.setPortSideByName)

    def _init_widget(self):
        fluo_cubes = [
            name
            for slot, name in sorted(self._cube_slot_to_name.items())
            if name != "EMP_BF"
        ]

        self._widget.setCubeChoices(fluo_cubes)

        if fluo_cubes:
            self._last_selected_fluo_cube_name = fluo_cubes[0]
            self._widget.setCurrentCube(self._last_selected_fluo_cube_name)

        self._widget.setCurrentPortSide("Left")
        self._current_port_side = "Left"
        self._widget.setMode(self._current_mode)
        self._widget.setConnected(self._manager.isConnected())

    def _safe_call(self, func, *args, raiseOnError=False):
        try:
            result = func(*args)
            self._widget.setConnected(self._manager.isConnected())
            return result
        except Exception:
            self._widget.setConnected(self._manager.isConnected())
            if raiseOnError:
                raise
            return None

    def setMode(self, mode):
        if mode == "FLUO":
            self.setFluoMode()
        elif mode == "CS":
            self.setCSMode()

    def _get_selected_fluo_cube_name(self):
        if self._last_selected_fluo_cube_name in self._cube_name_to_slot:
            return self._last_selected_fluo_cube_name

        for slot, name in sorted(self._cube_slot_to_name.items()):
            if name != "EMP_BF":
                return name

        return None

    def _performFluoModeTransition(
        self,
        *,
        raiseOnError=False,
        waitForShutter=False,
    ):
        """Run the normal widget FLUO transition.

        ``waitForShutter`` is used by setup-mode application so the delayed
        shutter operation completes before the apply is reported successful.
        Interactive use keeps the existing non-blocking QTimer behavior.
        """
        if not self._manager.isConnected():
            self._widget.setConnected(False)
            if raiseOnError:
                raise RuntimeError("Leica stand is not connected")
            return

        self._safe_call(self._manager.setCS, raiseOnError=raiseOnError)

        cube_name = self._get_selected_fluo_cube_name()
        if cube_name:
            slot = self._cube_name_to_slot.get(cube_name)
            if slot:
                self._safe_call(
                    self._manager.setCube,
                    slot,
                    raiseOnError=raiseOnError,
                )

        self._safe_call(
            self._manager.setILFieldDiaphragm,
            12,
            raiseOnError=raiseOnError,
        )
        self._safe_call(self._manager.setCameraPort, raiseOnError=raiseOnError)

        if waitForShutter:
            self._sleepPumpingEvents(self.FLUO_SHUTTER_DELAY_MS / 1000.0)
            self._finishSetFluoMode(raiseOnError=raiseOnError)
        else:
            QtCore.QTimer.singleShot(
                self.FLUO_SHUTTER_DELAY_MS,
                self._finishSetFluoMode,
            )

        self._current_mode = "FLUO"
        self._widget.setMode(self._current_mode)

    def setFluoMode(self):
        return self._performFluoModeTransition()

    def _finishSetFluoMode(self, *, raiseOnError=False):
        if not self._manager.isConnected():
            self._widget.setConnected(False)
            if raiseOnError:
                raise RuntimeError(
                    "Leica stand disconnected before opening illumination shutter"
                )
            return

        self._safe_call(
            self._manager.setILshutter,
            1,
            raiseOnError=raiseOnError,
        )

    def _performCSModeTransition(self, *, raiseOnError=False):
        """Run the normal widget CS transition."""
        if not self._manager.isConnected():
            self._widget.setConnected(False)
            if raiseOnError:
                raise RuntimeError("Leica stand is not connected")
            return

        self._safe_call(self._manager.setCS, raiseOnError=raiseOnError)

        if "EMP_BF" in self._cube_name_to_slot:
            self._safe_call(
                self._manager.setCube,
                self._cube_name_to_slot["EMP_BF"],
                raiseOnError=raiseOnError,
            )

        self._safe_call(self._manager.setCameraPort, raiseOnError=raiseOnError)
        self._safe_call(self._manager.setMagnScan, raiseOnError=raiseOnError)

        self._widget.setCurrentPortSide("Left")
        self._current_port_side = "Left"

        self._current_mode = "CS"
        self._widget.setMode(self._current_mode)

    def setCSMode(self):
        return self._performCSModeTransition()

    def setCubeByName(self, cube_name):
        if cube_name not in self._cube_name_to_slot:
            return

        self._last_selected_fluo_cube_name = cube_name

        if self._current_mode == "FLUO" and self._manager.isConnected():
            slot = self._cube_name_to_slot[cube_name]
            self._safe_call(self._manager.setCube, slot)

    def setPortSideByName(self, value, *, raiseOnError=False):
        if not self._manager.isConnected():
            self._widget.setConnected(False)
            if raiseOnError:
                raise RuntimeError("Leica stand is not connected")
            return

        if value == "Left":
            self._safe_call(
                self._manager.setMagnScan,
                raiseOnError=raiseOnError,
            )
        elif value == "Right":
            self._safe_call(
                self._manager.setMagn1,
                raiseOnError=raiseOnError,
            )
        else:
            return

        self._current_port_side = value
        self._widget.setCurrentPortSide(value)
