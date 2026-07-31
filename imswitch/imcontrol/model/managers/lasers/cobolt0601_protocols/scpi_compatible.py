"""SCPI-compatible Cobolt profile.

Named *compatible* rather than *SCPI* on purpose. It is not a pure dialect:
several operations fall back to short-form commands because deployed
controllers are known to accept SCPI for one operation and only the short form
for another. Those fallbacks are preserved exactly as the manager used them
before the extraction.

Whether this should eventually split into a pure-SCPI profile and a separate
mixed profile is a question for the hardware inventory (Phase 1B). Until
transcripts exist, collapsing the fallbacks would be guessing -- and a wrong
guess about a power command is a 1000-fold setpoint error.
"""

from .._protocol import ProbeResult
from . import (
    parse_float,
    parse_run_mode,
    probe_query,
    send_command,
    send_first_supported,
)


class ScpiCompatibleProfile:
    """SCPI command set, retaining the short-form fallbacks in current use."""

    profile_id = 'cobolt.scpi-compatible'
    auto_selectable = True
    unsupported_operations = frozenset()

    def __init__(self, power_unit: str = 'mw'):
        # Power setpoint unit. mW matches upstream pycobolt; some controllers
        # are configured to expose SCPI setpoints in watts. Mutable because the
        # manager still owns the temporary `scpiPowerUnit` override, which the
        # plan expects to become a profile distinction once verified.
        self.power_unit = power_unit
        # Resolved-variant cache, per purpose. Per instance, so two lasers on
        # different controllers never share a resolution.
        self._variant_cache = {}

    # ------------------------------------------------------------------
    # Units
    # ------------------------------------------------------------------

    def _power_arg(self, value_mw) -> float:
        value_mw = float(value_mw)
        return value_mw / 1000.0 if self.power_unit == 'w' else value_mw

    def _power_to_mw(self, value) -> float:
        value = float(value)
        return value * 1000.0 if self.power_unit == 'w' else value

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def probe(self, connection) -> ProbeResult:
        """Read-only probe for the SCPI command set.

        Requires the run-mode query plus at least one SCPI power setpoint
        query: run-mode alone is not enough, because a controller can answer it
        while rejecting every SCPI setpoint form. All setpoint variants are
        attempted rather than short-circuiting, so the fingerprint records
        which ones the controller actually supports.

        Replies are shape-checked -- a known run mode, and numeric setpoints --
        so a connection that answers ``OK`` to every query is not mistaken for
        an SCPI controller. Only an explicit rejection is clean negative
        evidence; a timeout or malformed reply propagates and makes discovery
        indeterminate.
        """
        positive = []
        negative = []
        evidence = []

        runmode_ok = probe_query(
            connection, 'LASer:RUNMode?', parse_run_mode, evidence
        )
        setpoint_ok = False
        for command in ('LASer:POWer:SETPoint?', 'LASer:CP:POWer:SETPoint?',
                        'LASer:PowerModulation:POWer:SETPoint?'):
            setpoint_ok |= probe_query(connection, command, parse_float,
                                       evidence)

        if runmode_ok and setpoint_ok:
            positive.append('LASer:RUNMode? + SCPI power setpoint query')
        elif runmode_ok:
            # Answering run-mode but no setpoint form is not an SCPI
            # controller for our purposes: every power write would fail.
            negative.append('SCPI run-mode answered but no SCPI setpoint form')

        return ProbeResult(
            profile_id=self.profile_id,
            positive=tuple(positive),
            negative=tuple(negative),
            evidence=tuple(evidence),
        )

    # ------------------------------------------------------------------
    # Lifecycle and emission
    # ------------------------------------------------------------------

    def disable_autostart(self, connection) -> None:
        send_command(connection, '@cobas 0')

    def start_controller(self, connection) -> None:
        """Start the controller's laser/TEC sequence under software control."""
        send_command(connection, '@cob1')

    def master_off(self, connection) -> None:
        send_command(connection, 'l0')

    def master_on(self, connection) -> None:
        send_command(connection, 'l1')

    def pause(self, connection) -> None:
        send_command(connection, 'las:paus 1')

    def resume(self, connection) -> None:
        send_command(connection, 'las:paus 0')

    # ------------------------------------------------------------------
    # Power and modulation
    # ------------------------------------------------------------------

    def enter_constant_power(self, connection) -> None:
        send_first_supported(
            connection,
            ['LASer:RUNMode ConstantPower', 'LAS:RUNM ConstantPower', 'cp'],
            purpose='enter constant-power mode',
            cache=self._variant_cache,
        )

    def set_constant_power_mw(self, connection, value_mw) -> None:
        send_first_supported(
            connection,
            [
                f'LASer:CP:POWer:SETPoint {self._power_arg(value_mw)}',
                f'p {float(value_mw) / 1000.0:.6f}',
            ],
            purpose='set constant-power setpoint',
            cache=self._variant_cache,
        )

    def enter_modulation_mode(self, connection) -> None:
        send_first_supported(
            connection,
            ['LASer:RUNMode PowerModulation', 'LAS:RUNM PowerModulation', 'em'],
            purpose='enter power-modulation mode',
            cache=self._variant_cache,
        )

    def set_modulation_power_mw(self, connection, value_mw) -> None:
        send_first_supported(
            connection,
            [
                f'LASer:PowerModulation:POWer:SETPoint '
                f'{self._power_arg(value_mw)}',
                f'slmp {float(value_mw)}',
            ],
            purpose='set modulation power',
            cache=self._variant_cache,
        )

    def set_digital_modulation_enabled(self, connection, enabled) -> None:
        value = 1 if enabled else 0
        send_first_supported(
            connection,
            [f'las:pm:dig:ena {value}', f'sdmes {value}'],
            purpose='set digital modulation enabled',
            cache=self._variant_cache,
        )

    def get_modulation_power_mw(self, connection) -> float:
        try:
            reply = send_command(
                connection, 'LASer:PowerModulation:POWer:SETPoint?'
            )
        except Exception:
            # Short-form query as the fallback; it reports mW directly, so it
            # is not subject to the configured SCPI power unit.
            return float(send_command(connection, 'glmp?'))
        return self._power_to_mw(reply)


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
