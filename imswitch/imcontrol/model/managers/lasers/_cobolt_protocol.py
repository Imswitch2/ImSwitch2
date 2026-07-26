"""Cobolt command transmission and reply classification.

This is the single place where a Cobolt reply is turned into either a value or
one of the shared failure types in :mod:`._protocol`. Keeping it in one
function is what stops each future Cobolt profile from re-implementing
"does this reply mean the command worked?" slightly differently.

The module is standalone by design. Phase 2 of
``docs/design/device-protocol-profile-refactor-plan.md`` lifts its contents
into ``cobolt0601_protocols/__init__.py`` as the vendor-local send/reply
helper; it should move mechanically rather than being rewritten.
"""

from ._protocol import (
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
