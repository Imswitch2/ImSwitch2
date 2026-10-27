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

    This controller is also a setup-mode component (``LeicaStand``) carrying the
    FLUO/CS stand mode as state. The component-apply path (``applyComponentState``
    with ``SETUP_MODE_APPLY``) intentionally uses the *raw* FLUO/CS sequence —
    ``setFLUO()``/``setCS()``/``setILshutter()`` — to exactly mirror EtMonalisa's
    event-time stand switching (see ``EtMonalisaController._switchStandToFastMode``/
    ``_switchStandToSlowMode``). This is deliberately distinct from the richer UI
    methods ``setFluoMode``/``setCSMode``, which also drive cube/port/diaphragm.
    Snapshot scope is FLUO/CS mode only; the raw path is the only stand sequence
    that is virtual-testable against the in-repo mock and must stay
    behavior-preserving because no hardware validation is available.
    """

    # StatefulComponentMixin attributes
    componentName = 'LeicaStand'
    stateSchemaVersion = 1
    legacyStateNames = ()
    setupModeCategory = 'microscope_stand'
    setupModeApplyPriority = SetupModeApplyPriority.MICROSCOPE_STAND
    setupModeHardwareCritical = True

    FLUO_SHUTTER_DELAY_MS = 800

    # Blocking settle after setFLUO() before opening the IL shutter, replicating
    # EtMonalisa's _sleepPumpingEvents(1.0) so the component-apply path matches
    # the event-time stand sequence byte-for-byte.
    FLUO_SETTLE_SECONDS = 1.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self, tryInheritParent=True)

        self._manager = None
        self._last_selected_fluo_cube_name = None
        self._current_mode = "FLUO"
        self._deviceLifecycleListener = None

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

        lifecycleService = getattr(self._master, "deviceLifecycleService", None)
        if lifecycleService is not None:
            self._deviceLifecycleListener = self._deviceLifecycleChanged
            lifecycleService.addListener(self._deviceLifecycleListener)

        # Register with unified state persistence (only reached when a usable
        # manager is present — mock/disconnected paths return early above).
        getWidgetStatePersistence().register('LeicaStand', self)

    def closeEvent(self):
        lifecycleService = getattr(self._master, "deviceLifecycleService", None)
        listener = getattr(self, "_deviceLifecycleListener", None)
        if lifecycleService is not None and listener is not None:
            lifecycleService.removeListener(listener)
            self._deviceLifecycleListener = None

    def _deviceLifecycleChanged(self, result):
        affected = tuple(getattr(result, "affected_device_ids", ()))
        if not any(
            getattr(device_id, "kind", None) == "stand"
            and getattr(device_id, "name", None) == "Microscope stand"
            for device_id in affected
        ):
            return

        self._invokeOnControllerThreadIfNeeded(self._refreshLifecycleConnectionState)

    def _refreshLifecycleConnectionState(self):
        if self._manager is None:
            self._widget.setConnected(False)
            return
        self._widget.setConnected(self._manager.isConnected())

    # ── Setup-mode component interface ──────────────────────────────────── #

    def getComponentState(self) -> dict:
        """Snapshot the current stand mode (FLUO/CS only)."""
        return {"mode": self._current_mode}

    def applyComponentState(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
    ) -> list[str]:
        """Restore the stand mode from a snapshot.

        STARTUP_RESTORE:
        - Does NOT actuate the stand. If the saved mode differs from the current
          tracked mode, append a warning noting it was not switched at startup.

        SETUP_MODE_APPLY:
        - Switches to ``state['mode']`` using the *raw* FLUO/CS sequence that
          mirrors EtMonalisa's event-time hooks (setFLUO -> blocking settle ->
          setILshutter(1) for FLUO; setCS for CS). Manager calls are wrapped so a
          failure surfaces as a returned warning rather than being swallowed —
          making stand failures visible is the point of this component.

        Returns:
            List of warning strings (empty if fully successful).
        """
        if self._manager is None:
            return ["Leica stand is not available."]

        if not self._manager.isConnected():
            self._widget.setConnected(False)
            return ["Leica stand is not connected."]

        if not isinstance(state, dict):
            return ["Saved Leica stand state is not a dictionary."]

        mode = state.get("mode")

        if mode not in ("FLUO", "CS"):
            return [f'Unknown Leica stand mode "{mode}"; stand not switched.']

        if applyMode == ComponentStateApplyMode.STARTUP_RESTORE:
            warnings = []
            if mode != self._current_mode:
                warnings.append(
                    f'Leica stand not switched at startup: '
                    f'current mode={self._current_mode}, saved mode={mode}.'
                )
            return warnings

        # SETUP_MODE_APPLY: raw FLUO/CS sequence mirroring EtMonalisa's hooks.
        try:
            if mode == "FLUO":
                self._manager.setFLUO()
                self._sleepPumpingEvents(self.FLUO_SETTLE_SECONDS)
                self._manager.setILshutter(1)
            else:  # mode == "CS"
                self._manager.setCS()
        except Exception as e:
            self.__logger.error(f"Failed to switch Leica stand to {mode}: {e}")
            self._widget.setConnected(self._manager.isConnected())
            return [f"Failed to switch Leica stand to {mode}: {e}"]

        self._current_mode = mode
        self._widget.setMode(self._current_mode)
        self._widget.setConnected(self._manager.isConnected())
        return []

    def describeComponentState(self, state: dict) -> list[str]:
        """Human-readable summary of a saved stand mode."""
        state = state or {}
        mode = state.get("mode")
        if mode in ("FLUO", "CS"):
            return [f"Stand: {mode}"]
        return ["Stand: unknown"]

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
        """Block for ``seconds`` while keeping the Qt event loop responsive.

        Mirrors ``EtMonalisaController._sleepPumpingEvents`` so the FLUO settle
        in the component-apply path matches the event-time stand sequence.
        """
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
        self._widget.setMode(self._current_mode)
        self._widget.setConnected(self._manager.isConnected())

    def _safe_call(self, func, *args):
        try:
            result = func(*args)
            self._widget.setConnected(self._manager.isConnected())
            return result
        except Exception:
            self._widget.setConnected(self._manager.isConnected())
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

    def setFluoMode(self):
        if not self._manager.isConnected():
            self._widget.setConnected(False)
            return

        self._safe_call(self._manager.setCS)

        cube_name = self._get_selected_fluo_cube_name()
        if cube_name:
            slot = self._cube_name_to_slot.get(cube_name)
            if slot:
                self._safe_call(self._manager.setCube, slot)

        self._safe_call(self._manager.setILFieldDiaphragm, 12)
        self._safe_call(self._manager.setCameraPort)

        QtCore.QTimer.singleShot(
            self.FLUO_SHUTTER_DELAY_MS, self._finishSetFluoMode
        )

        self._current_mode = "FLUO"
        self._widget.setMode(self._current_mode)

    def _finishSetFluoMode(self):
        if not self._manager.isConnected():
            self._widget.setConnected(False)
            return

        self._safe_call(self._manager.setILshutter, 1)

    def setCSMode(self):
        if not self._manager.isConnected():
            self._widget.setConnected(False)
            return

        self._safe_call(self._manager.setCS)

        if "EMP_BF" in self._cube_name_to_slot:
            self._safe_call(
                self._manager.setCube, self._cube_name_to_slot["EMP_BF"]
            )

        self._safe_call(self._manager.setCameraPort)
        self._safe_call(self._manager.setMagnScan)

        self._widget.setCurrentPortSide("Left")

        self._current_mode = "CS"
        self._widget.setMode(self._current_mode)

    def setCubeByName(self, cube_name):
        if cube_name not in self._cube_name_to_slot:
            return

        self._last_selected_fluo_cube_name = cube_name

        if self._current_mode == "FLUO" and self._manager.isConnected():
            slot = self._cube_name_to_slot[cube_name]
            self._safe_call(self._manager.setCube, slot)

    def setPortSideByName(self, value):
        if not self._manager.isConnected():
            self._widget.setConnected(False)
            return

        if value == "Left":
            self._safe_call(self._manager.setMagnScan)
        elif value == "Right":
            self._safe_call(self._manager.setMagn1)
