"""The Directory watcher dock is offered only to streaming reconstructors.

Constructing the real ``ImProcessMainView`` crashes the test process, so the
shipping method is bound to a stub holding fake docks -- the method under test
is the real one, only its surroundings are faked.

    pytest imswitch/improcess/_test/test_directory_watcher_visibility.py -v
"""

import pytest

from imswitch.improcess.view.ImProcessMainView import ImProcessMainView


class _Label:
    """A dock's tab label. Once the dock joins a tab group pyqtgraph
    reparents this into the tab bar, so hiding the dock does not hide it."""

    def __init__(self):
        self.visible = True

    def setVisible(self, value):
        self.visible = bool(value)

    def isVisible(self):
        return self.visible


class _Dock:
    def __init__(self):
        self.hidden = False
        self.label = _Label()

    def show(self):
        self.hidden = False

    def hide(self):
        self.hidden = True

    def isHidden(self):
        return self.hidden


class _Check:
    def __init__(self, checked=False):
        self.checked = checked

    def isChecked(self):
        return self.checked

    def setChecked(self, value):
        self.checked = bool(value)


class _Frame:
    def __init__(self, live_checked=False):
        self.liveCheck = _Check(live_checked)


class _View:
    """The slice of ImProcessMainView the method touches."""

    def __init__(self, *, panel_enabled=True, live_checked=False, dock=True):
        self._showFileWatcherPanel = panel_enabled
        self.directoryWatcherFrame = _Frame(live_checked)
        self.docks = {"Directory watcher": _Dock()} if dock else {}
        self.synced = 0

    def _syncDockVisibilityActions(self):
        self.synced += 1

    # The real helpers: setDirectoryWatcherAvailable delegates to them, so
    # faking them would stop testing the behaviour that matters.
    def isDirectoryWatcherRunning(self):
        return ImProcessMainView.isDirectoryWatcherRunning.__get__(self)()

    def stopDirectoryWatcher(self):
        return ImProcessMainView.stopDirectoryWatcher.__get__(self)()

    # A staticmethod on the view, so it binds as a plain function.
    _setDockVisible = staticmethod(ImProcessMainView._setDockVisible)


def _apply(view, available):
    ImProcessMainView.setDirectoryWatcherAvailable.__get__(view)(available)
    return view.docks.get("Directory watcher")


def test_shown_for_a_streaming_reconstructor():
    view = _View()
    _apply(view, False)

    assert _apply(view, True).isHidden() is False


def test_hidden_for_a_reconstructor_that_cannot_stream():
    view = _View()

    assert _apply(view, False).isHidden() is True


def test_the_startup_flag_still_wins():
    """A panel switched off in the config stays off however capable the
    selected reconstructor is."""
    view = _View(panel_enabled=False)

    assert _apply(view, True).isHidden() is True


def test_a_running_watch_is_stopped_when_the_panel_goes_away():
    """Hiding a live run would leave it going with no way to see or stop it.
    Clearing the checkbox emits sigLiveChanged(False) through the frame's own
    wiring, which is what actually stops the watcher."""
    view = _View(live_checked=True)

    _apply(view, False)

    assert view.directoryWatcherFrame.liveCheck.isChecked() is False


def test_an_idle_watch_is_left_alone():
    """Only a checked box is cleared, so no spurious sigLiveChanged is emitted."""
    view = _View(live_checked=False)

    _apply(view, False)

    assert view.directoryWatcherFrame.liveCheck.isChecked() is False


def test_showing_does_not_start_a_watch():
    view = _View()

    _apply(view, True)

    assert view.directoryWatcherFrame.liveCheck.isChecked() is False


def test_the_view_menu_checkboxes_are_resynced():
    view = _View()

    _apply(view, True)
    _apply(view, False)

    assert view.synced == 2


def test_a_missing_dock_is_survivable():
    view = _View(dock=False)

    ImProcessMainView.setDirectoryWatcherAvailable.__get__(view)(True)   # no raise



# --- the tab label, not just the dock ---------------------------------------

def test_hiding_also_hides_the_tab_label():
    """Verified against real pyqtgraph: the watcher dock sits in a TContainer
    (Multidata is added 'above' it), and Dock.hide() leaves label.isVisible()
    True -- so the panel still looks available. This is the bug that made the
    first version of this feature appear not to work."""
    view = _View()

    dock = _apply(view, False)

    assert dock.isHidden() is True
    assert dock.label.isVisible() is False


def test_showing_restores_the_tab_label():
    view = _View()
    _apply(view, False)

    dock = _apply(view, True)

    assert dock.isHidden() is False
    assert dock.label.isVisible() is True


def test_a_dock_without_a_label_is_survivable():
    view = _View()
    view.docks["Directory watcher"].label = None

    _apply(view, False)                      # no raise


# --- the capability this is driven by --------------------------------------

def test_the_real_reconstructors_report_the_expected_capability():
    """What the controller passes in: a declared capability, not an id check."""
    from imswitch.improcess.reconstructors.beadrec.reconstructor import (
        BeadRecReconstructor,
    )
    from imswitch.improcess.reconstructors.monalisa.reconstructor import (
        MonalisaReconstructor,
    )
    from imswitch.improcess.reconstructors.view_only.reconstructor import (
        ViewOnlyReconstructor,
    )

    assert getattr(MonalisaReconstructor, "supports_streaming", False)
    assert getattr(ViewOnlyReconstructor, "supports_streaming", False)
    assert not getattr(BeadRecReconstructor, "supports_streaming", False)



# --- changing reconstructor while a watch is running ------------------------

class _Widget(_View):
    """Adds the picker surface the change-guard touches."""

    def __init__(self, *, live_checked=True, answer=True):
        super().__init__(live_checked=live_checked)
        self.answer = answer
        self.asked = 0
        self.restored = []

    def confirmDirectoryWatcherInterruption(self):
        self.asked += 1
        return self.answer

    def setActiveReconstructorName(self, name):
        self.restored.append(name)


def _manager(widget, active_name="MoNaLISA"):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from imswitch.improcess.controller.ReconstructorManagerController import (
        ReconstructorManagerController,
    )

    c = ReconstructorManagerController.__new__(ReconstructorManagerController)
    c._widget = widget
    c._logger = MagicMock()
    c._main = SimpleNamespace(_activeReconstructor=SimpleNamespace(name=active_name))
    return c


def test_accepting_stops_the_watch_and_proceeds():
    widget = _Widget(answer=True)
    manager = _manager(widget)

    assert manager._confirm_reconstructor_change() is True
    assert widget.asked == 1
    assert widget.directoryWatcherFrame.liveCheck.isChecked() is False
    assert widget.restored == []


def test_declining_restores_the_picker_and_leaves_the_watch_running():
    widget = _Widget(answer=False)
    manager = _manager(widget, active_name="MoNaLISA")

    assert manager._confirm_reconstructor_change() is False
    assert widget.directoryWatcherFrame.liveCheck.isChecked() is True
    assert widget.restored == ["MoNaLISA"]


def test_nothing_running_means_no_question():
    """The guard is called on every change, so an idle watcher must not
    interrogate the user."""
    widget = _Widget(live_checked=False, answer=True)
    widget.answer = ImProcessMainView.confirmDirectoryWatcherInterruption.__get__(
        widget
    )()
    manager = _manager(widget)

    assert widget.answer is True          # returned early, no dialog
    assert manager._confirm_reconstructor_change() is True


def test_a_view_without_the_panel_is_survivable():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from imswitch.improcess.controller.ReconstructorManagerController import (
        ReconstructorManagerController,
    )

    c = ReconstructorManagerController.__new__(ReconstructorManagerController)
    c._widget = SimpleNamespace()          # no watcher methods at all
    c._logger = MagicMock()
    c._main = SimpleNamespace(_activeReconstructor=None)

    assert c._confirm_reconstructor_change() is True


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))


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
