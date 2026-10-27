"""One :class:`ImageWindow` per result, and the Window menu that manages them.

This is window mode's counterpart of the embedded viewer: where the napari view
has one canvas that is repainted for whichever result is current, the manager
keeps a window for every result that has been shown and brings the right one
forward. The result list stays the owner of *which results exist*; the manager
only follows it (``show`` when a result becomes current, ``retainOnly`` when
the list changes).

Two rules keep list and windows from fighting:

- A window the *program* raises does not report itself as activated by the
  user (``_programmatic``), and a window that is already the active one never
  reports again -- so "select a row -> raise its window -> window says it was
  activated -> select the row" ends after the first step.
- Closing a window only hides it; removing the result from the list is what
  disposes of it.
"""

from __future__ import annotations

import math
from typing import Callable

from qtpy import QtCore, QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.model.result import result_kind

from .image_window import ImageWindow

#: Result kinds that have no image to put in a window (they live in the
#: Results table / Graph panel / point-cloud display).
_NO_IMAGE_KINDS = ("curve", "table", "localization")


def has_window_image(result) -> bool:
    """Whether ``result`` has anything an image window can draw."""
    if result is None:
        return False
    if hasattr(result, "display_layers") and result.display_layers():
        return True
    return result_kind(result) not in _NO_IMAGE_KINDS


def tile_geometry(count: int, area: QtCore.QRect) -> list[QtCore.QRect]:
    """Equal cells filling ``area``: as square a grid as ``count`` windows allow."""
    if count <= 0:
        return []
    columns = math.ceil(math.sqrt(count))
    rows = math.ceil(count / columns)
    cell_w, cell_h = area.width() // columns, area.height() // rows
    cells = []
    for index in range(count):
        row, column = divmod(index, columns)
        cells.append(QtCore.QRect(
            area.left() + column * cell_w, area.top() + row * cell_h, cell_w, cell_h,
        ))
    return cells


def cascade_geometry(sizes: list[QtCore.QSize], area: QtCore.QRect, step: int) -> list[QtCore.QRect]:
    """Windows of the given sizes, each ``step`` px down-right of the last.

    A size is clamped to ``area``. The offset wraps back to the corner once the
    next window would no longer fit, so a long list never walks off the screen.
    """
    step = max(1, int(step))
    placed = []
    for index, size in enumerate(sizes):
        width, height = min(size.width(), area.width()), min(size.height(), area.height())
        fit = min((area.width() - width) // step, (area.height() - height) // step) + 1
        offset = (index % max(1, fit)) * step
        placed.append(QtCore.QRect(area.left() + offset, area.top() + offset, width, height))
    return placed


class ResultWindowManager(QtCore.QObject):
    """Creates, finds, raises and disposes the windows of the shown results."""

    #: A window became active by the user's doing (click, Window menu): the
    #: result list should follow. Carries the window's result.
    sigWindowActivated = QtCore.Signal(object)
    #: The set of open windows (or which is active) changed.
    sigWindowsChanged = QtCore.Signal()

    def __init__(self, parentWidget: QtWidgets.QWidget | None = None, *,
                 statusMessage: Callable[[str], None] | None = None,
                 windowFactory: Callable = ImageWindow):
        super().__init__(parentWidget)
        self._logger = initLogger(self, tryInheritParent=False)
        self._parentWidget = parentWidget
        self._status = statusMessage or (lambda _message: None)
        self._factory = windowFactory
        self._windows: dict[int, ImageWindow] = {}
        self._active: ImageWindow | None = None
        self._programmatic = False
        self._menu: QtWidgets.QMenu | None = None

    # ------------------------------------------------------------------
    # lookup
    # ------------------------------------------------------------------

    def windows(self) -> list[ImageWindow]:
        return list(self._windows.values())

    def openWindows(self) -> list[ImageWindow]:
        return [window for window in self._windows.values() if window.isVisible()]

    def windowFor(self, result) -> ImageWindow | None:
        return self._windows.get(id(result)) if result is not None else None

    def activeWindow(self) -> ImageWindow | None:
        """The window the toolbar acts on: the active one, if it is open."""
        active = self._active
        return active if active is not None and active.isVisible() else None

    # ------------------------------------------------------------------
    # showing
    # ------------------------------------------------------------------

    def show(self, result, *, activate: bool = True) -> ImageWindow | None:
        """Show ``result``'s window (created on first use) and make it the active one.

        ``activate=False`` is for a caller that is following a window the user
        already activated: it records the window as active but does not raise
        it again.
        """
        if result is None:
            return None
        if not has_window_image(result):
            self._status(
                f"{getattr(result, 'name', 'This result')!s} ({result_kind(result)}) has no "
                "image to show in a window; it is listed in the Results table / Graph panel"
            )
            return None
        window = self._windows.get(id(result))
        if window is None:
            window = self._factory(result)
            self._windows[id(result)] = window
            window.sigActivated.connect(lambda w=window: self._onActivated(w))
            window.sigClosed.connect(lambda w=window: self._onClosed(w))
        self._active = window
        if activate or window.isHidden():
            self._programmatic = True
            try:
                window.activate()
            finally:
                self._programmatic = False
        self.sigWindowsChanged.emit()
        return window

    def replace(self, old, new) -> bool:
        """Point ``old``'s window at ``new`` (a live update, an in-place refinement)."""
        window = self._windows.pop(id(old), None)
        if window is None:
            return False
        self._windows[id(new)] = window
        if not window.tryFastLiveUpdate(new, None):
            window.setResult(new)
        window.setWindowTitle(str(getattr(new, "name", "") or "Result"))
        self.sigWindowsChanged.emit()
        return True

    def _onActivated(self, window: ImageWindow) -> None:
        if self._programmatic or window is self._active:
            return
        self._active = window
        self.sigWindowsChanged.emit()
        self.sigWindowActivated.emit(window.result)

    def _onClosed(self, window: ImageWindow) -> None:
        if self._active is window:
            self._active = None
        self.sigWindowsChanged.emit()

    # ------------------------------------------------------------------
    # disposing
    # ------------------------------------------------------------------

    def retainOnly(self, results) -> None:
        """Dispose of the windows of results that left the list."""
        keep = {id(result) for result in results}
        for key in [key for key in self._windows if key not in keep]:
            window = self._windows.pop(key)
            if self._active is window:
                self._active = None
            window.dispose()
        self.sigWindowsChanged.emit()

    def closeAll(self) -> None:
        for window in list(self._windows.values()):
            window.dispose()
        self._windows.clear()
        self._active = None

    # ------------------------------------------------------------------
    # Window menu
    # ------------------------------------------------------------------

    def setWindowMenu(self, menu: QtWidgets.QMenu) -> None:
        """Fill ``menu`` with Tile, Cascade and the open windows, whenever it opens."""
        self._menu = menu
        menu.aboutToShow.connect(self.rebuildWindowMenu)
        self.rebuildWindowMenu()

    def rebuildWindowMenu(self) -> None:
        menu = self._menu
        if menu is None:
            return
        menu.clear()
        open_windows = self.openWindows()
        tile = menu.addAction("Tile")
        tile.setEnabled(bool(open_windows))
        tile.triggered.connect(lambda _checked=False: self.tile())
        cascade = menu.addAction("Cascade")
        cascade.setEnabled(bool(open_windows))
        cascade.triggered.connect(lambda _checked=False: self.cascade())
        menu.addSeparator()
        if not open_windows:
            empty = menu.addAction("(no image windows open)")
            empty.setEnabled(False)
        for window in open_windows:
            action = menu.addAction(window.windowTitle())
            action.setCheckable(True)
            action.setChecked(window is self._active)
            action.triggered.connect(lambda _checked=False, w=window: self.raiseWindow(w))

    def raiseWindow(self, window: ImageWindow) -> None:
        """Bring a window forward as the user's choice (the list follows)."""
        was_active = window is self._active
        self._active = window
        self._programmatic = True
        try:
            window.activate()
        finally:
            self._programmatic = False
        self.sigWindowsChanged.emit()
        if not was_active:
            self.sigWindowActivated.emit(window.result)

    def availableArea(self) -> QtCore.QRect:
        """The usable screen area of the main window's screen (else the primary one)."""
        screen = None
        if self._parentWidget is not None:
            screen = self._parentWidget.screen()
        if screen is None:
            screen = QtWidgets.QApplication.primaryScreen()
        return screen.availableGeometry()

    @staticmethod
    def _frameMargins(window: ImageWindow) -> QtCore.QSize:
        """Title bar + borders: what the window manager adds around the client area."""
        frame, client = window.frameGeometry(), window.geometry()
        return QtCore.QSize(frame.width() - client.width(), frame.height() - client.height())

    def tile(self) -> None:
        windows = self.openWindows()
        for window, cell in zip(windows, tile_geometry(len(windows), self.availableArea())):
            margins = self._frameMargins(window)
            window.setGeometry(
                cell.left(), cell.top(),
                max(1, cell.width() - margins.width()), max(1, cell.height() - margins.height()),
            )

    def cascade(self) -> None:
        windows = self.openWindows()
        if not windows:
            return
        # The title bar height is the natural offset: each window's title stays visible.
        margins = self._frameMargins(windows[0])
        step = margins.height() or QtWidgets.QApplication.style().pixelMetric(
            QtWidgets.QStyle.PM_TitleBarHeight
        )
        area = self.availableArea()
        rects = cascade_geometry([w.size() for w in windows], area, step)
        for window, rect in zip(windows, rects):
            window.setGeometry(rect)
            window.raise_()


__all__ = ["ResultWindowManager", "cascade_geometry", "has_window_image", "tile_geometry"]
