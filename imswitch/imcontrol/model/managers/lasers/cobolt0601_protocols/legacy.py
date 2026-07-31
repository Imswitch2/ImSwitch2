"""Legacy Cobolt 06-01 / 06-MLD short-form command profile.

The command set used by older 06-01 / 06-MLD controllers: ``l0``/``l1`` for the
master switch, ``cp``/``p`` for constant power, ``em``/``slmp``/``sdmes`` for
modulation. These controllers reject the SCPI-style variants outright.

Note the unit split, which is a property of the individual commands rather than
of the dialect: ``p`` takes **watts** while ``slmp`` takes **milliwatts**.
"""

from .._protocol import ProbeResult, UnsupportedOperation
from . import parse_flag, probe_query, send_command


class LegacyProfile:
    """Short-form Cobolt commands, as used by 06-01 / 06-MLD firmware."""

    profile_id = 'cobolt.legacy'
    auto_selectable = True

    #: Declared so the manager can reject an incompatible emission policy at
    #: startup instead of discovering it at the first enable. Kept in step with
    #: the methods below, which raise UnsupportedOperation if called anyway.
    unsupported_operations = frozenset({'pause', 'resume'})

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def probe(self, connection) -> ProbeResult:
        """Read-only probe for the short-form command set.

        ``l?`` is the master on/off query and answers ``0``/``1`` on every
        legacy firmware revision. Some controllers answer mode queries even
        when master state is unavailable, so ``gam?`` is a second non-mutating
        probe. Both are attempted so the fingerprint records what responded.

        Replies are shape-checked, so a connection that answers ``OK`` to
        everything is not mistaken for a legacy controller. Only an explicit
        rejection counts as clean negative evidence; anything else propagates
        and makes discovery indeterminate.

        The accepted forms are provisional: they match every unit and mock
        seen so far, and Phase 1B transcripts should confirm them.
        """
        positive = []
        evidence = []

        if probe_query(connection, 'l?',
                       lambda c, r: parse_flag(c, r, ('0', '1')), evidence):
            positive.append('l?')
        if probe_query(connection, 'gam?',
                       lambda c, r: parse_flag(c, r, ('0', '1', '2')),
                       evidence):
            positive.append('gam?')

        return ProbeResult(
            profile_id=self.profile_id,
            positive=tuple(positive),
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
        raise UnsupportedOperation(
            'Legacy Cobolt firmware has no las:paus command; '
            'emissionControl="pause" requires an SCPI-capable controller.'
        )

    def resume(self, connection) -> None:
        raise UnsupportedOperation(
            'Legacy Cobolt firmware has no las:paus command; '
            'emissionControl="pause" requires an SCPI-capable controller.'
        )

    # ------------------------------------------------------------------
    # Power and modulation
    # ------------------------------------------------------------------

    def enter_constant_power(self, connection) -> None:
        send_command(connection, 'cp')

    def set_constant_power_mw(self, connection, value_mw) -> None:
        # 'p' takes watts, hence the /1000 -- unlike 'slmp' below.
        send_command(connection, f'p {float(value_mw) / 1000.0:.6f}')

    def enter_modulation_mode(self, connection) -> None:
        send_command(connection, 'em')

    def set_modulation_power_mw(self, connection, value_mw) -> None:
        send_command(connection, f'slmp {float(value_mw)}')

    def set_digital_modulation_enabled(self, connection, enabled) -> None:
        send_command(connection, f'sdmes {1 if enabled else 0}')

    def get_modulation_power_mw(self, connection) -> float:
        return float(send_command(connection, 'glmp?'))


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
