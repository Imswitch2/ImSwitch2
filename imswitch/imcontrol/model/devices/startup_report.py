"""What did not start: the one list the user sees at startup and in the log.

Pure functions over the supervisor's hardware statuses, so the log line, the
window notice and the tests all say the same thing.
"""
from __future__ import annotations

from dataclasses import dataclass

from .status import DeviceConnectionState, DeviceRuntimeMode


@dataclass(frozen=True)
class StartupIssue:
    """One physical device that is not working as the setup declared it."""

    name: str
    category: str
    #: ``"not connected"`` (absent or failed, nothing stands in for it) or
    #: ``"mock"`` (the real device failed and a simulation stands in for it).
    state: str
    summary: str
    details: str | None = None

    @property
    def line(self) -> str:
        text = f"{self.name} ({self.category}): {self.state}"
        if self.summary:
            text += f" - {self.summary}"
        details = " ".join((self.details or "").split())   # one line, whatever the error said
        if details and details not in text:
            text += f" [{details[:240]}{'...' if len(details) > 240 else ''}]"
        return text


def startupIssues(statuses) -> tuple[StartupIssue, ...]:
    """The devices among ``statuses`` (``HardwareStatus`` objects) that failed.

    Left out: a configured mock (the setup asked for it) and a transient
    device that is absent by declaration -- neither is a failure.
    """
    issues = []
    for status in statuses:
        mode = status.mode
        connection = status.connection
        failed = connection in (
            DeviceConnectionState.ERROR, DeviceConnectionState.DISCONNECTED)
        if mode is DeviceRuntimeMode.ABSENT or not failed:
            continue
        issues.append(StartupIssue(
            name=status.name,
            category=str(getattr(status.category, "value", status.category)),
            state="mock" if mode is DeviceRuntimeMode.MOCK else "not connected",
            summary=status.summary or "",
            details=status.details,
        ))
    return tuple(sorted(issues, key=lambda issue: (issue.state, issue.name.casefold())))


def formatStartupReport(issues) -> str:
    """The multi-line text for the log and the window's tooltip."""
    return "\n".join(issue.line for issue in issues)


def startupNoticeText(issues) -> str:
    """The one-line notice for the window; empty when nothing failed."""
    if not issues:
        return ""
    absent = [i for i in issues if i.state == "not connected"]
    mocked = [i for i in issues if i.state == "mock"]
    parts = []
    if absent:
        parts.append(f"{len(absent)} device{'s' if len(absent) != 1 else ''} not connected")
    if mocked:
        parts.append(
            f"{len(mocked)} running on a MOCK instead of the real device"
            if len(mocked) != 1 else "1 running on a MOCK instead of the real device")
    return "Hardware: " + ", ".join(parts) + ". Reconnect from Hardware → Hardware status."
