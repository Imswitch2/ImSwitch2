from qtpy import QtCore, QtWidgets


class TimedMessageBox(QtWidgets.QMessageBox):
    """A message box that answers itself when nobody attends to it.

    A modal box that waits forever is right when the person is at the
    microscope, and wrong when they are not: an application started by a
    script, or closed from a remote desktop someone walked away from, hangs on
    a question nobody answers. The box presses ``unattendedButton`` once
    ``unattendedAfterS`` seconds have passed with no key or mouse activity
    inside it. The countdown is shown in that button's label, so it is clear
    which answer will be taken, and it restarts on any activity -- a person
    reading the text, or reaching for the other button, never has it decided
    for them.

    The keyboard default (Enter) stays whatever the caller sets; the
    unattended answer is a separate choice, so a shutdown prompt can default
    to *Yes* for the operator at the keyboard and to *No* for an empty room.
    """

    _ACTIVITY_EVENTS = (
        QtCore.QEvent.KeyPress,
        QtCore.QEvent.MouseButtonPress,
        QtCore.QEvent.MouseButtonDblClick,
        QtCore.QEvent.MouseMove,
        QtCore.QEvent.Wheel,
        QtCore.QEvent.TouchBegin,
    )

    def __init__(self, icon, title, text, buttons, parent=None, *,
                 defaultButton=QtWidgets.QMessageBox.NoButton,
                 unattendedButton, unattendedAfterS):
        super().__init__(icon, title, text, buttons, parent)
        if defaultButton != QtWidgets.QMessageBox.NoButton:
            self.setDefaultButton(defaultButton)

        self._unattendedButton = self.button(unattendedButton)
        if self._unattendedButton is None:
            raise ValueError('unattendedButton must be one of the box\'s buttons')
        self._unattendedLabel = self._unattendedButton.text()
        self._unattendedAfterS = max(1, int(round(unattendedAfterS)))
        self._remainingS = self._unattendedAfterS

        self._countdown = QtCore.QTimer(self)
        self._countdown.setInterval(1000)
        self._countdown.timeout.connect(self._tick)
        self._updateUnattendedLabel()

    # -- Qt lifecycle -------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        self._restartCountdown()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            # Key and mouse events go to the focused child (a button), not to
            # the box, so a filter on the box alone would miss them.
            app.installEventFilter(self)
        self._countdown.start()

    def hideEvent(self, event):
        self._countdown.stop()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().hideEvent(event)

    def eventFilter(self, obj, event):
        if (event.type() in self._ACTIVITY_EVENTS
                and isinstance(obj, QtWidgets.QWidget)
                and (obj is self or self.isAncestorOf(obj))):
            self._restartCountdown()
        return super().eventFilter(obj, event)

    # -- countdown ----------------------------------------------------------

    def remainingSeconds(self):
        """Seconds left before the unattended answer is taken."""
        return self._remainingS

    def _restartCountdown(self):
        self._remainingS = self._unattendedAfterS
        self._updateUnattendedLabel()
        if self._countdown.isActive():
            self._countdown.start()  # restart the 1 s interval from now

    def _tick(self):
        self._remainingS -= 1
        if self._remainingS <= 0:
            self._countdown.stop()
            self._remainingS = 0
            self._updateUnattendedLabel()
            self._unattendedButton.click()
            return
        self._updateUnattendedLabel()

    def _updateUnattendedLabel(self):
        self._unattendedButton.setText(
            f'{self._unattendedLabel} ({self._remainingS} s)'
        )

    # -- result -------------------------------------------------------------

    def clickedStandardButton(self):
        """The standard button that closed the box; NoButton if none did."""
        clicked = self.clickedButton()
        if clicked is None:
            return QtWidgets.QMessageBox.NoButton
        return self.standardButton(clicked)


def askYesNoQuestion(widget, title, question, *,
                     unattendedAnswer=None, unattendedAfterS=30):
    """ Asks the user a yes/no question and returns whether "yes" was clicked.

    With ``unattendedAnswer`` given (True for *Yes*, False for *No*), the box
    takes that answer by itself after ``unattendedAfterS`` seconds with no
    activity inside it; see :class:`TimedMessageBox`. """
    buttons = QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
    if unattendedAnswer is None:
        result = QtWidgets.QMessageBox.question(widget, title, question, buttons)
        return result == QtWidgets.QMessageBox.Yes

    box = TimedMessageBox(
        QtWidgets.QMessageBox.Question, title, question, buttons, widget,
        defaultButton=QtWidgets.QMessageBox.Yes,
        unattendedButton=(QtWidgets.QMessageBox.Yes if unattendedAnswer
                          else QtWidgets.QMessageBox.No),
        unattendedAfterS=unattendedAfterS,
    )
    box.exec_()
    return box.clickedStandardButton() == QtWidgets.QMessageBox.Yes


def showWarning(widget, title, message, *, unattendedAfterS=None):
    """ Tells the user about something that went wrong but was recovered from,
    e.g. hardware refusing a setting the GUI had already accepted.

    With ``unattendedAfterS`` given, the box closes by itself after that many
    seconds with no activity inside it; see :class:`TimedMessageBox`. """
    if unattendedAfterS is None:
        QtWidgets.QMessageBox.warning(widget, title, message)
        return

    box = TimedMessageBox(
        QtWidgets.QMessageBox.Warning, title, message, QtWidgets.QMessageBox.Ok,
        widget,
        defaultButton=QtWidgets.QMessageBox.Ok,
        unattendedButton=QtWidgets.QMessageBox.Ok,
        unattendedAfterS=unattendedAfterS,
    )
    box.exec_()


def askForTextInput(widget, title, label, suggested=None):
    """ Asks the user to enter a text string. Returns the string if "yes" is
    clicked, None otherwise. """
    result, okClicked = QtWidgets.QInputDialog.getText(
        widget, title, label, flags=QtCore.Qt.WindowSystemMenuHint | QtCore.Qt.WindowTitleHint, 
        text=suggested
    )
    return result if okClicked else None


def askForFilePath(widget, caption=None, defaultFolder=None, nameFilter=None, isSaving=False,multiFiles=False):
    """ Asks the user to pick a file path. Returns the file path if "OK" is
    clicked, None otherwise. """
    func = (QtWidgets.QFileDialog().getOpenFileName if not isSaving and not multiFiles
            else QtWidgets.QFileDialog().getOpenFileNames if not isSaving
            else QtWidgets.QFileDialog().getSaveFileName)

    result = func(widget, caption=caption, directory=defaultFolder, filter=nameFilter)[0]
    return result if result else None


def askForFolderPath(widget, caption=None, defaultFolder=None):
    """ Asks the user to pick a folder path. Returns the folder path if "OK" is
    clicked, None otherwise. """
    result = QtWidgets.QFileDialog.getExistingDirectory(widget, caption=caption,
                                                        directory=defaultFolder)
    return result if result else None


def askForTwoTextInputs(title, label1, label2, default1="", default2="", multiline2=True, readonly1=False):
    """
    Asks the user for two text inputs.
    Args:
        title (str): Dialog title.
        label1 (str): Label for first input.
        label2 (str): Label for second input.
        default1 (str): Default text for first input.
        default2 (str): Default text for second input.
        multiline2 (bool): If True, second input is QTextEdit; else QLineEdit.
        readonly1 (bool): If True, first input is read-only (cannot be edited).

    Returns:
        (val1, val2) if OK clicked and val1 is not empty (or readonly)
        (None, None) if Cancel clicked
    Notes:
    - Parent is None to ensure the dialog always appears in front.
    """
    dialog = QtWidgets.QDialog(None)
    dialog.setWindowTitle(title)
    dialog.setWindowFlags(QtCore.Qt.WindowSystemMenuHint | QtCore.Qt.WindowTitleHint)
    dialog.setWindowModality(QtCore.Qt.ApplicationModal)

    layout = QtWidgets.QVBoxLayout(dialog)

    edit1 = QtWidgets.QLineEdit()
    edit1.setText(default1)
    edit1.setReadOnly(readonly1)
    layout.addWidget(QtWidgets.QLabel(label1))
    layout.addWidget(edit1)

    layout.addWidget(QtWidgets.QLabel(label2))
    if multiline2:
        edit2 = QtWidgets.QTextEdit()
        edit2.setPlainText(default2)
        edit2.setFixedHeight(80)
    else:
        edit2 = QtWidgets.QLineEdit()
        edit2.setText(default2)
    layout.addWidget(edit2)

    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
    )
    layout.addWidget(buttons)

    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)

    if readonly1:
        edit2.setFocus()
    else:
        edit1.setFocus()

    if dialog.exec() != QtWidgets.QDialog.Accepted:
        return None, None

    val1 = edit1.text().strip()
    val2 = edit2.toPlainText().strip() if multiline2 else edit2.text().strip()

    if not val1:
        return None, None

    return val1, val2





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
