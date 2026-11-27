"""Panning the workflow editor's canvas: the view follows a middle-button
drag exactly and stops when the button is released; the wheel never moves it
sideways (a pressed wheel reports tilt events, and drivers' auto-scroll sends
wheel events: before, those drifted the canvas continuously)."""
import pytest

pytest.importorskip("pytestqt")

from qtpy import QtCore, QtGui, QtWidgets  # noqa: E402

from imswitch.imcommon.view.workfloweditor.canvas.scene import WorkflowScene  # noqa: E402
from imswitch.imcommon.view.workfloweditor.canvas.view import CanvasView  # noqa: E402

LEFT, MIDDLE, NONE = QtCore.Qt.LeftButton, QtCore.Qt.MiddleButton, QtCore.Qt.NoButton


@pytest.fixture
def view(qtbot):
    scene = WorkflowScene()
    widget = CanvasView(scene)
    qtbot.addWidget(widget)
    widget.resize(600, 400)
    widget.show()
    qtbot.waitExposed(widget)
    widget.centerOn(0, 0)
    yield widget
    widget.close()


def _mouse(view, kind, pos, button, buttons):
    viewport = view.viewport()
    event = QtGui.QMouseEvent(kind, QtCore.QPointF(pos), QtCore.QPointF(viewport.mapToGlobal(pos)),
                              button, buttons, QtCore.Qt.NoModifier)
    QtWidgets.QApplication.sendEvent(viewport, event)


def _wheel(view, pos, dx, dy, buttons=NONE):
    viewport = view.viewport()
    event = QtGui.QWheelEvent(QtCore.QPointF(pos), QtCore.QPointF(viewport.mapToGlobal(pos)), QtCore.QPoint(0, 0),
                              QtCore.QPoint(dx, dy), buttons, QtCore.Qt.NoModifier, QtCore.Qt.NoScrollPhase, False)
    QtWidgets.QApplication.sendEvent(viewport, event)


def _centre(view):
    return view.mapToScene(view.viewport().rect().center())


def _drag(view, start, steps, button=MIDDLE):
    _mouse(view, QtCore.QEvent.MouseButtonPress, start, button, button)
    pos = QtCore.QPoint(start)
    for step in steps:
        pos = pos + step
        _mouse(view, QtCore.QEvent.MouseMove, pos, NONE, button)
    _mouse(view, QtCore.QEvent.MouseButtonRelease, pos, button, NONE)
    return pos


@pytest.mark.parametrize("zoom", [1.0, 0.5])
def test_a_middle_drag_moves_the_view_by_the_drag_and_then_stops(view, zoom):
    view.zoom_by(zoom)
    before = _centre(view)
    end = _drag(view, QtCore.QPoint(300, 200), [QtCore.QPoint(10, 5)] * 8)
    after = _centre(view)
    # dragging the canvas right/down by (80, 40) px shows what was left/up of it
    assert after.x() == pytest.approx(before.x() - 80 / view.zoom, abs=1.5 / view.zoom)
    assert after.y() == pytest.approx(before.y() - 40 / view.zoom, abs=1.5 / view.zoom)
    assert not view.panning
    # after the release, moving the pointer (or holding still) moves nothing
    for i in range(10):
        _mouse(view, QtCore.QEvent.MouseMove, end + QtCore.QPoint(-5 * i, 3 * i), NONE, NONE)
    assert _centre(view) == after


def test_the_wheel_does_not_scroll_sideways_and_is_ignored_while_panning(view):
    before = _centre(view)
    for _ in range(20):                        # a tilted wheel, repeating while held
        _wheel(view, QtCore.QPoint(300, 200), 120, 0)
    assert _centre(view) == before and view.zoom == 1.0
    _mouse(view, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(300, 200), MIDDLE, MIDDLE)
    _wheel(view, QtCore.QPoint(300, 200), -120, 120, buttons=MIDDLE)
    assert _centre(view) == before and view.zoom == 1.0
    _mouse(view, QtCore.QEvent.MouseButtonRelease, QtCore.QPoint(300, 200), MIDDLE, NONE)
    _wheel(view, QtCore.QPoint(300, 200), 0, 120)          # the plain wheel still zooms
    assert view.zoom > 1.0


def test_a_missed_release_ends_the_pan_at_the_next_move(view):
    _mouse(view, QtCore.QEvent.MouseButtonPress, QtCore.QPoint(300, 200), MIDDLE, MIDDLE)
    _mouse(view, QtCore.QEvent.MouseMove, QtCore.QPoint(310, 200), NONE, MIDDLE)
    assert view.panning
    moved = _centre(view)
    # the release went to a popup: the next move arrives with no button held
    _mouse(view, QtCore.QEvent.MouseMove, QtCore.QPoint(200, 100), NONE, NONE)
    assert not view.panning and _centre(view) == moved


def test_a_quick_second_middle_press_pans_again_instead_of_opening_quick_add(view, qtbot):
    opened = []
    view.sigQuickAddHere.connect(opened.append)
    _drag(view, QtCore.QPoint(300, 200), [QtCore.QPoint(5, 0)])
    before = _centre(view)
    _mouse(view, QtCore.QEvent.MouseButtonDblClick, QtCore.QPoint(300, 200), MIDDLE, MIDDLE)
    _mouse(view, QtCore.QEvent.MouseMove, QtCore.QPoint(330, 200), NONE, MIDDLE)
    _mouse(view, QtCore.QEvent.MouseButtonRelease, QtCore.QPoint(330, 200), MIDDLE, NONE)
    assert opened == []
    assert _centre(view).x() == pytest.approx(before.x() - 30, abs=1.5)


def test_space_and_left_drag_pans_too(view):
    view.setFocus()
    view.keyPressEvent(QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_Space, QtCore.Qt.NoModifier))
    before = _centre(view)
    _drag(view, QtCore.QPoint(300, 200), [QtCore.QPoint(0, -20)], button=LEFT)
    view.keyReleaseEvent(QtGui.QKeyEvent(QtCore.QEvent.KeyRelease, QtCore.Qt.Key_Space, QtCore.Qt.NoModifier))
    assert _centre(view).y() == pytest.approx(before.y() + 20, abs=1.5)
    assert not view.panning
