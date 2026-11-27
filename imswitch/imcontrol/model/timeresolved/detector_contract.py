"""Mixin contract for detector managers that expose time-resolved products."""

from __future__ import annotations

from .types import TimeResolvedScanConfig, TimeResolvedScanProducts


class TimeResolvedDetectorMixin:
    """Optional detector-manager contract for TCSPC/time-gated products.

    Product capture is a *session* with one owner. ``configure`` opens it
    and returns a session token; while it is open, another ``configure``
    is refused (``RuntimeError``), so two workflows cannot reconfigure the
    same detector under each other, and ``clear`` with a token other than
    the session's is a no-op, so a second run's cleanup cannot erase the
    first run's state. Calls without a token keep the historical behaviour
    for single-consumer scripts: they open an unowned session anyone may
    clear.
    """

    def timeResolvedCapabilities(self) -> dict:
        raise NotImplementedError

    def configureTimeResolvedProducts(
        self,
        config: TimeResolvedScanConfig,
        owner: str | None = None,
    ) -> str | None:
        """Arm product capture for the next scan; returns the session token."""
        raise NotImplementedError

    def waitForFinalTimeResolvedProducts(
        self,
        timeout_s: float | None = None,
        owner: str | None = None,
    ) -> TimeResolvedScanProducts:
        raise NotImplementedError

    def getLastTimeResolvedProducts(
        self,
        *,
        copy: bool = True,
    ) -> TimeResolvedScanProducts | None:
        raise NotImplementedError

    def clearTimeResolvedProducts(self, owner: str | None = None) -> None:
        """Disarm capture. With a token, only the session's owner succeeds."""
        raise NotImplementedError

    def timeResolvedSessionOwner(self) -> str | None:
        """The token of the open session, ``None`` when none is open."""
        raise NotImplementedError
