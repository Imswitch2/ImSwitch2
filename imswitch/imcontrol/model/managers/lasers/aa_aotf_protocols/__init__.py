"""AA Opto-Electronic AOTF/AOM protocol profiles.

Unlike the Cobolt package, this is a command-extraction cleanup rather than a
dialect-discovery system. The AA controller exposes no read-only identity or
dialect query that is safe to rely on, so there is no ``discovery.py`` and no
automatic selection: the current, field-proven command behavior is captured as
one explicit compatibility profile, and additional profiles are added only when
a vendor manual or a captured hardware transcript justifies them.

Reply handling here is deliberately permissive. The manager has always
discarded ``query()`` return text, so a controller that works today must keep
working: a completed exchange counts as success regardless of what came back,
while transport failures still propagate. Tightening that into real
acknowledgement validation is a separate, evidence-backed change -- guessing at
an acknowledgement contract would start rejecting controllers that are fine.

See ``docs/design/device-protocol-profile-refactor-plan.md``.
"""

from .._protocol import CommandTimeout, TransportFailure


#: Exception-message fragments that indicate a lost reply rather than another
#: transport problem. Kept separate from the Cobolt list because the two
#: families reach different transport stacks (pyvisa/pyserial via RS232Manager
#: here, pyserial directly for Cobolt).
TIMEOUT_MARKERS = (
    'timeout',
    'timed out',
    'no response',
)


def send_query(connection, command: str) -> str:
    """Send ``command`` over the shared RS232 connection and return the reply.

    ``connection`` is the selected ``RS232Manager``; only ``query(str) -> str``
    is used. This coupling is vendor-local and intentional.

    The reply is returned unclassified **on purpose**. Completing the exchange
    is the defined success condition for the compatibility profile; callers may
    record the text for debugging but must not reject a controller on it until
    the real acknowledgement contract is known from hardware.

    Raises:
        CommandTimeout: the exchange timed out; the command may still have run.
        TransportFailure: the exchange failed for another reason.
    """
    try:
        return connection.query(command)
    except Exception as exc:
        detail = str(exc)
        if isinstance(exc, TimeoutError) or any(
            m in detail.lower() for m in TIMEOUT_MARKERS
        ):
            raise CommandTimeout(
                f'No reply from AA controller for {command!r}: {detail}',
                command=command,
            ) from exc
        raise TransportFailure(
            f'Transport failure sending {command!r} to AA controller: '
            f'{detail}',
            command=command,
        ) from exc


def validate_channel(channel) -> int:
    """Return ``channel`` as a valid 1-based AA channel index.

    Only the lower bound is enforced. The manager documents that indexing
    starts at 1, which is verifiable; the maximum channel count varies by
    controller model and is deliberately not invented here. Add an upper bound
    when a manual or transcript establishes one.
    """
    try:
        value = int(channel)
    except (TypeError, ValueError):
        raise ValueError(
            f'AA channel must be an integer, got {channel!r}'
        ) from None
    if value < 1:
        raise ValueError(
            f'AA channel indexing starts at 1, got {value}'
        )
    return value


def validate_amplitude(value) -> int:
    """Return ``value`` as a valid AA amplitude word.

    Negative amplitudes are rejected rather than sent: they are meaningless to
    the controller, and the caller is responsible for deciding what to do
    instead. As with the channel index, no upper bound is asserted without
    hardware evidence -- the shipped example setup uses 1023, but that is one
    controller's configuration, not a protocol guarantee.
    """
    try:
        amplitude = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f'AA amplitude must be an integer, got {value!r}'
        ) from None
    if amplitude < 0:
        raise ValueError(f'AA amplitude must not be negative, got {amplitude}')
    return amplitude


#: Profile used when ``protocolProfile`` is omitted. Omission is the upgrade
#: path for every existing setup file, so it must reproduce current behavior
#: rather than pretend to discover an unidentified controller.
DEFAULT_PROFILE_ID = 'aa.compatibility'


def build_profiles() -> dict:
    """Return one fresh instance of every known AA profile, by id."""
    from .compatibility import AACompatibilityProfile

    profiles = [AACompatibilityProfile()]
    return {profile.profile_id: profile for profile in profiles}


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
