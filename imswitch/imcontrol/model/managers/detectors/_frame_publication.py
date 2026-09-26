"""Pair a scan-driven detector's display storage with its scan's geometry.

A point-scan frame is published from several places -- the throttled mid-scan
preview, the frame-boundary latest frame, and (while a recording or BeadRec
consumes frames) a copy made on the consumer's thread and latched. For the
viewer to map a drawn rectangle back to the scanners, each published object
must carry the geometry of the scan whose pixels it holds
(docs/simple-point-scan-plan.md, D1). That holds by construction when:

* a scan that carries a geometry gets **fresh** display storage, so pixels
  already published for the previous scan are never rewritten, and
* the storage and its geometry are swapped together, and a reader on another
  thread can tell whether it read both from the same swap.

These helpers keep that pairing for APD, PMT and Time Tagger managers. They
are plain functions over the manager's ``__dict__`` so no manager changes its
class hierarchy. A scan without a geometry leaves nothing to pair: every
helper then returns exactly what the manager returned before.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

import numpy as np

from imswitch.imcontrol.model.scan_frame import (
    FrameGeometry,
    frame_geometry_from_scan_info,
    with_frame_geometry,
)

_PUBLICATION = '_framePublication'
_PENDING = '_pendingFrameGeometry'


class _Publication:
    """One storage generation's geometry. Compared by identity."""

    __slots__ = ('geometry',)

    def __init__(self, geometry: FrameGeometry):
        self.geometry = geometry


def remember_scan_geometry(manager: Any, scanInfoDict) -> Optional[FrameGeometry]:
    """Note the geometry of the scan being prepared, for the storage it gets.

    Copied now: the controller reuses one ``scanInfoDict`` across repeat
    frames and the coordinator strips its own keys at completion.
    """
    geometry = frame_geometry_from_scan_info(scanInfoDict)
    manager.__dict__[_PENDING] = geometry
    return geometry


def pending_scan_geometry(manager: Any) -> Optional[FrameGeometry]:
    return manager.__dict__.get(_PENDING)


def begin_frame_storage(manager: Any) -> None:
    """Call before replacing display storage: nothing is paired meanwhile."""
    manager.__dict__[_PUBLICATION] = None


def commit_frame_storage(manager: Any, geometry: Optional[FrameGeometry]) -> None:
    """Call after the new storage is in place."""
    manager.__dict__[_PUBLICATION] = (
        _Publication(geometry) if geometry is not None else None
    )


def current_frame_geometry(manager: Any) -> Optional[FrameGeometry]:
    """Geometry of the storage in place now (for the manager's own thread)."""
    publication = manager.__dict__.get(_PUBLICATION)
    return publication.geometry if publication is not None else None


def read_with_frame_geometry(manager: Any, read: Callable[[], Any]) -> Tuple[Any, Optional[FrameGeometry]]:
    """``read()`` and the geometry of the storage it read, from any thread.

    The storage swap is ``begin`` -> replace arrays -> ``commit``. If the same
    publication object is in place before and after ``read``, no swap began
    in between, so what was read belongs to it. Otherwise the geometry is
    withheld (``None``): a frame without geometry is merely not drawable, a
    frame with the wrong one would be wrong.
    """
    before = manager.__dict__.get(_PUBLICATION)
    value = read()
    after = manager.__dict__.get(_PUBLICATION)
    if before is None or before is not after:
        return value, None
    return value, before.geometry


def _on_own_thread(manager: Any) -> bool:
    try:
        from qtpy import QtCore
        return QtCore.QThread.currentThread() == manager.thread()
    except Exception:
        return True


def display_frame(manager: Any, frame: Any, geometry: Optional[FrameGeometry]) -> Any:
    """``frame`` as published for display, carrying ``geometry``.

    Off the manager's own thread (the live-view worker's queued path) the
    pixels are snapshotted, since the frame may reach the layer after more of
    the same scan has been written. Without a geometry the frame is returned
    untouched.
    """
    if geometry is None:
        return frame
    if not _on_own_thread(manager):
        frame = np.array(frame, copy=True)
    return with_frame_geometry(frame, geometry)


__all__ = [
    'begin_frame_storage',
    'commit_frame_storage',
    'current_frame_geometry',
    'display_frame',
    'pending_scan_geometry',
    'read_with_frame_geometry',
    'remember_scan_geometry',
]
