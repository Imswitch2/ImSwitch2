"""Cobolt 06-01 / 06-MLD protocol profiles.

A *profile* translates named device operations into Cobolt commands and
classifies the replies. It is a verified set of compatible operations, not
necessarily one clean vendor dialect: the SCPI-compatible profile deliberately
keeps short-form fallbacks that deployed controllers are known to need. Whether
the real population splits cleanly into legacy and SCPI, or needs a mixed
profile, is a question for the hardware inventory (Phase 1B) -- not an
assumption to bake in now.

This module holds what every Cobolt profile shares: the send/reply helper
(lifted unchanged from the Phase 1A ``_cobolt_protocol`` module), the
variant-resolution helper, identity reading, and the profile registry. Keeping
reply classification here is what stops each profile from re-deciding "did this
command work?" slightly differently.

See ``docs/design/device-protocol-profile-refactor-plan.md``.
"""

from .._protocol import (
    CommandRejected,
    CommandTimeout,
    TransportFailure,
    UnexpectedReply,
)


#: Reply fragments that mean the controller understood the command and refused
#: it. A rejected command has not executed.
REJECTION_MARKERS = (
    'illegal command',
    'syntax error',
    'permission denied',
    'not allowed',
    'out of range',
)

#: Exception-message fragments that mean no reply came back in time. Notably
#: ``PyCoboltManager.send_cmd`` reports a missing reply as
#: ``RuntimeError("Syntax Error: No response on ...")`` -- despite the wording
#: that is a lost reply, not a device rejection, so it must be classified as a
#: timeout with an unknown outcome.
TIMEOUT_MARKERS = (
    'no response',
    'timeout',
    'timed out',
)


def classify_reply(command: str, reply) -> str:
    """Return the stripped reply text, or raise the matching failure type.

    Raises:
        UnexpectedReply: the reply was absent or empty.
        CommandRejected: the controller explicitly refused the command.
    """
    text = '' if reply is None else str(reply).strip()
    if not text:
        # An empty reply is indistinguishable from a half-open port. Accepting
        # it would let a mutating command look successful when nothing was
        # acknowledged.
        raise UnexpectedReply(
            f'Cobolt returned an empty reply to {command!r}',
            command=command,
            reply=text,
        )

    lowered = text.lower()
    if lowered.startswith('error') or any(m in lowered for m in REJECTION_MARKERS):
        raise CommandRejected(
            f'Cobolt rejected {command!r}: {text!r}',
            command=command,
            reply=text,
        )
    return text


def send_command(connection, command: str) -> str:
    """Send ``command`` over ``connection`` and classify the outcome.

    ``connection`` is the existing Cobolt connection object; only
    ``send_cmd(str) -> str`` is used. This coupling is vendor-local and
    intentional -- there is no generic transport wrapper.

    Returns:
        The stripped reply text on success.

    Raises:
        CommandTimeout: no reply arrived; the command may still have executed.
        TransportFailure: the exchange failed for another reason.
        UnexpectedReply: the reply was empty or structurally invalid.
        CommandRejected: the controller explicitly refused the command.
    """
    try:
        reply = connection.send_cmd(command)
    except Exception as exc:
        detail = str(exc)
        if isinstance(exc, TimeoutError) or any(
            m in detail.lower() for m in TIMEOUT_MARKERS
        ):
            raise CommandTimeout(
                f'No reply from Cobolt for {command!r}: {detail}',
                command=command,
            ) from exc
        raise TransportFailure(
            f'Transport failure sending {command!r} to Cobolt: {detail}',
            command=command,
        ) from exc

    return classify_reply(command, reply)


def send_first_supported(connection, commands, *, purpose: str = '',
                         cache: dict = None) -> str:
    """Send the first variant the controller accepts.

    Advances to the next variant **only** after an explicit rejection, which
    proves the controller did not act on the previous one. Any other failure
    propagates immediately: after a timeout or an empty reply the outcome is
    unknown, and sending a second mutating variant could double-apply it.

    ``cache`` maps ``purpose`` to the index that last worked, so a known-losing
    variant is not retried on every setValue or scan transition.

    Raises:
        CommandRejected: every variant was rejected (the last rejection).
        ProtocolError: the first unknown-outcome failure encountered.
    """
    cached_index = cache.get(purpose) if cache is not None else None
    if cached_index is not None and cached_index >= len(commands):
        cached_index = None

    order = []
    if cached_index is not None:
        order.append(cached_index)
    order.extend(i for i in range(len(commands)) if i != cached_index)

    last_rejection = None
    for index in order:
        try:
            reply = send_command(connection, commands[index])
        except CommandRejected as exc:
            last_rejection = exc
            if cache is not None:
                cache.pop(purpose, None)
            continue
        if cache is not None:
            cache[purpose] = index
        return reply

    raise last_rejection


#: Run modes Cobolt controllers report for ``LASer:RUNMode?``. Used to check
#: the *shape* of a probe reply: a connection that answers "OK" to everything
#: must not be mistaken for an SCPI controller. An unrecognised mode makes
#: discovery indeterminate rather than positive -- deliberately conservative,
#: and a candidate for widening once Phase 1B transcripts exist.
RUN_MODES = frozenset({
    'constantcurrent',
    'constantpower',
    'currentmodulation',
    'powermodulation',
    'modulation',
    'off',
})


def parse_run_mode(command: str, reply: str) -> str:
    """Validate a run-mode reply, or raise :class:`UnexpectedReply`."""
    normalised = reply.replace(' ', '').replace('_', '').lower()
    if normalised not in RUN_MODES:
        raise UnexpectedReply(
            f'Cobolt answered {command!r} with {reply!r}, which is not a '
            f'known run mode; the reply does not identify an SCPI controller',
            command=command,
            reply=reply,
        )
    return normalised


def parse_float(command: str, reply: str) -> float:
    """Validate a numeric reply, or raise :class:`UnexpectedReply`."""
    try:
        return float(reply)
    except (TypeError, ValueError):
        raise UnexpectedReply(
            f'Cobolt answered {command!r} with {reply!r}, which is not the '
            f'expected numeric value',
            command=command,
            reply=reply,
        ) from None


def parse_flag(command: str, reply: str, allowed=('0', '1')) -> str:
    """Validate a small enumerated reply, or raise :class:`UnexpectedReply`."""
    text = reply.strip()
    if text not in allowed:
        raise UnexpectedReply(
            f'Cobolt answered {command!r} with {reply!r}; expected one of '
            f'{", ".join(allowed)}',
            command=command,
            reply=reply,
        )
    return text


def probe_query(connection, command: str, parser, evidence: list) -> bool:
    """Run one read-only probe query and classify the reply.

    Returns True on a valid positive reply and False on an explicit rejection,
    which is clean negative evidence: the controller understood the query and
    refused it, so it simply does not speak this dialect.

    Every other failure propagates. A timeout, transport failure, or
    structurally invalid reply means the probe learned *nothing*, and treating
    "no answer" as "not this dialect" is how a healthy controller with a flaky
    link gets driven with the wrong command set.
    """
    try:
        reply = send_command(connection, command)
    except CommandRejected as exc:
        evidence.append((command, f'rejected: {exc.reply!r}'))
        return False
    parser(command, reply)
    evidence.append((command, reply))
    return True


def read_identity(connection) -> dict:
    """Read the Cobolt identity fields that both dialects share.

    Deliberately not profile-specific: identity has to be readable *before* a
    profile is selected, and ``gfv?``/``sn?``/``glm?`` are the same hooks
    Cobolt's own ``pycobolt`` package uses. Missing fields come back as None
    rather than raising -- an old controller that cannot answer ``glm?`` is
    still perfectly usable.
    """
    def _optional(command):
        try:
            return send_command(connection, command)
        except Exception:
            return None

    firmware = (
        _optional('gfv?')
        or getattr(connection, 'firmware', None)
        or getattr(connection, 'firmware_version', None)
    )
    serial = (
        _optional('sn?')
        or _optional('gsn?')
        or getattr(connection, 'serialnumber', None)
    )
    model = _optional('glm?') or getattr(connection, 'modelnumber', None)

    return {
        'firmware': str(firmware).strip() if firmware else None,
        'serial': str(serial).strip() if serial else None,
        'model': str(model).strip() if model else None,
    }


def build_profiles(power_unit: str = 'mw') -> dict:
    """Return one fresh instance of every known Cobolt profile, by id.

    Instances are per-manager because each carries its own resolved-variant
    cache; two lasers on different controllers must not share one.
    """
    from .legacy import LegacyProfile
    from .scpi_compatible import ScpiCompatibleProfile

    profiles = [LegacyProfile(), ScpiCompatibleProfile(power_unit=power_unit)]
    return {profile.profile_id: profile for profile in profiles}


#: Order in which ``protocolProfile: "auto"`` considers candidates. A positive
#: SCPI signature wins over legacy query responses, preserving the manager's
#: long-standing precedence: modern controllers often still answer short-form
#: queries such as ``l?``, so overlap is expected and is not an error.
AUTO_SELECTION_ORDER = ('cobolt.scpi-compatible', 'cobolt.legacy')


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
