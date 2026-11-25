"""The GUI-stall watchdog and the step-timing lines (imcommon.model.gui_diagnostics)."""
import logging
import threading
import time

import pytest
from qtpy import QtTest

from imswitch.imcommon.model import gui_diagnostics as gd


def test_the_threshold_comes_from_the_environment_only_when_it_is_a_positive_number():
    assert gd.stallThresholdFromEnvironment({'IMSWITCH_GUI_STALL_SECONDS': '2.5'}) == 2.5
    for value in ('', '0', '-1', 'soon'):
        assert gd.stallThresholdFromEnvironment({'IMSWITCH_GUI_STALL_SECONDS': value}) is None
    assert gd.stallThresholdFromEnvironment({}) is None
    assert gd.startGuiStallWatchdog(None, '/nonexistent') is None
    assert gd.startGuiStallWatchdog(0, '/nonexistent') is None


def test_a_blocked_gui_thread_is_dumped_with_the_stack_it_is_in(qtbot, tmp_path):
    watchdog = gd.startGuiStallWatchdog(0.3, str(tmp_path))
    try:
        QtTest.QTest.qWait(100)                  # the heartbeat is running
        assert watchdog.dumpCount == 0

        def theGuiThreadIsStuckHere():
            time.sleep(1.0)
        theGuiThreadIsStuckHere()                # the event loop is not pumped
        QtTest.QTest.qWait(400)                  # it is again: the stall is closed
    finally:
        watchdog.stop()
    text = (tmp_path / gd.STALL_LOG_NAME).read_text()
    assert 'GUI thread not pumping events' in text
    assert 'theGuiThreadIsStuckHere' in text     # the main thread's stack names the call
    assert 'responsive again' in text
    assert watchdog.dumpCount >= 1 and watchdog.stalls and watchdog.stalls[0] >= 0.3


def test_a_quiet_gui_thread_writes_nothing(qtbot, tmp_path):
    watchdog = gd.startGuiStallWatchdog(0.3, str(tmp_path))
    try:
        QtTest.QTest.qWait(800)
    finally:
        watchdog.stop()
    assert not (tmp_path / gd.STALL_LOG_NAME).exists() and watchdog.dumpCount == 0


def test_a_step_logs_its_name_duration_and_thread(caplog):
    caplog.set_level(logging.DEBUG, logger='imswitch')
    seen = []

    def work():
        with gd.timedStep('scan stop', 'stop the tasks'):
            time.sleep(0.02)
        seen.append(1)
    thread = threading.Thread(target=work, name='StopWorker')
    thread.start()
    thread.join()
    lines = [r.getMessage() for r in caplog.records if 'scan stop' in r.getMessage()]
    assert len(lines) == 1 and seen == [1]
    assert 'step "stop the tasks"' in lines[0] and 'on thread StopWorker' in lines[0]
    assert caplog.records[-1].levelno == logging.INFO      # long enough to be INFO
    with gd.timedStep('scan stop', 'nothing to do'):
        pass
    assert caplog.records[-1].levelno == logging.DEBUG      # a trivial step stays quiet


def test_a_step_that_raises_is_logged_and_still_raises(caplog):
    caplog.set_level(logging.DEBUG, logger='imswitch')
    with pytest.raises(RuntimeError):
        with gd.timedStep('scan stop', 'the bad step'):
            raise RuntimeError('boom')
    assert '(raised)' in caplog.records[-1].getMessage()
