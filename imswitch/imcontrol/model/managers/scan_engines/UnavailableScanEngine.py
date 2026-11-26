"""Stands in for a scan engine that could not be built.

A scan engine whose board or generator is missing used to raise at startup and
take the whole application down with it. The setup loader now keeps the engine's
place with this object: the application starts, the failure is reported, and a
scan that needs the engine is refused by name, with the reason, instead of
crashing or running on the wrong engine.
"""

from __future__ import annotations

from typing import Iterable

from imswitch.imcontrol.model.errors import ScanDesignRefusedError
from imswitch.imcontrol.model.scan_program.engine import (
    IMPOSSIBLE, EngineCapabilities, FeasibilityReport, ScanEngine, ScanEngineRefused,
)


class UnavailableScanEngine(ScanEngine):
    """An engine that never started. It owns the channels its setup entry
    names (so a program for its devices is refused here, not run elsewhere),
    declares no capability and refuses every run with ``reason``."""

    unavailable = True

    def __init__(self, name: str, reason: str, channels: Iterable[str] = ()):
        self.name = name
        self.reason = str(reason)
        self._channels = frozenset(str(c) for c in channels)

    @property
    def message(self) -> str:
        return (f'The scan engine {self.name!r} did not start ({self.reason}); '
                'nothing that needs it can run. Fix it and restart ImSwitch.')

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(name=self.name, abort='none')

    def owns(self, channel: str) -> bool:
        channel = str(channel)
        return channel in self._channels or channel.partition('/')[0] == self.name

    def assess(self, program, channels) -> FeasibilityReport:
        report = FeasibilityReport(self.name)
        report.add(f'engine {self.name}', IMPOSSIBLE, self.message)
        return report

    def compile(self, program, channels, captured_um=None, devices=None):
        raise ScanEngineRefused(self.assess(program, channels))

    def run_signals(self, signalDict, scanInfoDict):
        raise ScanDesignRefusedError(self.message)

    def prepare(self, compiled):
        raise ScanDesignRefusedError(self.message)

    def run(self, compiled=None, blocking=False):
        raise ScanDesignRefusedError(self.message)

    def abort(self) -> str:
        return 'none'

    def finalize(self):
        return True
