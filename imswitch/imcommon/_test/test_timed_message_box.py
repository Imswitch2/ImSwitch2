"""A message box that answers itself when nobody is there.

The startup restore warning and the shutdown "save widget state?" question
are both modal, and both used to wait forever.  These pin the three
behaviours that make the timed box safe: it takes the *unattended* answer,
not the keyboard default, when the countdown runs out; activity inside the
box restarts the countdown; and the label says which button will be pressed.
"""

import pytest
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.view.guitools import TimedMessageBox, askYesNoQuestion, showWarning

pytestmark = [pytest.mark.nohardware, pytest.mark.ui]


def _box(qtbot, unattendedAfterS=2):
    box = TimedMessageBox(
        QtWidgets.QMessageBox.Question, 'Title', 'Text',
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        defaultButton=QtWidgets.QMessageBox.Yes,
        unattendedButton=QtWidgets.QMessageBox.No,
        unattendedAfterS=unattendedAfterS,
    )
    qtbot.addWidget(box)
    return box


def test_countdown_label_names_the_button_that_will_be_pressed(qtbot):
    box = _box(qtbot, unattendedAfterS=30)
    noButton = box.button(QtWidgets.QMessageBox.No)
    yesButton = box.button(QtWidgets.QMessageBox.Yes)

    assert noButton.text().endswith('(30 s)')
    assert '(' not in yesButton.text()
    assert box.defaultButton() is yesButton


def test_unattended_answer_is_taken_when_the_countdown_runs_out(qtbot):
    box = _box(qtbot, unattendedAfterS=1)
    box.show()

    with qtbot.waitSignal(box.finished, timeout=5000):
        pass

    assert box.clickedStandardButton() == QtWidgets.QMessageBox.No


def test_activity_inside_the_box_restarts_the_countdown(qtbot):
    box = _box(qtbot, unattendedAfterS=3)
    box.show()
    qtbot.wait(1100)
    assert box.remainingSeconds() < 3

    qtbot.mouseMove(box.button(QtWidgets.QMessageBox.Yes))
    QtWidgets.QApplication.processEvents()

    assert box.remainingSeconds() == 3
    assert box.button(QtWidgets.QMessageBox.No).text().endswith('(3 s)')
    box.reject()


def test_clicking_the_other_button_wins(qtbot):
    box = _box(qtbot, unattendedAfterS=30)
    box.show()

    qtbot.mouseClick(box.button(QtWidgets.QMessageBox.Yes), QtCore.Qt.LeftButton)

    assert box.clickedStandardButton() == QtWidgets.QMessageBox.Yes
    assert not box.isVisible()


def test_ask_yes_no_question_unattended_answers_no(qtbot):
    assert askYesNoQuestion(None, 'T', 'Q', unattendedAnswer=False,
                            unattendedAfterS=1) is False


def test_ask_yes_no_question_unattended_can_answer_yes(qtbot):
    assert askYesNoQuestion(None, 'T', 'Q', unattendedAnswer=True,
                            unattendedAfterS=1) is True


def test_show_warning_closes_by_itself(qtbot):
    # Returning at all is the assertion: an untimed warning would block here.
    showWarning(None, 'T', 'Something was not applied', unattendedAfterS=1)
