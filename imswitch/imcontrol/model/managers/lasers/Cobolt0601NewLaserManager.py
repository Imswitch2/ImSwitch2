"""LaserManager for Cobolt 06-01 / 06-MLD lasers.

Vendor commands live in ``cobolt0601_protocols``: this manager selects a
protocol profile at startup and then drives the laser through named
operations, never through command strings. Older 06-01 / 06-MLD units use the
short Cobolt command set (``l0``/``l1``/``em``/``sdmes``/``slmp``). Newer
firmware uses the SCPI-style profile exposed by Cobolt's official
``pycobolt`` package (``LASer:RUNMode`` /
``LASer:CP:POWer:SETPoint`` / ``LASer:PowerModulation:POWer:SETPoint``).

What stays here is what only the manager can own: configuration, the safe
initialization sequence, enable/disable/scan sequencing, cached ImSwitch
state, the master-versus-pause emission policy, and simulation.

Safety design (matches the WFS reference at
``/Users/lenny/PycharmProjects/WidefieldStarss/src/WFS/cobolt_laser.py``):

- **Off state = digital modulation mode + ``l0``.** With ``l0`` the master
  switch is off; even if the mode were wrong, the beam cannot emit. The
  laser stays warm so re-enabling is instant. This is the only setting in
  which power changes are *guaranteed* not to leak through to the beam.
- **On state = constant-power mode + ``l1`` at the cached setpoint.**
- **Power changes never apply when the laser is "off".** They update the
  cached setpoint and are flushed on the next enable.
- **Autostart disabled (``@cobas 0``)** so the laser does not power on by
  itself when the controller boots.
- ``emissionControl="pause"`` is the exception for SCPI/OEM firmware that
  should not receive ``l0``. It keeps the controller in modulation-gated and
  paused state, then resumes only after the requested power has been written.
"""

from imswitch.imcommon.model import initLogger
from .LaserManager import LaserManager, normalise_ports
from .cobolt0601_protocols import (
    AUTO_SELECTION_ORDER,
    build_profiles,
    read_identity,
)
from ._protocol import (
    CommandRejected,
    DeviceInitializationError,
    ProtocolError,
)
import traceback


LEGACY_PROFILE_ID = 'cobolt.legacy'
SCPI_PROFILE_ID = 'cobolt.scpi-compatible'


def _as_bool(value) -> bool:
    """Interpret a managerProperties flag as a bool.

    JSON gives a real bool, but hand-edited setups and the config editor can
    produce strings. Anything unrecognised is False, which is the safe default
    for ``simulation``: an unparseable value must not silently disable real
    hardware.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


class Cobolt0601NewLaserManager(LaserManager):
    """LaserManager for Cobolt 06-01 lasers.

    Manager properties:

    - ``digitalPorts`` -- a string array containing the COM ports to connect
      to, e.g. ``["COM4"]``.
    - ``modulationPowerMw`` -- power (mW) preloaded into the modulation
      setpoint at startup, so that if external TTL is wired the laser
      will emit at this level when toggled on with TTL HIGH. Default 5 mW.
    - ``emissionControl`` -- ``"master"`` (default, ``l0``/``l1``),
      ``"pause"`` (``las:paus 1``/``0``), or ``"auto"`` (diagnostic only;
      logs the detected family but resolves to master).
    - ``protocolProfile`` -- command dialect to use: ``"cobolt.legacy"``,
      ``"cobolt.scpi-compatible"``, or ``"auto"`` to discover it from
      read-only probes. Omitting it means ``auto``, which reproduces the
      manager's long-standing detection behavior for existing setup files.
    - ``scpiPowerUnit`` -- unit expected by SCPI power setpoint commands:
      ``"mW"`` (default, matches upstream ``pycobolt``) or ``"W"`` for
      firmware/configurations that expose SCPI setpoints in watts. Retained
      for compatibility; it is expected to become a profile distinction once
      the hardware inventory verifies which controllers need watts.
    - ``simulation`` -- ``true`` to use the mock driver instead of opening the
      port. Defaults to ``false``.
    - ``useMockOnFailure`` -- when a real connection cannot be opened, use the
      in-process mock instead of aborting startup. Defaults to ``true`` for
      compatibility with existing hardware-less setups. Set to ``false`` when
      a missing laser must be a hard startup error.
    """

    def __init__(self, laserInfo, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)

        # Lazy import of hardware library
        try:
            from .PyCoboltManager import Cobolt06
            self._Cobolt06 = Cobolt06
        except ImportError as e:
            self.__logger.error(f'Failed to import PyCoboltManager library: {e}')
            raise

        self._port = normalise_ports(laserInfo.managerProperties['digitalPorts'])[0]
        self._modulation_power_mw = float(
            laserInfo.managerProperties.get('modulationPowerMw', 5.0)
        )
        # ``simulation`` explicitly skips the real transport.  A separate
        # fallback flag preserves the long-standing behaviour for unavailable
        # ports without making every normal startup a simulation.
        self._simulation = _as_bool(
            laserInfo.managerProperties.get('simulation', False)
        )
        self._use_mock_on_failure = _as_bool(
            laserInfo.managerProperties.get('useMockOnFailure', True)
        )
        self._protocol_profile = laserInfo.managerProperties.get('protocolProfile')
        if self._protocol_profile is not None:
            self._protocol_profile = str(self._protocol_profile).strip()
        self._profiles = {}
        self._profile = None

        self._scpi_power_unit = str(
            laserInfo.managerProperties.get('scpiPowerUnit', 'mW')
        ).strip().lower()
        if self._scpi_power_unit not in ('mw', 'w'):
            self.__logger.warning(
                f'Unknown Cobolt scpiPowerUnit={self._scpi_power_unit!r}; '
                f'defaulting to mW.'
            )
            self._scpi_power_unit = 'mw'

        # Emission-control strategy (opt-in per laser):
        #   'master' (default) -> l0/l1 master switch. Unchanged behavior for
        #     every existing setup.
        #   'pause' -> toggle the beam with las:paus 1 / las:paus 0
        #     (pause/resume) and NEVER send l0. Needed for OEM-locked firmware
        #     (e.g. 1.2.1.0) where l0 aborts the laser into a state only a
        #     physical interlock edge can clear.
        #   'auto' -> diagnostic mode for mixed Cobolt fleets. It never sends
        #     pause probes or silently switches into pause mode; explicit
        #     'pause' is required for OEM/interlock-locked units.
        self._emission_control = str(
            laserInfo.managerProperties.get('emissionControl', 'master')
        ).strip().lower()
        if self._emission_control not in ('master', 'pause', 'auto'):
            self.__logger.warning(
                f'Unknown Cobolt emissionControl={self._emission_control!r}; '
                f'defaulting to master.'
            )
            self._emission_control = 'master'
        self._pause_mode = self._emission_control == 'pause'

        # Cached state — power setpoint chosen by GUI/script; only flushed
        # to the laser when _enabled is True.
        self._setpoint_mw = 0
        self._enabled = False
        self._real_hw = False
        self._mock_fallback = False
        self._profiles = build_profiles(self._scpi_power_unit)
        self._firmware_version = None
        self._serial_number = None
        self._model_number = None
        # Failure type of the most recent profile call, so callers can tell an
        # explicit rejection (safe to try an alternative) from an unknown
        # outcome (never retry). None when the last operation succeeded.
        self._last_failure = None

        if self._simulation:
            self.__logger.info(
                f'Cobolt laser {name} starting in simulation mode; '
                f'port {self._port} will not be opened.'
            )
            self._start_mock()
        else:
            self.__logger.debug(f'Initializing Cobolt laser {name} on {self._port}')
            try:
                self._laser = self._Cobolt06(port=self._port)
            except Exception as exc:
                self.__logger.debug(
                    f'Cobolt {name} open failure traceback:\n'
                    f'{traceback.format_exc()}'
                )
                if not self._use_mock_on_failure:
                    raise DeviceInitializationError(
                        f'Cobolt laser {name!r} could not be opened on '
                        f'{self._port}: {exc}. Check the configured port and '
                        f'device power. To run without hardware, set '
                        f'managerProperties.simulation to true, or set '
                        f'useMockOnFailure to true.'
                    ) from exc
                self.__logger.warning(
                    f'Cobolt laser {name!r} could not be opened on '
                    f'{self._port}: {exc}. Falling back to MockCobolt06. '
                    f'Set managerProperties.useMockOnFailure to false to '
                    f'make this a hard startup error.'
                )
                self._start_mock()
                self._mock_fallback = True
            else:
                self._real_hw = True

        # Profile validation and the safe state are part of initialization: if
        # either fails we must not hand back a manager that looks usable. The
        # port is closed again so a failed startup does not leak it.
        try:
            self._detect_firmware()
            self._init_safe_state()
        except Exception:
            self._close_connection()
            raise

        super().__init__(laserInfo, name, isBinary=False,
                         valueUnits='mW', valueDecimals=0)

    def _start_mock(self) -> None:
        """Create the local Cobolt mock without attempting a serial port."""
        from imswitch.imcontrol.model.lantzdrivers_mock.cobolt.cobolt0601 import (
            MockCobolt06,
        )

        self._laser = MockCobolt06(self._port)
        self._laser.initialize()
        self._real_hw = False

    # ------------------------------------------------------------------
    # Profile bridge — the manager never builds a vendor command string.
    # ------------------------------------------------------------------

    def _run(self, operation_name: str, *args, log_failure: bool = True) -> bool:
        """Invoke a profile operation and report success as a bool.

        The manager's sequencing logic is built on "did this step work?", and
        each call site decides for itself whether a failure is fatal or
        best-effort — that safety policy stays here rather than moving into
        the profiles. The failure *type* is recorded in ``self._last_failure``
        so callers can tell an explicit rejection from an unknown outcome.
        """
        profile = self._profile
        if profile is None:
            self._last_failure = None
            self.__logger.error(
                f'Cobolt {self._port}: no protocol profile selected; '
                f'cannot run {operation_name}.'
            )
            return False

        operation = getattr(profile, operation_name, None)
        if operation is None:
            self._last_failure = None
            self.__logger.error(
                f'Cobolt {self._port}: profile {profile.profile_id} does not '
                f'implement {operation_name}.'
            )
            return False

        try:
            operation(self._laser, *args)
        except CommandRejected as exc:
            self._last_failure = exc
            if log_failure:
                self.__logger.warning(str(exc))
            return False
        except ProtocolError as exc:
            self._last_failure = exc
            if log_failure:
                self.__logger.error(
                    f'Cobolt {self._port}: {operation_name} failed with an '
                    f'unknown outcome ({type(exc).__name__}): {exc.message}'
                )
            return False
        self._last_failure = None
        return True

    def _read_identity(self) -> None:
        """Read and log the Cobolt identity fields the controller exposes."""
        identity = read_identity(self._laser)
        self._firmware_version = identity['firmware']
        self._serial_number = identity['serial']
        self._model_number = identity['model']

        self.__logger.info(
            f'Cobolt {self._port} identity: '
            f'firmware={self._firmware_version or "unknown"}, '
            f'serial={self._serial_number or "unknown"}, '
            f'model={self._model_number or "unknown"}'
        )

    # ------------------------------------------------------------------
    # Transitional selection shims
    #
    # ``_scpi`` and ``_scpi_power_unit`` predate protocol profiles. They are
    # kept as views onto the selected profile so existing callers and the
    # command-sequence tests keep working unchanged through the extraction.
    # Phase 3 replaces them with the profile identifier itself.
    # ------------------------------------------------------------------

    @property
    def _scpi(self):
        profile = getattr(self, '_profile', None)
        if profile is None:
            return None
        return profile.profile_id == SCPI_PROFILE_ID

    @_scpi.setter
    def _scpi(self, value):
        if value is None:
            self._profile = None
            return
        if not getattr(self, '_profiles', None):
            self._profiles = build_profiles(
                getattr(self, '_scpi_power_unit', 'mw')
            )
        self._profile = self._profiles[
            SCPI_PROFILE_ID if value else LEGACY_PROFILE_ID
        ]

    @property
    def _scpi_power_unit(self):
        return getattr(self, '_scpi_power_unit_value', 'mw')

    @_scpi_power_unit.setter
    def _scpi_power_unit(self, value):
        self._scpi_power_unit_value = value
        for profile in (getattr(self, '_profiles', None) or {}).values():
            if hasattr(profile, 'power_unit'):
                profile.power_unit = value

    # ------------------------------------------------------------------
    # Firmware autodetection
    # ------------------------------------------------------------------

    def _detect_firmware(self) -> None:
        """Probe the laser to decide which command set to use.

        The probe deliberately records the controller identity first:
        ``gfv?`` (firmware), ``sn?``/``gsn?`` (serial), and ``glm?``
        (model). These are the same identification hooks used by Cobolt's
        official ``pycobolt`` package and give us a useful fingerprint when
        a lab unit behaves differently from the others.

        Firmware-family probing is positive but not over-specific:

          - SCPI probes: ``LASer:RUNMode?`` plus at least one SCPI setpoint
            query. Current upstream ``pycobolt`` uses
            ``LASer:CP:POWer:SETPoint?``; some firmware also accepts older
            ``LASer:POWer:SETPoint?``.
          - Legacy probes: ``l?`` or ``gam?``.
          - Both probe groups fail → raise
            :class:`DeviceInitializationError`. The controller is
            unresponsive, powered off, or not a Cobolt, and guessing a
            command set for an unidentified laser is exactly the kind of
            silent assumption this manager must not make.

        SCPI takes priority when both succeed — it's the modern command
        set and avoids ambiguity on units. Modern controllers often still
        answer short-form queries such as ``l?``, so overlap is expected and
        is not itself an error. Decision is one-shot for the lifetime of the
        manager.

        An explicit ``protocolProfile`` skips discovery: the requested profile
        is loaded and validated read-only, and a validation that contradicts
        the request is refused rather than silently resolved to a different
        profile.
        """
        self._read_identity()

        requested = self._protocol_profile
        if requested and requested.lower() != 'auto':
            self._select_requested_profile(requested)
        else:
            self._discover_profile()

        self._validate_profile_capabilities()

        if self._emission_control == 'auto':
            self._pause_mode = False
            self.__logger.warning(
                f'Cobolt {self._port}: emissionControl=auto is diagnostic only '
                f'and resolves to master control. Detected '
                f'{"SCPI" if self._scpi else "legacy"} command family; set '
                f'emissionControl="pause" explicitly only for OEM/interlock '
                f'firmware that must not receive l0.'
            )

    def _probe(self, profile):
        """Run a profile's read-only probe, aborting if it learns nothing.

        A probe reports a clean negative when the controller *rejects* its
        queries — that genuinely means "not this dialect". Any other failure
        means the probe is uninformative, and continuing would let a healthy
        controller with a flaky link be driven with the wrong command set: an
        SCPI unit whose SCPI queries time out still answers ``l?``, and would
        otherwise be selected as legacy.
        """
        try:
            return profile.probe(self._laser)
        except ProtocolError as exc:
            raise DeviceInitializationError(
                f'Cobolt {self._port}: probing profile {profile.profile_id} '
                f'did not complete ({type(exc).__name__}: {exc.message}). '
                f'Profile discovery is indeterminate, so no command dialect '
                f'can be trusted. Check the port, baud rate, cabling and '
                f'device power rather than assuming a different dialect.'
            ) from exc

    def _select_requested_profile(self, requested: str) -> None:
        """Load an explicitly configured profile and validate it read-only."""
        profile = self._profiles.get(requested)
        if profile is None:
            raise DeviceInitializationError(
                f'Cobolt {self._port}: unknown protocolProfile '
                f'{requested!r}. Available profiles: '
                f'{", ".join(sorted(self._profiles))}.'
            )

        result = self._probe(profile)
        if not result.matched:
            raise DeviceInitializationError(
                f'Cobolt {self._port}: the controller does not behave like '
                f'the requested protocolProfile {requested!r} '
                f'(evidence: {result.evidence}). Refusing to silently switch '
                f'to a different profile.'
            )

        self._profile = profile
        self.__logger.debug(
            f'Cobolt {self._port}: using explicitly configured profile '
            f'{profile.profile_id}.'
        )

    def _discover_profile(self) -> None:
        """Select a profile from bounded, read-only probes.

        Candidates are probed in the documented precedence order, and every
        candidate is probed even after a match so the fingerprint records the
        full picture. Overlap between SCPI and legacy is expected, not an
        error; a device that matches nothing is refused rather than guessed.
        """
        results = {}
        for profile_id in AUTO_SELECTION_ORDER:
            profile = self._profiles.get(profile_id)
            if profile is None or not profile.auto_selectable:
                continue
            results[profile_id] = self._probe(profile)

        for profile_id in AUTO_SELECTION_ORDER:
            result = results.get(profile_id)
            if result is not None and result.matched:
                self._profile = self._profiles[profile_id]
                self.__logger.debug(
                    f'Cobolt {self._port}: detected profile {profile_id} '
                    f'(evidence: {result.evidence}, '
                    f'scpiPowerUnit={self._scpi_power_unit}).'
                )
                return

        raise DeviceInitializationError(
            f'Cobolt {self._port}: neither firmware family responds. '
            f'SCPI probes (LASer:RUNMode?, SCPI power setpoint queries) '
            f'and legacy probes (l?, gam?) all failed. The controller may '
            f'be powered off, unplugged, on the wrong port, or in an '
            f'error state. Refusing to guess a command set for an '
            f'unidentified device. To run without hardware, set '
            f'managerProperties.simulation to true.'
        )

    def _validate_profile_capabilities(self) -> None:
        """Fail at startup when the emission policy needs a missing operation.

        Configuration and profile can each be valid yet mutually incompatible:
        ``emissionControl="pause"`` needs pause/resume, which legacy firmware
        does not have. Catching it here reports a configuration error instead
        of surfacing later as a failed emission transition at the first enable.
        """
        if not self._pause_mode:
            return
        profile = self._profile
        unsupported = getattr(profile, 'unsupported_operations', frozenset())
        missing = [op for op in ('pause', 'resume') if op in unsupported]
        if missing:
            raise DeviceInitializationError(
                f'Cobolt {self._port}: emissionControl="pause" requires '
                f'{" and ".join(missing)}, which profile '
                f'{profile.profile_id} does not support. Use '
                f'emissionControl="master", or configure a profile that '
                f'supports pausing emission.'
            )

    # --- Per-action helpers, delegating to the selected profile ---
    # Each returns True only if every required operation succeeded.

    def _enter_constant_power(self) -> bool:
        return self._run('enter_constant_power')

    def _enter_modulation_mode(self, mod_power_mw: float) -> bool:
        """Enter digital-modulation mode at the given modulation power.

        All three steps are attempted even if an earlier one fails, so the
        laser is driven as far towards the gated state as the controller
        allows. The caller decides what a partial failure means.
        """
        ok1 = self._run('set_modulation_power_mw', mod_power_mw)
        ok2 = self._run('enter_modulation_mode')
        ok3 = self._run('set_digital_modulation_enabled', True)
        return ok1 and ok2 and ok3

    def _set_cw_power_mw(self, value_mw: float) -> bool:
        """Set the constant-power setpoint."""
        return self._run('set_constant_power_mw', value_mw)

    # ------------------------------------------------------------------
    # Safe-state setup + ImSwitch API
    # ------------------------------------------------------------------

    def _init_safe_state(self) -> None:
        """Put the laser into the safe state at startup.

        - autostart off (``@cobas 0``) — universal
        - modulation mode armed at the configured idle power — firmware-branched
        - master ``l0`` so even without TTL the beam is dark — universal

        ``l0`` is the safety-critical command; if it fails initialization
        raises, because a laser whose beam could not be confirmed dark must
        not be handed to the GUI as a working device. The mode-entry is
        best-effort because the master switch already guarantees the beam is
        dark.
        """
        if self._pause_mode:
            # SAFETY: do NOT send '@cob1' here. On this OEM-locked firmware
            # '@cob1' does not start the laser immediately (the controller is in
            # standby until a physical interlock edge) — instead it PRIMES a
            # pending turn-on that fires the moment the operator cycles the
            # interlock at startup. The laser would then emit by itself even
            # though it is "off" in the GUI. We therefore leave the laser in the
            # modulation safe state only: PowerModulation + digital gate on, so
            # with the scanner's TTL idle-low the beam is held dark, plus
            # 'las:paus 1'. The once-per-start interlock cycle then only CLEARS
            # the standby state; light is produced solely when the user turns the
            # laser on in the GUI (setEnabled -> las:paus 0 + constant power).
            #
            # TODO(640 / OEM-locked Cobolt fw 1.2.1.0): the once-per-start
            # interlock cycle itself is still required because the controller
            # parks in a standby/aborted state that only a physical interlock
            # edge clears, and '@cobas' (autostart config) is permission-denied
            # over serial. A real fix needs vendor reconfiguration of the
            # controller. Revisit if Cobolt/HUBNER provide an unlock/restart.
            self._enter_modulation_mode(self._modulation_power_mw)
            ok_pause = self._run('pause')
            if not ok_pause:
                raise DeviceInitializationError(
                    f'Cobolt {self._port}: emission-pause (las:paus 1) failed '
                    f'during safe-state init, so the beam could not be '
                    f'confirmed dark. Verify the controller is responsive '
                    f'before opening the shutter.'
                )
            self._enabled = False
            self.__logger.debug(
                f'Cobolt {self._port} initialised in safe state '
                f'(SCPI pause mode: modulation-gated + paused, autostart NOT '
                f'armed — no emission until turned on in the GUI).'
            )
        else:
            self._run('disable_autostart')
            self._enter_modulation_mode(self._modulation_power_mw)

            ok_off = self._run('master_off')
            if not ok_off:
                raise DeviceInitializationError(
                    f'Cobolt {self._port}: master-off (l0) failed during '
                    f'safe-state init, so the beam could not be confirmed '
                    f'dark. Verify the controller is responsive before '
                    f'opening the shutter.'
                )
            self._enabled = False
            self.__logger.debug(
                f'Cobolt {self._port} initialised in safe state '
                f'({"SCPI" if self._scpi else "legacy"} commands, master off).'
            )

    def setEnabled(self, enabled: bool) -> None:
        """ImSwitch on/off toggle. Fail-closed: ``_enabled`` is only
        updated to True if every command in the on-sequence succeeded.

        - ``False`` → modulation mode + ``l0`` (beam cannot emit).
        - ``True``  → constant-power mode at the cached setpoint + ``l1``.

        In pause emission-control mode the on/off is delegated to
        :meth:`_set_enabled_pause`, which pauses/resumes the beam without
        sending ``l0`` (see ``__init__``).
        """
        if self._pause_mode:
            self._set_enabled_pause(enabled)
            return
        if enabled:
            # Always flush the setpoint, including 0 — skipping the write
            # would turn the laser on at whatever power the hardware last
            # had, not what the GUI shows.
            #
            # Each step is a prerequisite for the next: the master switch is
            # only allowed to close once the power setpoint and the mode are
            # known good. Continuing past a failure would briefly enable the
            # laser at whatever power the hardware still held.
            if not self._set_cw_power_mw(self._setpoint_mw):
                self._abort_enable('power setpoint')
                return
            if not self._enter_constant_power():
                self._abort_enable('constant-power mode')
                return
            if not self._run('master_on'):
                self._abort_enable('master on')
                return

            self._enabled = True
        else:
            # Master off is the critical step — must succeed. If it does
            # not, _enabled stays True so callers know the off-transition
            # did not actually happen.
            ok_off = self._run('master_off')
            if not ok_off:
                self.__logger.error(
                    f'CRITICAL: Cobolt {self._port} master-off l0 failed. '
                    f'_enabled remains True; verify shutter state manually.'
                )
                return
            # Optional: return to modulation-mode safe state. Failure here
            # is non-critical because l0 already darked the beam.
            self._enter_modulation_mode(self._modulation_power_mw)
            self._enabled = False

    def _set_enabled_pause(self, enabled: bool) -> None:
        """On/off for ``emissionControl='pause'``.

        This path only resumes (``las:paus 0``) or pauses (``las:paus 1``)
        the beam and never sends ``l0``, so the OEM interlock-re-arm path is
        not triggered. Fail-closed like the master path: ``_enabled`` only
        goes True if the whole on-sequence succeeded; an off that fails to
        pause leaves ``_enabled`` True so the caller knows the beam may still
        be live.
        """
        if enabled:
            # Always flush the setpoint, including 0 (see setEnabled). Resume
            # is only reached once power and mode are known good — the same
            # prerequisite rule as the master path.
            if not self._set_cw_power_mw(self._setpoint_mw):
                self._abort_enable('power setpoint')
                return
            if not self._enter_constant_power():
                self._abort_enable('constant-power mode')
                return
            if not self._run('resume'):
                self._abort_enable('resume emission')
                return
            self._enabled = True
        else:
            ok_pause = self._run('pause')
            if not ok_pause:
                self.__logger.error(
                    f'CRITICAL: Cobolt {self._port} emission-pause '
                    f'(las:paus 1) failed; _enabled remains True. Verify '
                    f'shutter state manually.'
                )
                return
            self._enabled = False

    def _abort_enable(self, failed_step: str) -> None:
        """Drive back to the safe state after a failed enable prerequisite.

        Leaves ``_enabled`` False so the GUI and scripting layers can see the
        transition did not take effect.
        """
        self.__logger.error(
            f'Cobolt {self._port} failed to enable at step {failed_step!r}; '
            f'reverting to the safe-off state without enabling emission.'
        )
        if self._pause_mode:
            self._run('pause')
        else:
            self._run('master_off')
            self._enter_modulation_mode(self._modulation_power_mw)
        self._enabled = False

    def setValue(self, power) -> None:
        """Update the constant-power setpoint.

        Only flushed to the laser when ``_enabled`` is True. When the
        laser is off, the new value is cached and will take effect on
        the next ``setEnabled(True)``.

        While the laser is live the cache is only committed after the
        controller accepts the write, so the manager never reports a setpoint
        the hardware never took. If the outcome is unknown the manager cannot
        say what power is actually emitting, so it drives the defined safe-off
        recovery rather than leaving an unverified beam live.
        """
        try:
            value = int(power)
        except (TypeError, ValueError):
            self.__logger.warning(f'Ignoring non-numeric setValue({power!r})')
            return

        if not self._enabled:
            self._setpoint_mw = value
            return

        if self._set_cw_power_mw(value):
            self._setpoint_mw = value
            return

        if isinstance(self._last_failure, CommandRejected):
            # The controller refused the value and did not act on it, so the
            # beam is still at the previous, known setpoint.
            self.__logger.error(
                f'Cobolt {self._port} rejected setpoint {value} mW; keeping '
                f'the previous setpoint of {self._setpoint_mw} mW.'
            )
            return

        self.__logger.error(
            f'Cobolt {self._port} setpoint write of {value} mW failed with an '
            f'unknown outcome; the emitted power cannot be confirmed. '
            f'Driving the laser to the safe-off state.'
        )
        self.setEnabled(False)

    def setScanModeActive(self, active: bool) -> None:
        """Switch between continuous (off) and scan/digital-modulation (on).

        In scan mode the laser is in digital-modulation mode at the cached
        power setpoint and emission is gated by external TTL. Outside scan
        mode the laser returns to whatever ``_enabled`` says.
        """
        if active:
            # The GUI setpoint is the sole authority for the scan power —
            # including 0, which must arm the laser DARK. Falling back to
            # _modulation_power_mw here would emit light the user explicitly
            # set to zero; that default is only for the idle safe state.
            #
            # Arming the master switch depends on the modulation power AND the
            # digital gate being set: if either failed, the TTL line may not
            # gate the beam at all, so enabling emission could produce
            # continuous light at an unknown power for the whole scan.
            if not self._enter_modulation_mode(self._setpoint_mw):
                self.__logger.error(
                    f'Cobolt {self._port} could not arm digital modulation '
                    f'for the scan; forcing the safe-off state instead of '
                    f'enabling emission.'
                )
                if self._pause_mode:
                    self._run('pause')
                else:
                    self._run('master_off')
                return
            if self._setpoint_mw <= 0:
                # A zero GUI setpoint is an explicit off command. Keep the
                # master/resume state off even though the scan includes this
                # laser, so TTL pulses cannot produce emission.
                if self._pause_mode:
                    self._run('pause')
                else:
                    self._run('master_off')
                return
            if self._pause_mode:
                # Laser is already started; just un-pause. The digital gate
                # (las:pm:dig:ena 1, set by _enter_modulation_mode) keeps the
                # beam dark until the scanner drives the TTL line HIGH.
                self._run('resume')
            else:
                self._run('master_on')   # master on; TTL gates the actual emission
        else:
            # Leave scan mode — back to the saved enable state.
            self.setEnabled(self._enabled)

    def setModulationEnabled(self, enabled: bool) -> None:
        self._run('set_digital_modulation_enabled', enabled)

    def setModulationPower(self, power) -> None:
        try:
            value = float(power)
        except (TypeError, ValueError):
            return
        self._run('set_modulation_power_mw', value)

    def getModulationPower(self):
        """Read back the modulation setpoint, falling back to the config value.

        A controller that cannot report its setpoint is not an error worth
        propagating to the GUI — the configured idle power is the best answer
        available.
        """
        profile = self._profile
        if profile is None:
            return self._modulation_power_mw
        try:
            return profile.get_modulation_power_mw(self._laser)
        except (ProtocolError, ValueError, TypeError):
            return self._modulation_power_mw

    def getFirmwareInfo(self):
        """Return the Cobolt identity and selected command profile."""
        return {
            'firmware': self._firmware_version,
            'serial': self._serial_number,
            'model': self._model_number,
            'profileId': self._profile.profile_id if self._profile else None,
            'requestedProtocolProfile': self._protocol_profile or 'auto',
            'requestedEmissionControl': self._emission_control,
            'resolvedEmissionControl': 'pause' if self._pause_mode else 'master',
            'emissionControl': 'pause' if self._pause_mode else 'master',
            'scpiPowerUnit': 'W' if self._scpi_power_unit == 'w' else 'mW',
        }

    def finalize(self) -> None:
        """Drive the laser to a safe-off state and release the port.

        The safe-off command and the port close are independent: if darkening
        the beam fails we still release the connection, otherwise the port
        stays open until the process exits and the next session cannot
        reconnect.
        """
        try:
            if self._pause_mode:
                # Pause the beam without sending l0. Sending l0 here
                # would abort it into the interlock-re-arm state, forcing a
                # physical re-arm at the next session start.
                self._run('pause')
            else:
                self._run('master_off')
        except Exception:
            self.__logger.error(
                f'Could not turn off Cobolt {self._port} during finalize:\n'
                f'{traceback.format_exc()}'
            )
        finally:
            self._close_connection()

    def _close_connection(self) -> None:
        """Release the underlying connection, if it exposes a way to do so.

        Tolerates connections that have neither method (the mock does not),
        and never raises: finalization must not be derailed by a port that is
        already gone.
        """
        laser = getattr(self, '_laser', None)
        if laser is None:
            return
        for method_name in ('disconnect', 'close'):
            method = getattr(laser, method_name, None)
            if not callable(method):
                continue
            try:
                method()
            except Exception as exc:
                self.__logger.warning(
                    f'Could not close Cobolt connection on {self._port} '
                    f'via {method_name}(): {exc}'
                )
                continue
            return

    def getAllDeviceNames(self):  # legacy hook
        try:
            from .PyCoboltManager import list_lasers
            return list_lasers()
        except Exception:
            return []


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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
