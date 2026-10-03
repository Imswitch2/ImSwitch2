"""The log buffer, the log file, and the Log panel that views them.

The buffer exists because the Log panel can be opened at any point in a session
and the records worth reading are the ones from before it was opened.  These
tests pin that down, plus the two things that are easy to get wrong when a
handler feeds a widget: the DEBUG/console level split, and unregistering.
"""

import logging
import re

import pytest

from imswitch.imcommon.model import logging as imswitchLogging
from imswitch.imcommon.model.logging import (LogRecordBuffer, attachLogFile, baseLogger,
                                             initLogger, logBuffer, setLogLevel)

pytestmark = pytest.mark.nohardware


@pytest.fixture
def buffer():
    """A buffer of its own, attached to the real imswitch logger."""
    buf = LogRecordBuffer(capacity=5)
    baseLogger.addHandler(buf)
    try:
        yield buf
    finally:
        baseLogger.removeHandler(buf)


def test_console_level_does_not_clamp_the_buffer_or_the_file(buffer):
    # The whole point of the split: a rig run that misbehaved is often not one
    # you can repeat with --debug, so the detail has to be recorded up front.
    setLogLevel('INFO')
    initLogger('test').debug('a debug record')

    assert any('a debug record' in text for _levelno, _name, text in buffer.records())
    consoleHandlers = [h for h in baseLogger.handlers if not isinstance(h, LogRecordBuffer)]
    assert consoleHandlers, 'coloredlogs handler missing'
    assert all(h.level == logging.INFO for h in consoleHandlers)
    # The logger itself must stay open at DEBUG or nothing reaches the buffer.
    assert baseLogger.level == logging.DEBUG


def test_buffer_is_bounded(buffer):
    log = initLogger('test')
    for i in range(20):
        log.info(f'record {i}')

    records = buffer.records()
    assert len(records) == 5
    assert 'record 19' in records[-1][2]


def test_total_counts_records_the_buffer_no_longer_holds(buffer):
    # The panel tells what is new by comparing against total(), so it has to keep
    # counting past the point where the deque starts dropping -- an index into a
    # bounded deque would silently mis-slice after a burst.
    log = initLogger('test')
    before = buffer.total()
    for i in range(20):
        log.info(f'record {i}')

    assert buffer.total() - before == 20
    assert len(buffer.records()) == 5


def test_log_file_starts_with_the_buffered_backlog(tmp_path, monkeypatch):
    # A bundle that dies during startup leaves only this file, so the records
    # from before the file was attached have to be in it.
    monkeypatch.setattr(imswitchLogging, '_logFileHandler', None)
    log = initLogger('test')
    log.info('before the file existed')

    path = tmp_path / 'logs' / 'imswitch.log'
    handler = attachLogFile(str(path))
    assert handler is not None
    try:
        log.info('after the file existed')
        handler.flush()
        contents = path.read_text(encoding='utf-8')
    finally:
        baseLogger.removeHandler(handler)
        handler.close()

    # Both halves carry a timestamp: the backlog goes to the stream verbatim
    # while live records are formatted, and those two paths have drifted before
    # (the file once stamped the backlog and nothing after it).  Checked on these
    # two records rather than every line, because a buffered traceback spans
    # several lines and only its first carries the stamp.
    stamped = {}
    for line in contents.splitlines():
        for marker in ('before the file existed', 'after the file existed'):
            if marker in line:
                stamped[marker] = line
    assert set(stamped) == {'before the file existed', 'after the file existed'}, stamped
    for marker, line in stamped.items():
        assert re.match(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', line), (
            f'{marker!r} is not timestamped: {line!r}'
        )


def test_attaching_a_log_file_twice_keeps_the_first(tmp_path, monkeypatch):
    monkeypatch.setattr(imswitchLogging, '_logFileHandler', None)
    first = attachLogFile(str(tmp_path / 'a' / 'imswitch.log'))
    try:
        assert attachLogFile(str(tmp_path / 'b' / 'imswitch.log')) is first
    finally:
        baseLogger.removeHandler(first)
        first.close()


def test_an_unwritable_log_path_does_not_stop_startup(tmp_path, monkeypatch):
    # Read-only or full disks must not keep ImSwitch2 from starting.
    monkeypatch.setattr(imswitchLogging, '_logFileHandler', None)
    blocker = tmp_path / 'blocker'
    blocker.write_text('not a directory')
    assert attachLogFile(str(blocker / 'logs' / 'imswitch.log')) is None


def test_log_panel_is_off_by_default_and_opt_in():
    # Hidden at startup, in a bundle as much as from a terminal: it is always
    # built and always one click away under View > Log.
    from imswitch.improcess.model.processing_config import is_log_panel_enabled

    assert is_log_panel_enabled({}) is False
    assert is_log_panel_enabled({'logPanel': True}) is True
    assert is_log_panel_enabled({'logPanel': False}) is False


@pytest.mark.ui
class TestLogWidget:
    @staticmethod
    def _widget(qtbot):
        from imswitch.imcommon.view.LogWidget import LogWidget

        widget = LogWidget()
        qtbot.addWidget(widget)
        return widget

    def test_shows_records_logged_before_it_was_built(self, qtbot):
        initLogger('test').warning('logged before the panel existed')
        widget = self._widget(qtbot)
        assert 'logged before the panel existed' in widget.textEdit.toPlainText()

    def test_live_records_arrive(self, qtbot):
        # Through the widget's own timer, not by calling the drain directly:
        # a panel whose timer never starts looks fine in every other test.
        widget = self._widget(qtbot)
        initLogger('test').warning('a live record')
        qtbot.waitUntil(lambda: 'a live record' in widget.textEdit.toPlainText(),
                        timeout=3000)

    def test_raising_the_level_reveals_buffered_debug_records(self, qtbot):
        initLogger('test').debug('a buffered debug record')
        widget = self._widget(qtbot)
        # Hidden at the default INFO filter...
        assert 'a buffered debug record' not in widget.textEdit.toPlainText()

        widget.levelCombo.setCurrentIndex(0)  # Debug
        # ...and back from the buffer, not from what the widget happened to hold.
        assert 'a buffered debug record' in widget.textEdit.toPlainText()

    def test_text_filter_applies_to_history(self, qtbot):
        log = initLogger('test')
        log.warning('keep this one')
        log.warning('drop that one')
        widget = self._widget(qtbot)

        widget.filterEdit.setText('keep')
        shown = widget.textEdit.toPlainText()
        assert 'keep this one' in shown
        assert 'drop that one' not in shown

    def test_dropping_panels_without_an_event_loop_does_not_crash(self, qapp):
        """Regression: this segfaulted the process.

        The first version had the buffer push records to registered listeners,
        which the panel marshalled onto the GUI thread through a Qt signal.  A
        panel torn down without the event loop running -- which is what a pytest
        teardown looks like -- left that signal connected to a dead receiver, and
        the next record anywhere in the process killed the interpreter.  It took
        out two CI workers before the cause was found.
        """
        from imswitch.imcommon.view.LogWidget import LogWidget

        for _ in range(5):
            widget = LogWidget()
            widget.deleteLater()
            del widget

        initLogger('test').warning('a record after the panels were dropped')
        # Reaching here at all is the assertion; the buffer kept the record.
        assert any('after the panels were dropped' in text
                   for _levelno, _name, text in logBuffer.records())

        # Hand the worker back in one piece.  Those deleteLater() calls are still
        # queued, and leaving them for whichever later test next spins the event
        # loop made an unrelated one segfault -- which is this test's own mess,
        # not the bug it guards.
        qapp.processEvents()

    def test_its_timer_belongs_to_it(self, qtbot):
        # Ownership is what makes the above safe: a timer parented to the widget
        # cannot fire after the widget is gone.
        widget = self._widget(qtbot)
        assert widget._timer.parent() is widget


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
