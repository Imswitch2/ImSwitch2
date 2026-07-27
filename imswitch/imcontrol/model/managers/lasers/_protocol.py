"""Shared protocol vocabulary for laser device managers.

This module is deliberately small and vendor-independent. It holds the
exception taxonomy, the probe result, and the typing-only profile-selection
contract that vendor protocol packages and the ImSwitch-facing managers agree
on. It does not open hardware, send commands, implement a base manager, or
model device capabilities.

The reason the exception types are split this finely is a safety asymmetry
that matters for laser control:

- :class:`CommandRejected` means the device understood the command and refused
  it, so it provably did **not** execute. A documented alternative command may
  safely be tried.
- :class:`TransportFailure`, :class:`CommandTimeout` and
  :class:`UnexpectedReply` mean the outcome is **unknown** -- the command may
  already have reached the device even though no usable reply came back. An
  alternative must never be tried, because the first command may have taken
  effect and a second one would double-apply it.

See ``docs/design/device-protocol-profile-refactor-plan.md``.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class ProtocolError(Exception):
    """Base class for protocol-level failures raised by a device profile.

    A ``ProtocolError`` always describes the outcome of a single command
    exchange. Configuration and lifecycle problems use
    :class:`DeviceInitializationError` instead.
    """

    def __init__(self, message: str, *, command: str = None):
        super().__init__(message)
        self.message = message
        self.command = command


class CommandRejected(ProtocolError):
    """The device understood the command and explicitly refused it.

    The command did not execute, so a documented alternative may be tried.
    """

    def __init__(self, message: str, *, command: str = None, reply: str = None):
        super().__init__(message, command=command)
        self.reply = reply


class TransportFailure(ProtocolError):
    """The exchange did not complete, so the command outcome is unknown."""


class CommandTimeout(TransportFailure):
    """No reply arrived in time. The command may still have been executed."""


class UnexpectedReply(ProtocolError):
    """A reply arrived but was empty or structurally invalid.

    Treated as unknown-outcome, not as success: an empty reply is exactly what
    a half-open serial port produces, and accepting it would let a mutating
    command silently appear to have worked.
    """

    def __init__(self, message: str, *, command: str = None, reply: str = None):
        super().__init__(message, command=command)
        self.reply = reply


class UnsupportedOperation(ProtocolError):
    """The selected profile does not implement the requested operation.

    Surfaced during initialization rather than on first use, so that a
    configuration incompatibility -- for example an emission policy needing
    pause/resume against a profile that has neither -- is reported as a
    configuration error instead of a failed emission transition.
    """


class DeviceInitializationError(Exception):
    """A device could not be brought up in the configured mode.

    Deliberately not a :class:`ProtocolError`: it describes the manager's
    lifecycle outcome (port could not be opened, no profile matched, the safe
    state could not be established), not the result of one command. Raising it
    is what replaces silent substitution of a mock driver.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one profile's bounded, read-only probe.

    ``positive`` and ``negative`` name the signals observed, and ``evidence``
    carries ``(label, detail)`` pairs worth logging in the device fingerprint.
    Probe failures are expected during discovery and are debug information,
    not warnings.
    """

    profile_id: str
    positive: tuple = ()
    negative: tuple = ()
    evidence: tuple = ()

    @property
    def matched(self) -> bool:
        """Whether this profile saw positive evidence and no contradiction."""
        return bool(self.positive) and not self.negative


@runtime_checkable
class ProtocolProfile(Protocol):
    """Typing-only selection contract shared by every vendor profile.

    Only the members that profile *selection* needs are pinned here. The
    device operations themselves stay vendor-specific and duck-typed while
    there are few profiles.

    ``auto_selectable`` is ``False`` for a profile that differs from another
    only in mutating commands, which cannot be probed safely. Such a profile
    must be configured explicitly rather than guessed.
    """

    profile_id: str
    auto_selectable: bool

    def probe(self, connection) -> ProbeResult:
        ...


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
