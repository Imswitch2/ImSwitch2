import pyqtgraph.console
from qtpy import QtWidgets

from .basewidgets import Widget


class ConsoleWidget(Widget):
    # pyqtgraph's console already scrolls, and it is meant to fill the
    # panel rather than sit at its preferred height inside a scroll area.
    scrollablePanel = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # No minimum height of its own: docks stack vertically and a
        # splitter's minimum is the sum of its children's, so a panel that
        # insists on 180 px makes the window that much taller to open --
        # and a few of them together make it taller than the screen, at
        # which point Qt keeps the window at its minimum and the bottom is
        # cut off. pyqtgraph's console scrolls by itself.
        self.setMinimumSize(0, 0)

        self.pgWidget = pyqtgraph.console.ConsoleWidget()

        layout = QtWidgets.QGridLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.pgWidget)
        self.setLayout(layout)
