"""How much room a docked panel demands, and how much it is given.

Docks stack vertically in a pyqtgraph splitter.  A splitter never shrinks
below the sum of its children's minimum heights and it clips rather than
scrolls, so two things have to hold for the window to fit a laptop screen
with every control still reachable:

* a panel must be able to shrink (it scrolls its own contents), and
* the space it *opens* with must come from how tall its contents are, not
  from refusing to shrink.

Before this, both were broken at once, which is why ImSwitch opened with its
bottom edge off-screen until it was maximised and restored, and why dragging
one dock rearranged all the others.
"""
from types import SimpleNamespace

import pytest
from pyqtgraph.dockarea import Dock
from qtpy import QtCore, QtWidgets

from imswitch.imcontrol.view.ImConMainView import (
    ImConMainView, _MAX_DOCK_STRETCH, _MIN_DOCK_STRETCH, _dockTitleHeight,
)
from imswitch.imcontrol.view.widgets.basewidgets import (
    PANEL_MINIMUM_HEIGHT, Widget, WidgetFactory,
)
# Through the package, not the submodules: importing
# ``...view.widgets.FlipMirrorWidget`` binds the *module* onto the package and
# shadows the lazily exported class of the same name for everyone who imports
# it afterwards (test_flip_mirrors asserts on exactly that).
from imswitch.imcontrol.view.widgets import (
    ConsoleWidget, FlipMirrorWidget, TriggerScopeScanWidget, ViewWidget,
    ViewerToolsWidget,
)


pytestmark = pytest.mark.nohardware


SCROLLABLE_PANELS = [ViewWidget, ViewerToolsWidget, FlipMirrorWidget, TriggerScopeScanWidget]


@pytest.fixture
def factory():
    return WidgetFactory(SimpleNamespace())


@pytest.mark.parametrize('widgetClass', SCROLLABLE_PANELS, ids=lambda c: c.__name__)
def test_factory_built_panels_can_shrink(widgetClass, factory, qtbot):
    widget = factory.createWidget(widgetClass)
    qtbot.addWidget(widget)

    assert widget.minimumSizeHint().height() <= PANEL_MINIMUM_HEIGHT, (
        f'{widgetClass.__name__} insists on '
        f'{widget.minimumSizeHint().height()} px of height; that is added to '
        f'the window minimum and pushes its bottom edge off the screen'
    )


@pytest.mark.parametrize('widgetClass', SCROLLABLE_PANELS, ids=lambda c: c.__name__)
def test_shrinking_a_panel_does_not_shrink_what_it_asks_to_open_with(
    widgetClass, factory, qtbot
):
    """The scroll wrapper must not cost the panel its place in the layout."""
    bare = widgetClass(SimpleNamespace())
    qtbot.addWidget(bare)
    wrapped = factory.createWidget(widgetClass)
    qtbot.addWidget(wrapped)

    # The wrapper's own margins are the only difference allowed.
    assert abs(wrapped.panelContentSizeHint().height()
               - bare.sizeHint().height()) <= 8


def test_panels_that_scroll_themselves_are_left_alone(factory, qtbot):
    widget = factory.createWidget(ConsoleWidget)
    qtbot.addWidget(widget)

    assert ConsoleWidget.scrollablePanel is False
    assert widget.panelContentSizeHint() == widget.sizeHint()


def test_makeScrollable_is_idempotent(factory, qtbot):
    widget = factory.createWidget(ViewWidget)
    qtbot.addWidget(widget)
    first = widget.makeScrollable()

    assert widget.makeScrollable() is first


class _PanelOfHeight(QtWidgets.QWidget):
    def __init__(self, height):
        super().__init__()
        self._height = height

    def panelContentSizeHint(self):
        return QtCore.QSize(120, self._height)


def _dockSizingStub(panelHeights):
    """A stand-in for the main view carrying just what the sizing pass reads."""
    docks, widgets = {}, {}
    for key, height in panelHeights.items():
        docks[key] = Dock(key, size=(1, 1))
        widgets[key] = _PanelOfHeight(height)
    return SimpleNamespace(docks=docks, widgets=widgets)


def test_dock_stretch_follows_panel_height(qtbot):
    stub = _dockSizingStub({'Tall': 400, 'Short': 80})
    for dock in stub.docks.values():
        qtbot.addWidget(dock)

    ImConMainView.applyContentAwareDockSizing(stub)

    tall = stub.docks['Tall'].stretch()[1]
    short = stub.docks['Short'].stretch()[1]
    assert tall > short, (
        'every dock is created with the same stretch, so without this pass a '
        'one-row panel is handed exactly as much height as a thirty-row one'
    )


def test_dock_stretch_is_bounded_at_both_ends(qtbot):
    stub = _dockSizingStub({'Huge': 4000, 'Tiny': 4})
    for dock in stub.docks.values():
        qtbot.addWidget(dock)

    ImConMainView.applyContentAwareDockSizing(stub)

    huge, tiny = stub.docks['Huge'], stub.docks['Tiny']
    assert huge.stretch()[1] == _MAX_DOCK_STRETCH + _dockTitleHeight(huge)
    assert tiny.stretch()[1] == _MIN_DOCK_STRETCH + _dockTitleHeight(tiny)


def test_dock_stretch_includes_the_title_bar(qtbot):
    """The splitter divides the column between whole docks, title bar included.

    A dock asking for exactly its panel's height opens one title bar short of
    it -- enough to cut the last row off every panel at once.
    """
    stub = _dockSizingStub({'Panel': 200})
    dock = stub.docks['Panel']
    qtbot.addWidget(dock)

    ImConMainView.applyContentAwareDockSizing(stub)

    assert _dockTitleHeight(dock) > 0
    assert dock.stretch()[1] == 200 + _dockTitleHeight(dock)


def test_image_dock_outweighs_the_panel_columns(qtbot):
    stub = _dockSizingStub({'Image': 200, 'Settings': 300})
    for dock in stub.docks.values():
        qtbot.addWidget(dock)

    ImConMainView.applyContentAwareDockSizing(stub)

    assert (stub.docks['Image'].stretch()[0]
            > stub.docks['Settings'].stretch()[0]), (
        'the viewer, not the parameter forms, gets the leftover width'
    )


def test_every_docked_panel_class_is_a_scrollable_widget():
    """A new panel must not be able to opt out of shrinking by accident."""
    assert Widget.scrollablePanel is True
