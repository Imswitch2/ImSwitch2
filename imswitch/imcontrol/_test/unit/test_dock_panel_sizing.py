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
    ImConMainView, _LayoutPreservingDockArea, _MAX_DOCK_STRETCH,
    _MIN_DOCK_STRETCH, _dockTitleHeight,
)
from imswitch.imcontrol.view.widgets.basewidgets import (
    PANEL_MINIMUM_HEIGHT, Widget, WidgetFactory,
)
# Through the package, not the submodules: importing
# ``...view.widgets.FlipMirrorWidget`` binds the *module* onto the package and
# shadows the lazily exported class of the same name for everyone who imports
# it afterwards (test_flip_mirrors asserts on exactly that).
from imswitch.imcontrol.view.widgets import (
    ConsoleWidget, FlipMirrorWidget, LeicaStandWidget, SetupModesWidget,
    TriggerScopeScanWidget, ViewWidget, ViewerToolsWidget,
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


def _inTallContainer(widget, qtbot, height=700):
    """Returns the host -- pytest-qt only weak-refs it, so callers must hold it."""
    host = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget)
    host.setLayout(layout)
    qtbot.addWidget(host)
    host.resize(400, height)
    host.show()
    qtbot.waitExposed(host)
    qtbot.wait(10)
    return host


def test_a_panel_filled_in_after_wrapping_asks_for_its_real_height(factory, qtbot):
    """Controllers add a panel's rows long after WidgetFactory wraps it.

    The wrapper has to stay transparent to sizeHint(), or the layout above
    keeps the hint the panel had while it was still empty -- and a panel that
    caps itself at its own preferred height (QSizePolicy.Maximum, which Flip
    Mirrors, Stand and Setup Modes all use) is then stuck a few dozen pixels
    tall however much room its dock has, with its contents scrolling inside
    that.
    """
    widget = factory.createWidget(FlipMirrorWidget)
    host = _inTallContainer(widget, qtbot)
    emptyHint = widget.sizeHint().height()

    for name in ('Illumination', 'DetectionTilt', 'DetectionPath'):
        widget.addFlipMirror(name, {0: 'Out', 1: 'In'})
    qtbot.wait(10)

    assert widget.sizeHint().height() > emptyHint
    assert widget.height() == widget.panelContentSizeHint().height()
    assert host.height() > widget.height(), 'the container had room to spare'


@pytest.mark.parametrize(
    'widgetClass', [FlipMirrorWidget, LeicaStandWidget, SetupModesWidget],
    ids=lambda c: c.__name__,
)
def test_panels_that_cap_themselves_still_grow_to_their_contents(
    widgetClass, factory, qtbot
):
    """QSizePolicy.Maximum means 'no taller than my contents', not 'tiny'.

    These three are the panels that cap themselves that way. The two that
    build their whole UI in __init__ were never broken -- their contents were
    complete before the wrapper went on -- so this is a guard on all three
    rather than a reproduction; the reproduction is the test above, on the
    panel whose rows arrive from its controller.
    """
    widget = factory.createWidget(widgetClass)
    assert widget.sizePolicy().verticalPolicy() == QtWidgets.QSizePolicy.Maximum, \
        'this test only says anything while the panel caps itself'
    host = _inTallContainer(widget, qtbot)

    assert host.height() > widget.panelContentSizeHint().height(), 'room to spare'
    assert widget.height() >= widget.panelContentSizeHint().height()


def test_capped_panels_can_still_shrink(factory, qtbot):
    """...and the whole point of the wrapper survives: they still give way."""
    widget = factory.createWidget(FlipMirrorWidget)
    for name in ('Illumination', 'DetectionTilt', 'DetectionPath'):
        widget.addFlipMirror(name, {0: 'Out', 1: 'In'})
    host = _inTallContainer(widget, qtbot, height=60)

    assert widget.minimumSizeHint().height() <= PANEL_MINIMUM_HEIGHT
    assert widget.height() <= host.height()


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


# ----------------------------------------------------------------------
# Rearranging one part of the layout must leave the rest of it alone
# ----------------------------------------------------------------------

def _threeColumnArea(qtbot):
    """Two panel columns either side of a wide middle dock, as ImControl has."""
    area = _LayoutPreservingDockArea()
    qtbot.addWidget(area)
    docks = {name: Dock(name, size=(1, 1)) for name in
             ('Left', 'Middle', 'RightTop', 'RightBottom')}
    for dock in docks.values():
        dock.addWidget(QtWidgets.QWidget())
    area.addDock(docks['Left'], 'left')
    area.addDock(docks['Middle'], 'right', docks['Left'])
    area.addDock(docks['RightTop'], 'right', docks['Middle'])
    area.addDock(docks['RightBottom'], 'bottom', docks['RightTop'])
    area.resize(900, 600)
    area.show()
    qtbot.waitExposed(area)
    return area, docks


def test_rearranging_one_column_leaves_the_others_where_they_were(qtbot):
    """The reported bug: moving a dock on the right shrank the panel on the left.

    A column's stretch is the sum of its docks', so any dock move changes it,
    and pyqtgraph answers a descendant's stretch change by re-dividing every
    splitter on the way up from the stretch factors alone -- discarding every
    width the user had dragged, anywhere in the window.
    """
    area, docks = _threeColumnArea(qtbot)
    top = area.topContainer

    # What the user does first: drag the splitter to widen the left column.
    widened = [360, 340, 200]
    top.setSizes(widened)
    qtbot.wait(10)
    before = top.sizes()
    assert before[0] > 300, 'setup: the left column should now be the wide one'

    # ...and then rearranges something on the far side of the window.
    area.moveDock(docks['RightBottom'], 'top', docks['RightTop'])
    qtbot.wait(10)

    assert top.sizes() == before


def test_tabbing_a_column_together_leaves_the_other_columns_alone(qtbot):
    """The second half of the reported bug, and a different code path.

    Dropping one of a column's two docks onto the other leaves the column
    holding a single tab container, so pyqtgraph dissolves the column and puts
    the tab container in its place.  That *is* a child change for the splitter
    holding the columns -- which is why suppressing the stretch-change resize
    alone did not cover it -- but no pane was gained or lost there, so its
    sizes must survive.
    """
    area, docks = _threeColumnArea(qtbot)
    top = area.topContainer
    top.setSizes([360, 340, 200])
    qtbot.wait(10)
    before = top.sizes()

    area.moveDock(docks['RightBottom'], 'above', docks['RightTop'])
    qtbot.wait(10)

    assert docks['RightTop'].container().type() == 'tab'
    assert area.topContainer.sizes() == before


def test_splitting_a_tab_group_leaves_the_other_columns_alone(qtbot):
    """...and the same the other way round, when a tab container dissolves."""
    area, docks = _threeColumnArea(qtbot)
    area.moveDock(docks['RightBottom'], 'above', docks['RightTop'])
    qtbot.wait(10)
    top = area.topContainer
    top.setSizes([360, 340, 200])
    qtbot.wait(10)
    before = top.sizes()

    area.moveDock(docks['RightBottom'], 'bottom', docks['RightTop'])
    qtbot.wait(10)

    assert docks['RightTop'].container().type() == 'vertical'
    assert area.topContainer.sizes() == before


def test_floating_a_dock_leaves_the_other_columns_alone(qtbot):
    """Floating runs pyqtgraph's addDock on the *new* window's area.

    So the area the dock leaves is never the one running, and removing the
    second-to-last dock of a column dissolved that column and re-divided the
    window width behind the user's back. pyqtgraph also hard-codes a stock
    DockArea for the floating window, which would behave like upstream.
    """
    area, docks = _threeColumnArea(qtbot)
    top = area.topContainer
    top.setSizes([360, 340, 200])
    qtbot.wait(10)
    before = top.sizes()

    area.floatDock(docks['RightBottom'])
    qtbot.wait(10)

    try:
        assert [type(a).__name__ for a in area.tempAreas] == \
            ['_LayoutPreservingDockArea']
        assert area.topContainer.sizes() == before
    finally:
        for temp in list(area.tempAreas):
            temp.win.close()


def test_docking_a_floated_dock_back_leaves_the_other_columns_alone(qtbot):
    """The return trip, which tears the temporary area down as it goes."""
    area, docks = _threeColumnArea(qtbot)
    area.floatDock(docks['RightBottom'])
    qtbot.wait(10)
    top = area.topContainer
    top.setSizes([360, 340, 200])
    qtbot.wait(10)
    before = top.sizes()

    area.moveDock(docks['RightBottom'], 'bottom', docks['RightTop'])
    qtbot.wait(10)

    assert docks['RightBottom'].area is area
    assert area.topContainer.sizes() == before


def test_a_new_column_is_still_given_room(qtbot):
    """Splitting a dock off into a column of its own must resize the rest.

    The space has to come from somewhere; this is the case where pyqtgraph's
    own sizing is the right answer.
    """
    area, docks = _threeColumnArea(qtbot)
    top = area.topContainer
    columnsBefore = top.count()

    # Beside Middle, which sits directly in the top splitter -- so this is a
    # new column rather than a split inside the existing one.
    area.moveDock(docks['RightTop'], 'right', docks['Middle'])
    qtbot.wait(10)

    top = area.topContainer
    assert top.count() == columnsBefore + 1
    assert docks['RightTop'].container() is top
    assert docks['RightTop'].width() > 0


def test_a_container_that_gains_a_dock_still_makes_room_for_it(qtbot):
    """The suppression must not stop the moved-into column laying itself out."""
    area, docks = _threeColumnArea(qtbot)

    rightColumn = docks['RightTop'].container()
    area.moveDock(docks['Left'], 'bottom', docks['RightBottom'])
    qtbot.wait(10)

    assert docks['Left'].container() is rightColumn
    assert docks['Left'].height() > 0


def test_stretch_values_still_travel_up_past_a_suppressed_resize(qtbot):
    """Only the resize is suppressed; the containers above still need the value."""
    area, docks = _threeColumnArea(qtbot)
    top = area.topContainer
    rightColumn = docks['RightTop'].container()

    docks['RightTop'].setStretch(rightColumn.stretch()[0] + 500, 400)
    qtbot.wait(10)

    assert rightColumn.stretch()[0] >= 500
    assert top.stretch()[0] >= rightColumn.stretch()[0]
