"""Mixin contract for detector managers that expose time-resolved products."""

from __future__ import annotations

from .types import TimeResolvedScanConfig, TimeResolvedScanProducts


class TimeResolvedDetectorMixin:
    """Optional detector-manager contract for TCSPC/time-gated products."""

    def timeResolvedCapabilities(self) -> dict:
        raise NotImplementedError

    def configureTimeResolvedProducts(self, config: TimeResolvedScanConfig) -> None:
        raise NotImplementedError

    def waitForFinalTimeResolvedProducts(
        self,
        timeout_s: float | None = None,
    ) -> TimeResolvedScanProducts:
        raise NotImplementedError

    def getLastTimeResolvedProducts(
        self,
        *,
        copy: bool = True,
    ) -> TimeResolvedScanProducts | None:
        raise NotImplementedError

    def clearTimeResolvedProducts(self) -> None:
        raise NotImplementedError
