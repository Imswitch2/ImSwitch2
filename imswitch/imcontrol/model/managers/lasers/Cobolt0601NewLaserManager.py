"""LaserManager for Cobolt 06-01 / 06-MLD lasers.

This implementation talks directly through ``laser.send_cmd(...)`` and
selects the command profile at startup. Older 06-01 / 06-MLD units use the
short Cobolt command set (``l0``/``l1``/``em``/``sdmes``/``slmp``). Newer
firmware uses the SCPI-style profile exposed by Cobolt's official
``pycobolt`` package (``LASer:RUNMode`` /
``LASer:CP:POWer:SETPoint`` / ``LASer:PowerModulation:POWer:SETPoint``).

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

from imswitch.imcommon.model import initLogger, pythontools
from .LaserManager import LaserManager, normalise_ports
import importlib
import traceback


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
    - ``scpiPowerUnit`` -- unit expected by SCPI power setpoint commands:
      ``"mW"`` (default, matches upstream ``pycobolt``) or ``"W"`` for
      firmware/configurations that expose SCPI setpoints in watts.
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
        # Firmware capability — detected at init. True for SCPI-only/newer
        # Cobolt firmware (e.g. Skyra) that rejects the short-form commands
        # `em`/`slmp`/`sdmes`/`cp`. False for legacy 06-01/06-MLD which
        # rejects the SCPI variants `LAS:RUNM`/`las:pm:dig:ena`/`las:paus`.
        # Initialised to None and set during _detect_firmware().
        self._scpi = None
        self._firmware_version = None
        self._serial_number = None
        self._model_number = None
        self._command_variant_cache = {}

        self.__logger.debug(f'Initializing Cobolt laser {name} on {self._port}')
        try:
            self._laser = self._Cobolt06(port=self._port)
            self._real_hw = True
            self._detect_firmware()
            self._init_safe_state()
            super().__init__(laserInfo, name, isBinary=False,
                             valueUnits='mW', valueDecimals=0)
        except Exception as exc:
            self.__logger.error(
                f'Failed to open real Cobolt {name} on {self._port}; '
                f'loading mock: {exc}'
            )
            self.__logger.debug(
                f'Cobolt {name} open failure traceback:\n{traceback.format_exc()}'
            )
            package = importlib.import_module(
                pythontools.joinModulePath(
                    'imswitch.imcontrol.model.lantzdrivers_mock.cobolt.', 'cobolt0601'
                )
            )
            driver = getattr(package, 'MockCobolt06')
            self._laser = driver(self._port)
            self._laser.initialize()
            super().__init__(laserInfo, name, isBinary=False,
                             valueUnits='mW', valueDecimals=0)

    # ------------------------------------------------------------------
    # Raw command shim — bypasses the SCPI helpers in PyCoboltManager.
    # ------------------------------------------------------------------

    def _cmd(self, command: str, *, log_failure: bool = True):
        """Send a raw Cobolt command and return ``(ok, reply)``.

        ``ok`` is True if the controller produced a reply and the reply
        does not look like an error message ("illegal command" or
        "syntax error"). Transport failures (no reply, raised exception)
        return ``(False, '')``.

        Set ``log_failure=False`` only for expected probe failures, such
        as firmware-family detection. Callers that drive state transitions
        MUST check ``ok`` before updating internal flags — see
        :meth:`setEnabled`. Fail-open semantics are unsafe for laser
        control.
        """
        try:
            reply = self._laser.send_cmd(command)
        except Exception as e:
            if log_failure:
                self.__logger.error(f'Command {command!r} raised: {e}')
            return False, ''
        reply = reply or ''
        rl = reply.lower()
        if 'illegal command' in rl or 'syntax error' in rl:
            if log_failure:
                self.__logger.warning(
                    f'Cobolt firmware rejected {command!r}: {reply!r}'
                )
            return False, reply
        return True, reply

    def _cmd_or_warn(self, command: str) -> bool:
        """Send a command and log a warning on failure. Returns ``ok``."""
        ok, _ = self._cmd(command)
        if not ok:
            self.__logger.warning(
                f'Command {command!r} did not succeed; downstream state '
                f'may be inconsistent.'
            )
        return ok

    def _cmd_any(self, commands, purpose: str) -> bool:
        """Try command variants in order and cache the winning index.

        ``commands`` contain concrete command strings, often with dynamic
        numeric values. Caching the index per purpose avoids retrying a known
        losing variant on every setValue/scan transition while still allowing
        fallback if a later command fails.
        """
        if not hasattr(self, '_command_variant_cache'):
            self._command_variant_cache = {}

        failures = []
        cached_index = self._command_variant_cache.get(purpose)
        if cached_index is not None and cached_index < len(commands):
            command = commands[cached_index]
            ok, reply = self._cmd(command, log_failure=False)
            if ok:
                return True
            failures.append(f'{command!r} -> {reply!r}')
            self._command_variant_cache.pop(purpose, None)

        for index, command in enumerate(commands):
            if index == cached_index:
                continue
            ok, reply = self._cmd(command, log_failure=False)
            if ok:
                self._command_variant_cache[purpose] = index
                return True
            failures.append(f'{command!r} -> {reply!r}')

        self.__logger.warning(
            f'Cobolt {self._port}: all command variants failed for '
            f'{purpose}: {", ".join(failures)}'
        )
        return False

    def _query_optional(self, command: str):
        ok, reply = self._cmd(command, log_failure=False)
        if not ok:
            return None
        value = str(reply).strip()
        return value if value else None

    def _read_identity(self) -> None:
        """Read and log the Cobolt identity fields when the controller exposes them."""
        firmware = (
            self._query_optional('gfv?')
            or getattr(self._laser, 'firmware', None)
            or getattr(self._laser, 'firmware_version', None)
        )
        serial = (
            self._query_optional('sn?')
            or self._query_optional('gsn?')
            or getattr(self._laser, 'serialnumber', None)
        )
        model = self._query_optional('glm?') or getattr(self._laser, 'modelnumber', None)

        self._firmware_version = str(firmware).strip() if firmware else None
        self._serial_number = str(serial).strip() if serial else None
        self._model_number = str(model).strip() if model else None

        self.__logger.info(
            f'Cobolt {self._port} identity: '
            f'firmware={self._firmware_version or "unknown"}, '
            f'serial={self._serial_number or "unknown"}, '
            f'model={self._model_number or "unknown"}'
        )

    def _scpi_power_arg(self, value_mw: float) -> float:
        """Convert a GUI mW value to the configured SCPI setpoint unit."""
        value_mw = float(value_mw)
        if self._scpi_power_unit == 'w':
            return value_mw / 1000.0
        return value_mw

    def _scpi_power_reply_to_mw(self, value: float) -> float:
        value = float(value)
        if self._scpi_power_unit == 'w':
            return value * 1000.0
        return value

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
          - Both probe groups fail → log ERROR (controller unresponsive
            or wrong device on the port) and default to legacy as a
            best-effort. Subsequent commands will fail loudly thanks to
            the fail-closed logic in :meth:`setEnabled`, so the operator
            gets a clear signal early.

        SCPI takes priority when both succeed — it's the modern command
        set and avoids ambiguity on units. Decision is one-shot for the
        lifetime of the manager and logged at INFO (or ERROR on the
        unresponsive path).
        """
        self._read_identity()

        ok_runmode, _ = self._cmd('LASer:RUNMode?', log_failure=False)
        ok_power_generic, _ = self._cmd('LASer:POWer:SETPoint?', log_failure=False)
        ok_power_cp, _ = self._cmd('LASer:CP:POWer:SETPoint?', log_failure=False)
        ok_power_pm, _ = self._cmd(
            'LASer:PowerModulation:POWer:SETPoint?', log_failure=False
        )
        scpi_ok = ok_runmode and (
            ok_power_generic or ok_power_cp or ok_power_pm
        )

        # Legacy probe: ``l?`` returns "0" or "1" on every Cobolt
        # firmware revision I'm aware of (it's the master on/off query). Some
        # controllers answer mode queries even when master state is unavailable,
        # so ``gam?`` is a second non-mutating legacy probe.
        ok_legacy_probe, _ = self._cmd('l?', log_failure=False)
        ok_legacy_mode, _ = self._cmd('gam?', log_failure=False)
        legacy_ok = ok_legacy_probe or ok_legacy_mode

        if scpi_ok:
            self._scpi = True
            self.__logger.debug(
                f'Cobolt {self._port}: SCPI firmware detected; '
                f'using SCPI command set '
                f'(runmode={ok_runmode}, generic_power={ok_power_generic}, '
                f'cp_power={ok_power_cp}, pm_power={ok_power_pm}, '
                f'scpiPowerUnit={self._scpi_power_unit}).'
            )
        elif legacy_ok:
            self._scpi = False
            self.__logger.debug(
                f'Cobolt {self._port}: legacy firmware detected; '
                f'using em/slmp/sdmes/cp command set '
                f'(l?={ok_legacy_probe}, gam?={ok_legacy_mode}).'
            )
        else:
            self._scpi = False
            self.__logger.error(
                f'Cobolt {self._port}: NEITHER firmware family responds. '
                f'SCPI probes (LASer:RUNMode?, SCPI power setpoint queries) '
                f'and legacy probes (l?, gam?) all failed. Controller may be '
                f'powered off, unplugged, on the wrong port, or in an error state. '
                f'Defaulting to legacy command set as a best-effort — '
                f'subsequent commands will likely fail.'
            )

        if self._emission_control == 'auto':
            self._pause_mode = False
            self.__logger.warning(
                f'Cobolt {self._port}: emissionControl=auto is diagnostic only '
                f'and resolves to master control. Detected '
                f'{"SCPI" if self._scpi else "legacy"} command family; set '
                f'emissionControl="pause" explicitly only for OEM/interlock '
                f'firmware that must not receive l0.'
            )

    # --- Per-action helpers that branch on firmware ---
    # Each returns True only if every required command succeeded.

    def _enter_constant_power(self) -> bool:
        if self._scpi:
            return self._cmd_any(
                ['LASer:RUNMode ConstantPower', 'LAS:RUNM ConstantPower', 'cp'],
                'enter constant-power mode',
            )
        return self._cmd_or_warn('cp')

    def _enter_modulation_mode(self, mod_power_mw: float) -> bool:
        """Enter digital-modulation mode at the given modulation power.

        SCPI power commands default to mW because that is what the official
        upstream ``pycobolt.Cobolt06`` wrapper sends. Set
        ``managerProperties.scpiPowerUnit = "W"`` for controllers configured
        to expose SCPI setpoints in watts.
        """
        if self._scpi:
            scpi_value = self._scpi_power_arg(mod_power_mw)
            ok1 = self._cmd_any(
                [
                    f'LASer:PowerModulation:POWer:SETPoint {scpi_value}',
                    f'slmp {float(mod_power_mw)}',
                ],
                'set modulation power',
            )
            ok2 = self._cmd_any(
                ['LASer:RUNMode PowerModulation', 'LAS:RUNM PowerModulation', 'em'],
                'enter power-modulation mode',
            )
            ok3 = self._cmd_any(
                ['las:pm:dig:ena 1', 'sdmes 1'],
                'enable digital modulation',
            )
            return ok1 and ok2 and ok3
        ok1, _ = self._cmd(f'slmp {float(mod_power_mw)}')
        ok2, _ = self._cmd('em')
        ok3, _ = self._cmd('sdmes 1')
        return ok1 and ok2 and ok3

    def _set_cw_power_mw(self, value_mw: float) -> bool:
        """Set the constant-power setpoint."""
        if self._scpi:
            scpi_value = self._scpi_power_arg(value_mw)
            return self._cmd_any(
                [
                    f'LASer:CP:POWer:SETPoint {scpi_value}',
                    f'p {float(value_mw) / 1000.0:.6f}',
                ],
                'set constant-power setpoint',
            )
        return self._cmd_or_warn(f'p {float(value_mw) / 1000.0:.6f}')

    # ------------------------------------------------------------------
    # Safe-state setup + ImSwitch API
    # ------------------------------------------------------------------

    def _init_safe_state(self) -> None:
        """Put the laser into the safe state at startup.

        - autostart off (``@cobas 0``) — universal
        - modulation mode armed at the configured idle power — firmware-branched
        - master ``l0`` so even without TTL the beam is dark — universal

        ``l0`` is the safety-critical command; if it fails we log at
        ERROR. The mode-entry is best-effort because the master switch
        already guarantees the beam is dark.
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
            ok_pause, _ = self._cmd('las:paus 1')
            if not ok_pause:
                self.__logger.error(
                    f'CRITICAL: emission-pause (las:paus 1) failed during '
                    f'safe-state init for Cobolt {self._port}. Verify the '
                    f'controller is responsive before opening the shutter.'
                )
            self._enabled = False
            self.__logger.debug(
                f'Cobolt {self._port} initialised in safe state '
                f'(SCPI pause mode: modulation-gated + paused, autostart NOT '
                f'armed — no emission until turned on in the GUI).'
            )
        else:
            self._cmd_or_warn('@cobas 0')
            self._enter_modulation_mode(self._modulation_power_mw)

            ok_off, _ = self._cmd('l0')
            if not ok_off:
                self.__logger.error(
                    f'CRITICAL: master-off l0 failed during safe-state init for '
                    f'Cobolt {self._port}. Verify the controller is responsive '
                    f'before opening the shutter.'
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
            ok_power = self._set_cw_power_mw(self._setpoint_mw)
            ok_mode = self._enter_constant_power()
            ok_master, _ = self._cmd('l1')

            if not (ok_power and ok_mode and ok_master):
                # Something in the on-sequence failed. Drive the laser
                # back to the safe state and leave _enabled = False so
                # the GUI / scripting layer can see the transition did
                # not take effect.
                self.__logger.error(
                    f'Cobolt {self._port} failed to enable '
                    f'(power_ok={ok_power}, mode_ok={ok_mode}, '
                    f'master_ok={ok_master}); reverting to safe-off state.'
                )
                self._cmd_or_warn('l0')
                self._enter_modulation_mode(self._modulation_power_mw)
                self._enabled = False
                return

            self._enabled = True
        else:
            # Master off is the critical step — must succeed. If it does
            # not, _enabled stays True so callers know the off-transition
            # did not actually happen.
            ok_off, _ = self._cmd('l0')
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
            # Always flush the setpoint, including 0 (see setEnabled).
            ok_power = self._set_cw_power_mw(self._setpoint_mw)
            ok_mode = self._enter_constant_power()
            ok_resume, _ = self._cmd('las:paus 0')
            if not (ok_resume and ok_mode and ok_power):
                self.__logger.error(
                    f'Cobolt {self._port} failed to enable in pause mode '
                    f'(resume_ok={ok_resume}, mode_ok={ok_mode}, '
                    f'power_ok={ok_power}); pausing emission.'
                )
                self._cmd_or_warn('las:paus 1')
                self._enabled = False
                return
            self._enabled = True
        else:
            ok_pause, _ = self._cmd('las:paus 1')
            if not ok_pause:
                self.__logger.error(
                    f'CRITICAL: Cobolt {self._port} emission-pause '
                    f'(las:paus 1) failed; _enabled remains True. Verify '
                    f'shutter state manually.'
                )
                return
            self._enabled = False

    def setValue(self, power) -> None:
        """Update the constant-power setpoint.

        Only flushed to the laser when ``_enabled`` is True. When the
        laser is off, the new value is cached and will take effect on
        the next ``setEnabled(True)``.
        """
        try:
            value = int(power)
        except (TypeError, ValueError):
            self.__logger.warning(f'Ignoring non-numeric setValue({power!r})')
            return
        self._setpoint_mw = value

        if not self._enabled:
            return

        self._set_cw_power_mw(value)

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
            self._enter_modulation_mode(self._setpoint_mw)
            if self._setpoint_mw <= 0:
                # A zero GUI setpoint is an explicit off command. Keep the
                # master/resume state off even though the scan includes this
                # laser, so TTL pulses cannot produce emission.
                if self._pause_mode:
                    self._cmd('las:paus 1')
                else:
                    self._cmd('l0')
                return
            if self._pause_mode:
                # Laser is already started; just un-pause. The digital gate
                # (las:pm:dig:ena 1, set by _enter_modulation_mode) keeps the
                # beam dark until the scanner drives the TTL line HIGH.
                self._cmd('las:paus 0')
            else:
                self._cmd('l1')   # master on; TTL gates the actual emission
        else:
            # Leave scan mode — back to the saved enable state.
            self.setEnabled(self._enabled)

    def setModulationEnabled(self, enabled: bool) -> None:
        if self._scpi:
            self._cmd_any(
                [
                    f'las:pm:dig:ena {1 if enabled else 0}',
                    f'sdmes {1 if enabled else 0}',
                ],
                'set digital modulation enabled',
            )
        else:
            self._cmd(f'sdmes {1 if enabled else 0}')

    def setModulationPower(self, power) -> None:
        try:
            value = float(power)
        except (TypeError, ValueError):
            return
        if self._scpi:
            self._cmd_any(
                [
                    f'LASer:PowerModulation:POWer:SETPoint {self._scpi_power_arg(value)}',
                    f'slmp {value}',
                ],
                'set modulation power',
            )
        else:
            self._cmd(f'slmp {value}')

    def getModulationPower(self):
        try:
            if self._scpi:
                ok, reply = self._cmd('LASer:PowerModulation:POWer:SETPoint?')
                if ok:
                    return self._scpi_power_reply_to_mw(float(reply))
                ok, reply = self._cmd('glmp?')
                if ok:
                    return float(reply)
                return self._modulation_power_mw

            ok, reply = self._cmd('glmp?')
            if ok:
                return float(reply)
            return self._modulation_power_mw
        except (ValueError, TypeError):
            return self._modulation_power_mw

    def getFirmwareInfo(self):
        """Return the Cobolt identity and selected command profile."""
        return {
            'firmware': self._firmware_version,
            'serial': self._serial_number,
            'model': self._model_number,
            'commandSet': 'SCPI' if self._scpi else 'legacy',
            'requestedEmissionControl': self._emission_control,
            'resolvedEmissionControl': 'pause' if self._pause_mode else 'master',
            'emissionControl': 'pause' if self._pause_mode else 'master',
            'scpiPowerUnit': 'W' if self._scpi_power_unit == 'w' else 'mW',
        }

    def finalize(self) -> None:
        """Drive the laser to a safe-off state on shutdown."""
        try:
            if self._pause_mode:
                # Pause the beam without sending l0. Sending l0 here
                # would abort it into the interlock-re-arm state, forcing a
                # physical re-arm at the next session start.
                self._cmd('las:paus 1')
            else:
                self._cmd('l0')
        except Exception:
            self.__logger.error(
                f'Could not turn off Cobolt {self._port} during finalize:\n'
                f'{traceback.format_exc()}'
            )

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
