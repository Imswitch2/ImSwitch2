import os
import weakref
from abc import ABCMeta, abstractmethod

from qtpy import QtCore, QtWidgets


class _QObjectABCMeta(type(QtCore.QObject), ABCMeta):
    pass


#: How short a scrolled panel is allowed to get: about one row of controls
#: plus the scrollbar's arrows, so a squeezed panel still shows something to
#: scroll rather than collapsing to nothing.
PANEL_MINIMUM_HEIGHT = 48


class _PanelScrollArea(QtWidgets.QScrollArea):
    """ The scroll area :meth:`Widget.makeScrollable` wraps a panel in.

    Qt's own minimum for a scroll area is large enough (both scrollbars plus
    the frame, ~76 px here) that a column of ten panels still cannot be made
    to fit a laptop screen.  Since the whole point of the wrapper is that the
    panel may be given less room than it wants, it reports its own floor.
    """

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setHeight(min(hint.height(), PANEL_MINIMUM_HEIGHT))
        return hint

    def sizeHint(self):
        """ Ask for as much room as the panel inside would have asked for.

        Qt's own hint for a resizable scroll area is a constant that has
        nothing to do with its contents, which makes the wrapper anything but
        transparent: a panel that caps itself at its own preferred height with
        ``QSizePolicy.Maximum`` -- Flip Mirrors, Stand and Setup Modes all do
        -- ends up capped at that constant instead, a few dozen pixels, no
        matter how much room its dock has.
        """
        content = self.widget()
        if content is None:
            return super().sizeHint()

        hint = content.sizeHint()
        frame = 2 * self.frameWidth()
        width = hint.width() + frame
        if self.verticalScrollBarPolicy() != QtCore.Qt.ScrollBarAlwaysOff:
            # Room for the scrollbar that shows up as soon as it is needed,
            # so its arrival does not clip the content sideways as well.
            width += self.verticalScrollBar().sizeHint().width()
        return QtCore.QSize(width, hint.height() + frame)

    def eventFilter(self, source, event):
        """ Keep the layout above from caching a hint from before the panel filled up.

        A panel is wrapped while it is still empty -- its controller adds the
        rows afterwards -- and ``sizeHint()`` above is only consulted again if
        something invalidates it.  Without this the panel keeps asking for the
        height it wanted with nothing in it.
        """
        if source is self.widget() and event.type() == QtCore.QEvent.LayoutRequest:
            self.updateGeometry()
        return super().eventFilter(source, event)


class WidgetFactory:
    """ Factory class for creating widgets. """

    def __init__(self, options):
        self._options = options
        self._baseKwargs = {}
        self._createdWidgets = []

    def createWidget(self, widgetClass, *args, **extraKwargs):
        kwargs = self._baseKwargs.copy()
        kwargs.update(extraKwargs)

        if issubclass(widgetClass, Widget):
            widget = widgetClass(self._options, *args, **kwargs)
            if widget.scrollablePanel and QtWidgets.QWidget.layout(widget) is not None:
                widget.makeScrollable()
        else:
            widget = widgetClass(*args, **kwargs)

        self._createdWidgets.append(weakref.ref(widget))
        return widget

    def setArgument(self, name, value):
        self._baseKwargs[name] = value


class Widget(QtWidgets.QWidget, metaclass=_QObjectABCMeta):
    """ Superclass for all Widgets. All Widgets are subclasses of QWidget. """

    sigKeyReleased = QtCore.Signal(object)

    #: Whether WidgetFactory should move this panel's contents into a scroll
    #: area once it has been built -- see :meth:`makeScrollable`. Turn it off
    #: only for panels whose content is a single widget that scrolls itself
    #: *and* is meant to take all the room it is given (a console, a viewer);
    #: a parameter form always wants this on, including one that already has
    #: an inner scroll area, because the wrapper is what lets the panel as a
    #: whole shrink.
    scrollablePanel = True

    _panelScrollArea = None
    _panelScrollContent = None

    @abstractmethod
    def __init__(self, options, *_args, **_kwargs):
        self._options = options
        QtWidgets.QWidget.__init__(self)

    def keyReleaseEvent(self, event):
        self.sigKeyReleased.emit(event)
        super().keyReleaseEvent(event)

    def makeScrollable(self, horizontal=True):
        """ Move everything this panel has laid out into an internal scroll area.

        Docks stack vertically inside a splitter, and a splitter's minimum
        height is the sum of its children's.  A panel that cannot shrink
        therefore adds its whole height to the smallest size the window can
        take; a handful of them together push that minimum past the screen,
        at which point Qt holds the window at the minimum and the bottom is
        off-screen.  Worse, a splitter clips rather than scrolls, so whatever
        sits at the bottom of a squeezed panel simply disappears.

        A panel that scrolls its own contents contributes almost nothing to
        that sum and shows a scrollbar instead of losing its last row.  How
        much space each panel *opens* with is then decided by the dock stretch
        factors in ``ImConMainView``, which are derived from content size --
        not, as before, by panels refusing to shrink.

        Call this once, after the panel's layout is fully populated (the
        layout may still gain rows afterwards; only the layout object itself
        has to exist).
        """
        if getattr(self, '_panelScrollArea', None) is not None:
            return self._panelScrollArea

        innerLayout = QtWidgets.QWidget.layout(self)
        if innerLayout is None:
            raise RuntimeError(
                f'{type(self).__name__}.makeScrollable() needs a layout to wrap;'
                f' call it after setLayout()'
            )

        # setLayout() on a fresh widget steals the layout from its current
        # parent and reparents every child widget along with it.
        content = QtWidgets.QWidget()
        content.setObjectName('panelScrollContent')
        content.setLayout(innerLayout)

        scrollArea = _PanelScrollArea()
        scrollArea.setObjectName('panelScrollArea')
        scrollArea.setFrameShape(QtWidgets.QFrame.NoFrame)
        scrollArea.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarAsNeeded if horizontal else QtCore.Qt.ScrollBarAlwaysOff
        )
        scrollArea.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        scrollArea.setWidgetResizable(True)
        scrollArea.setWidget(content)
        scrollArea.setMinimumSize(0, 0)
        # setWidget() installs this already; repeating it is idempotent and
        # says out loud that _PanelScrollArea.eventFilter needs it.
        content.installEventFilter(scrollArea)

        outerLayout = QtWidgets.QVBoxLayout()
        outerLayout.setContentsMargins(0, 0, 0, 0)
        outerLayout.setSpacing(0)
        outerLayout.addWidget(scrollArea)
        self.setLayout(outerLayout)

        self.setMinimumSize(0, 0)
        self._panelScrollArea = scrollArea
        self._panelScrollContent = content
        return scrollArea

    def panelContentSizeHint(self):
        """ The height this panel would like to open with, scroll area or not.

        ``sizeHint()`` of a scrolled panel is the scroll area's, which says
        nothing about the form inside it.  ``ImConMainView`` sizes docks from
        this instead so that wrapping a panel in a scroll area does not change
        how much room it gets on startup.
        """
        content = getattr(self, '_panelScrollContent', None)
        if content is not None:
            return content.sizeHint()
        return self.sizeHint()

    def replaceWithError(self, errorText):
        errorLabel = QtWidgets.QLabel(errorText)

        grid = QtWidgets.QGridLayout()
        existingLayout = QtWidgets.QWidget.layout(self)
        if existingLayout is not None:
            QtWidgets.QWidget().setLayout(existingLayout)  # unset layout
        self._panelScrollArea = None
        self._panelScrollContent = None
        self.setLayout(grid)
        grid.addWidget(errorLabel)


class NapariHybridWidget(Widget, metaclass=_QObjectABCMeta):
    """ Superclass for widgets that can use the functionality of
    NapariBaseWidget. Derived classes should not implement __init__; instead,
    they should implement __post_init__. """

    def __init__(self, options, *, napariViewer=None):
        Widget.__init__(self, options)

        if napariViewer is None:
            if 'IMSWITCH_FULL_APP' not in os.environ or os.environ['IMSWITCH_FULL_APP'] != '1':
                # ImSwitch components running as napari plugins - raise error
                raise ValueError('napariViewer must be specified')

            self.replaceWithError('Could not load widget; napariViewer not specified. This error is'
                                  ' most likely happening because this widget requires the image'
                                  ' display widget to also be enabled, but it is not enabled in'
                                  ' your currently active hardware configuration.')
            return

        self.viewer = napariViewer
        self.__post_init__()

    def addItemToViewer(self, item):
        """Add an overlay item to the backing napari viewer."""
        from imswitch.imcommon.view.guitools import naparitools

        return naparitools.NapariBaseWidget.addItemToViewer(self, item)

    @abstractmethod
    def __post_init__(self):
        pass


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
