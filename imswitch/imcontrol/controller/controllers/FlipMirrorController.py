from imswitch.imcommon.model import APIExport, initLogger
from imswitch.imcontrol.model import getWidgetStatePersistence

from ..basecontrollers import (
    ComponentStateApplyMode,
    ImConWidgetController,
    SetupModeApplyPriority,
    StatefulComponentMixin,
)


#: Positions of a flip mirror whose manager does not name its states: the classic two-state mirror.
DEFAULT_POSITION_COUNT = 2


def _position_count(device):
    """How many positions (states ``0..N-1``) a flip-mirror device has, from its ``get_state_names()``."""
    getNames = getattr(device, "get_state_names", None)
    if getNames is None:
        return DEFAULT_POSITION_COUNT
    try:
        return len(getNames())
    except Exception:
        return DEFAULT_POSITION_COUNT


def _position_count_mismatch(manager, follower_name, master_name):
    """``(follower count, master count)`` when the two flip mirrors differ in positions, else ``None``."""
    counts = (_position_count(manager[follower_name]), _position_count(manager[master_name]))
    return counts if counts[0] != counts[1] else None


class FlipMirrorController(StatefulComponentMixin, ImConWidgetController):
    """Controller for FlipMirrorWidget.

    A flip mirror is a switch with N named positions (two for a classic mirror, more for a filter
    wheel, turret or light path). A follower copies its master's state, so a link needs the same
    number of positions on both: ``set_link`` refuses a mismatched pair and a follower's master
    choices list only the mirrors with its own number of positions.
    """

    # StatefulComponentMixin attributes
    componentName = 'FlipMirror'
    stateSchemaVersion = 1
    legacyStateNames = ()
    setupModeCategory = 'beam_path'
    setupModeApplyPriority = SetupModeApplyPriority.BEAM_PATH
    setupModeHardwareCritical = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.__logger = initLogger(self, tryInheritParent=True)

        self._manager = getattr(self._master, "flipMirrorsManager", None)
        self._names = []

        # follower -> master
        self._master_by_follower = {}

        # master -> set(followers)
        self._followers_by_master = {}

        if self._manager is None or not self._manager.hasDevices():
            self._widget.setAllEnabled(False)
            return

        self._names = self._manager.getAllDeviceNames()

        for name in self._names:
            manager = self._manager[name]

            if hasattr(manager, "get_state_names"):
                state_names = manager.get_state_names()
            else:
                state_names = {0: "0", 1: "1"}

            self._widget.addFlipMirror(name, state_names=state_names)

        self._widget.sigStateChanged.connect(self.move_flip)
        self._widget.sigLinkChanged.connect(self.set_link)

        # Lifecycle callbacks run on the Hardware Status reconnect worker.
        # Reconnect only restores connectivity and observes the actual mirror
        # state; it never replays link/follower motion.
        self._deviceLifecycleListener = None
        lifecycleService = getattr(self._master, 'deviceLifecycleService', None)
        if lifecycleService is not None:
            self._deviceLifecycleListener = self._deviceLifecycleChanged
            lifecycleService.addListener(self._deviceLifecycleListener)

        self._refresh_all_states()
        self._refresh_link_ui()
        
        # Register with unified state persistence
        getWidgetStatePersistence().register('FlipMirror', self)

    def closeEvent(self):
        lifecycleService = getattr(self._master, 'deviceLifecycleService', None)
        listener = getattr(self, '_deviceLifecycleListener', None)
        if lifecycleService is not None and listener is not None:
            lifecycleService.removeListener(listener)
            self._deviceLifecycleListener = None

    def _deviceLifecycleChanged(self, result):
        affected = tuple(
            device_id
            for device_id in getattr(result, 'affected_device_ids', ())
            if getattr(device_id, 'kind', None) == 'flip_mirror'
        )
        if not affected:
            return
        self._invokeOnControllerThreadIfNeeded(
            lambda: self._refreshLifecycleAffectedFlipMirrors(affected)
        )

    def _rebuild_row_if_positions_changed(self, name):
        """Rebuild the widget row of ``name`` when its manager now names other positions than the row shows.

        A switch whose device was absent at startup is built with the classic two placeholder
        positions; once the device is there it may be a ten-position wheel (or name its two
        positions after the hardware). The rest of the controller reads the manager's count live,
        so only the row needs to follow.
        """
        getNames = getattr(self._manager[name], "get_state_names", None)
        if getNames is None:
            return False
        try:
            names = {int(state): str(text) for state, text in getNames().items()}
        except Exception as exc:
            self.__logger.error(f"Failed to read the positions of flip mirror {name}: {exc}")
            return False
        if not names or names == self._widget.stateNames(name):
            return False
        self._widget.rebuildFlipMirror(name, names)
        return True

    def _refreshLifecycleAffectedFlipMirrors(self, device_ids):
        known = set(self._names)
        for device_id in device_ids:
            if device_id.name not in known:
                continue
            manager = self._manager[device_id.name]
            self._rebuild_row_if_positions_changed(device_id.name)
            cached_state = getattr(manager, 'get_cached_state', lambda: None)()
            if cached_state is not None and self._is_connected(device_id.name):
                self._widget.setState(device_id.name, cached_state)
        self._refresh_link_ui()

    def getComponentState(self) -> dict:
        """Snapshot the current flip mirror states and link configuration."""
        mirrors = {}

        for name in self._names:
            state = None
            connected = self._is_connected(name)
            if connected:
                try:
                    state = int(self._manager[name].get_state())
                except Exception as e:
                    self.__logger.error(f"Failed to snapshot flip mirror {name}: {e}")

            mirrors[name] = {
                "state": state,
                "connected": connected,
            }

        return {
            "mirrors": mirrors,
            "links": dict(self._master_by_follower),
        }

    def applyComponentState(self, state: dict, *, applyMode: ComponentStateApplyMode) -> list[str]:
        """Restore flip mirror state from a snapshot.
        
        STARTUP_RESTORE:
        - Do NOT physically move mirrors
        - Do NOT set links (which would trigger physical moves)
        - Only update widget display/bookkeeping
        - Warn if saved state differs from current physical state
        
        SETUP_MODE_APPLY:
        - Restore links and move mirrors to saved states (full restoration)
        
        Args:
            state: Dict returned by getComponentState()
            applyMode: ComponentStateApplyMode.STARTUP_RESTORE or SETUP_MODE_APPLY
        
        Returns:
            List of warning strings (empty if fully successful)
        """
        warnings = []

        if self._manager is None or not self._manager.hasDevices():
            return ["No flip mirror manager/devices are available."]

        if not isinstance(state, dict):
            return ["Saved flip mirror state is not a dictionary."]

        mirrors = state.get("mirrors", {})
        links = state.get("links", {})

        if not isinstance(mirrors, dict):
            warnings.append("Saved flip mirror mirror-state section is not a dictionary.")
            mirrors = {}
        if not isinstance(links, dict):
            warnings.append("Saved flip mirror link section is not a dictionary.")
            links = {}

        if applyMode == ComponentStateApplyMode.STARTUP_RESTORE:
            # STARTUP_RESTORE: Do NOT move mirrors, do NOT set links
            # Only check if saved state differs and warn
            for name, mirrorState in mirrors.items():
                if name not in self._names:
                    continue
                
                if not self._is_connected(name):
                    continue
                
                if isinstance(mirrorState, dict):
                    savedState = mirrorState.get("state")
                else:
                    savedState = mirrorState
                
                if savedState is None:
                    continue
                
                if not self._is_position_of(name, savedState):
                    warnings.append(self._not_a_position_message(name, savedState, 'not applied'))
                    continue

                # Check if current state differs from saved state
                try:
                    currentState = int(self._manager[name].get_state())
                    if currentState != int(savedState):
                        warnings.append(
                            f'Flip mirror "{name}" not moved at startup: '
                            f'current state={currentState}, saved state={savedState}.'
                        )
                except Exception as e:
                    self.__logger.debug(f"Could not check flip mirror {name} state: {e}")
            
            if links:
                warnings.append("Flip mirror links not restored in startup mode.")
            
        elif applyMode == ComponentStateApplyMode.SETUP_MODE_APPLY:
            # SETUP_MODE_APPLY: Full restoration (current behavior)
            # Remove existing links first so physical states can be restored without
            # follower propagation from the old link graph.
            for follower in list(self._master_by_follower.keys()):
                self.set_link(follower, None)

            for name, mirrorState in mirrors.items():
                if name not in self._names:
                    warnings.append(f'Flip mirror "{name}" is not available.')
                    continue

                if not self._is_connected(name):
                    warnings.append(f'Flip mirror "{name}" is not connected.')
                    continue

                if isinstance(mirrorState, dict):
                    savedState = mirrorState.get("state")
                else:
                    savedState = mirrorState

                if savedState is None:
                    warnings.append(f'Flip mirror "{name}" has no saved state.')
                    continue

                if not self._is_position_of(name, savedState):
                    warnings.append(self._not_a_position_message(name, savedState, 'not moved'))
                    continue

                if not self._safe_move_one(name, int(savedState)):
                    warnings.append(f'Failed to move flip mirror "{name}" to state {savedState}.')

            for follower, master in links.items():
                if follower not in self._names:
                    warnings.append(f'Flip mirror follower "{follower}" is not available.')
                    continue
                if master not in self._names:
                    warnings.append(f'Flip mirror master "{master}" is not available.')
                    continue
                if not self.set_link(follower, master):
                    warnings.append(f'Failed to restore flip mirror link {follower} -> {master}.')

        self._refresh_link_ui()
        return warnings

    def describeComponentState(self, state: dict) -> list[str]:
        """Generate human-readable summary of a saved flip mirror state.
        
        Args:
            state: Dict returned by getComponentState()
        
        Returns:
            List of formatted strings suitable for setup-mode inspector
        """
        state = state or {}
        summaries = []
        
        mirrors = state.get("mirrors", {})
        links = state.get("links", {})
        
        if mirrors:
            summaries.append("  mirrors:")
            for name, mirrorState in sorted(mirrors.items()):
                if isinstance(mirrorState, dict):
                    savedState = mirrorState.get("state")
                    connected = mirrorState.get("connected", False)
                else:
                    savedState = mirrorState
                    connected = True
                
                conn_str = "connected" if connected else "disconnected"
                state_str = str(savedState) if savedState is not None else "N/A"
                summaries.append(f"    {name}: state={state_str}, {conn_str}")
        
        if links:
            summaries.append("  links:")
            for follower, master in sorted(links.items()):
                summaries.append(f"    {follower} → {master}")
        
        return summaries or ["  no flip mirror state"]

    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None
    ) -> list[dict]:
        """Identify potential hazards in a saved flip mirror state.
        
        Flip mirrors are beam-path activation; they do not have configurable
        hazard thresholds. Returns empty list for both modes.
        
        Args:
            state: Dict returned by getComponentState()
            applyMode: The mode in which the state would be applied
            context: Optional consumer-provided context (unused for flip mirrors)
        
        Returns:
            Empty list (no hazards reported for flip mirrors)
        """
        return []

    def _is_position_of(self, name, state):
        """Whether ``state`` is one of the positions ``0..N-1`` of flip mirror ``name``."""
        try:
            state = int(state)
        except (TypeError, ValueError):
            return False
        return 0 <= state < _position_count(self._manager[name])

    def _not_a_position_message(self, name, state, outcome):
        count = _position_count(self._manager[name])
        return (f'Flip mirror "{name}" {outcome}: saved state {state!r} is not one of its '
                f'{count} positions (0 to {count - 1}).')

    def _is_connected(self, name):
        try:
            return bool(self._manager[name].is_connected())
        except Exception:
            return False

    def _get_error(self, name):
        try:
            return self._manager[name].get_last_error()
        except Exception:
            return None

    def _safe_get_state(self, name):
        try:
            state = self._manager[name].get_state()
            self._widget.setState(name, state)
            return state
        except Exception as e:
            self.__logger.error(f"Failed to read flip mirror {name}: {e}")
            return None

    def _safe_move_one(self, name, state):
        try:
            self._manager[name].move_to(state)
            self._widget.setState(name, state)
            return True
        except Exception as e:
            self.__logger.error(f"Failed to move flip mirror {name} to {state}: {e}")
            return False

    def _refresh_all_states(self):
        for name in self._names:
            if self._is_connected(name):
                self._safe_get_state(name)

        self._refresh_link_ui()

    def _refresh_link_ui(self):
        for name in self._names:
            current_master = self._master_by_follower.get(name)

            choices = self._valid_master_choices_for(name)
            self._widget.setMasterChoices(name, choices, current_master=current_master)
            self._widget.setLink(name, current_master)

        for name in self._names:
            connected = self._is_connected(name)
            is_follower = name in self._master_by_follower
            has_followers = len(self._followers_by_master.get(name, set())) > 0

            state_enabled = connected and not is_follower

            # A follower must keep link controls enabled so it can be unlinked.
            # A master with followers cannot itself be linked to something else.
            link_enabled = connected and not has_followers

            if not connected:
                error = self._get_error(name)
                status = "Error" if error else "Disconnected"
            else:
                status = "OK"

            self._widget.setRowState(
                name,
                connected=connected,
                state_enabled=state_enabled,
                link_enabled=link_enabled,
                status_text=status,
            )

    def _valid_master_choices_for(self, follower_name):
        choices = []

        for candidate in self._names:
            if candidate == follower_name:
                continue

            if not self._is_connected(candidate):
                continue

            # A follower copies its master's state, so both need the same positions.
            if _position_count_mismatch(self._manager, follower_name, candidate):
                continue

            # No chains: a follower cannot be used as a master.
            if candidate in self._master_by_follower:
                continue

            # A mirror that already follows something cannot itself become a follower.
            # If follower_name has followers, it should not be linkable.
            if len(self._followers_by_master.get(follower_name, set())) > 0:
                continue

            choices.append(candidate)

        return choices

    def _is_valid_link(self, follower_name, master_name):
        if follower_name not in self._names:
            return False

        if master_name not in self._names:
            return False

        if follower_name == master_name:
            return False

        if _position_count_mismatch(self._manager, follower_name, master_name):
            return False

        if not self._is_connected(follower_name) or not self._is_connected(master_name):
            return False

        # No chains: master cannot already be a follower.
        if master_name in self._master_by_follower:
            return False

        # No chains/cycles: follower cannot already be a master.
        if len(self._followers_by_master.get(follower_name, set())) > 0:
            return False

        return True

    @APIExport(runOnUIThread=True)
    def move_flip(self, name, state):
        """Move one flip mirror and all linked followers to a state (0 or 1 for a two-state mirror, 0..N-1 for a selector)."""
        state = int(state)

        if name in self._master_by_follower:
            self.__logger.warning(f"Ignoring manual move for follower flip mirror {name}")
            self._refresh_link_ui()
            return

        if name not in self._names:
            self.__logger.error(f"Unknown flip mirror: {name}")
            return

        ok = self._safe_move_one(name, state)

        for follower in sorted(self._followers_by_master.get(name, set())):
            follower_ok = self._safe_move_one(follower, state)
            ok = ok and follower_ok

        self._refresh_link_ui()
        return ok

    @APIExport(runOnUIThread=True)
    def set_link(self, follower_name, master_name):
        """Set or remove follower -> master link."""
        if follower_name not in self._names:
            self.__logger.error(f"Unknown follower flip mirror: {follower_name}")
            return False

        # Unlink
        if master_name is None:
            old_master = self._master_by_follower.pop(follower_name, None)
            if old_master is not None:
                self._followers_by_master.get(old_master, set()).discard(follower_name)
                if len(self._followers_by_master.get(old_master, set())) == 0:
                    self._followers_by_master.pop(old_master, None)

            self._refresh_link_ui()
            return True

        # Link
        if master_name in self._names:
            mismatch = _position_count_mismatch(self._manager, follower_name, master_name)
            if mismatch:
                self.__logger.error(
                    f"Cannot link flip mirror {follower_name} ({mismatch[0]} positions) to {master_name} "
                    f"({mismatch[1]} positions): a follower copies its master's state, so both need the "
                    f"same number of positions"
                )
                self._refresh_link_ui()
                return False

        if not self._is_valid_link(follower_name, master_name):
            self.__logger.error(
                f"Invalid flip mirror link: {follower_name} -> {master_name}"
            )
            self._refresh_link_ui()
            return False

        # Remove previous link if any.
        old_master = self._master_by_follower.pop(follower_name, None)
        if old_master is not None:
            self._followers_by_master.get(old_master, set()).discard(follower_name)

        self._master_by_follower[follower_name] = master_name
        self._followers_by_master.setdefault(master_name, set()).add(follower_name)

        # Immediate sync: follower physically takes master's current state.
        master_state = self._safe_get_state(master_name)
        if master_state is not None:
            self._safe_move_one(follower_name, master_state)

        self._refresh_link_ui()
        return True

    @APIExport(runOnUIThread=True)
    def get_flip_state(self, name):
        """Return current state of one flip mirror."""
        if name not in self._names:
            raise ValueError(f"Unknown flip mirror: {name}")

        return self._manager[name].get_state()
