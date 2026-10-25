"""The Expert panel's Simple view (docs/design/plans/expert-simple-view.md):
the scan's values as a list, beside the drawing of its step and the preview,
with nothing that adds, removes or reorders.

The **Parameters** card lists the values the scan is made from. In this part
that is a scan that is its template's output: the template's own form, inline,
each committed field making the scan again from the template (the panel's
``sigTemplateEdited``, which the controller answers). The card is given the
form class it hosts, so this module imports nothing from the panel.

What the card shows changes under no one's hands: a new scan replaces it
whole, the field being typed in too; a change it makes by itself waits until
the focus leaves it. Whatever it removes is disconnected first, hidden, and
deleted later, never on the stack of a signal of its own.
"""

from __future__ import annotations

from qtpy import QtWidgets

from ._expert_scan_parts import Card, _muted


class ParametersCard(Card):
    """The scan's values: the template's form when the scan is its
    template's output. Needs from the panel: ``template()``, ``devices``,
    ``editPending()``, ``refreshParts()`` and the signals
    ``sigTemplateEdited`` and ``sigPresetRequested``."""

    def __init__(self, panel, formClass, parent=None):
        super().__init__('Parameters', parent)
        self._panel, self._formClass = panel, formClass
        self.valuesLabel = _muted('')
        self.valuesLabel.setVisible(False)
        self.header.addWidget(self.valuesLabel)
        self.holder = QtWidgets.QWidget()
        self._holderLayout = QtWidgets.QVBoxLayout(self.holder)
        self._holderLayout.setContentsMargins(0, 0, 0, 0)
        self.body.addWidget(self.holder)
        self._form = None
        self._invalid = False       # the next refresh replaces the content, whatever has the focus
        self._pending = False       # a change of content was put off
        self._waiting = False       # ... and the card listens for the focus to leave

    # -- what it shows ------------------------------------------------------------

    def form(self):
        """The template's form, or None."""
        return self._form

    def showsTemplate(self) -> bool:
        return self._form is not None

    def invalidate(self):
        """The next refresh replaces the content, whatever has the focus
        (entering the view)."""
        self._invalid = True

    def programShown(self):
        """A new scan is shown: its content replaces this one, the field being
        typed in too -- what that field showed was for the scan it replaced.
        Unless the template's form is committing: its own commit is what made
        the scan. This only marks; the refresh after the tables are written
        builds the new content."""
        if self._form is not None and self._form.isCommitting():
            return
        self.invalidate()

    def refresh(self):
        preset, params, problem = self._panel.template() or (None, {}, '')
        wanted = preset.id if preset is not None else None
        shown = self._form.preset.id if self._form is not None else None
        if wanted != shown or (wanted is not None and self._invalid):
            if self._invalid or self._mayChange():
                self._replace(preset, params, problem)
            else:
                self._putOff()
        else:
            self._pending = False
            self._stopWaiting()
            if self._form is not None:
                self._form.setProblem(problem)
                if not problem:
                    self._form.setValues(params)
        self._invalid = False
        self.valuesLabel.setText('the template\'s values' if self._form is not None else '')
        self.valuesLabel.setVisible(self._form is not None)

    # -- replacing the content ----------------------------------------------------

    def _replace(self, preset, params, problem):
        self._pending = False
        self._stopWaiting()
        self._remove()
        if preset is None:
            return
        form = self._formClass(preset, self._panel.devices, params)
        form.sigCommitted.connect(
            lambda pid=preset.id: self._panel.sigTemplateEdited.emit(pid))
        form.sigEditRequested.connect(
            lambda pid=preset.id: self._panel.sigPresetRequested.emit(pid))
        form.setProblem(problem)
        self._holderLayout.addWidget(form)
        self._form = form

    def _remove(self):
        """Take the form away. Its signals go first: a field that commits as
        it is hidden (focus out) writes nowhere. It is deleted later, not
        here: this may run inside a signal of one of its own editors."""
        form, self._form = self._form, None
        if form is None:
            return
        for signal in (form.sigCommitted, form.sigEditRequested):
            try:
                signal.disconnect()
            except TypeError:
                pass                  # nothing was connected
        form.hide()
        form.setParent(None)
        form.deleteLater()

    # -- no change under the user's hands -----------------------------------------

    def _holdsFocus(self) -> bool:
        focus = QtWidgets.QApplication.focusWidget()
        return focus is not None and self.isAncestorOf(focus)

    def _mayChange(self) -> bool:
        """Whether a change the card makes by itself can be made now: not
        while a field of the card has the focus, and not while a change put
        off waits for the report of an edit -- leaving a field commits it
        before the focus signal comes, and its read-back arrives only with
        the report, which decides."""
        return not self._holdsFocus() and not (self._pending and self._panel.editPending())

    def _putOff(self):
        """Keep the content as it is, live, until the focus leaves the card
        (and the edit it brought is reported)."""
        self._pending = True
        if self._holdsFocus() and not self._waiting:
            self._waiting = True
            QtWidgets.QApplication.instance().focusChanged.connect(self._focusChanged)

    def _stopWaiting(self):
        if self._waiting:
            self._waiting = False
            QtWidgets.QApplication.instance().focusChanged.disconnect(self._focusChanged)

    def _focusChanged(self, _old, now):
        if now is not None and self.isAncestorOf(now):
            return                    # moved inside the card: still in use
        self._stopWaiting()
        # Leaving a field commits it before this signal comes: if that edit is
        # not reported yet, the refresh its report brings decides.
        if not self._panel.editPending():
            self._panel.refreshParts()


__all__ = ['ParametersCard']


# Copyright (C) 2020-2026 ImSwitch developers
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
