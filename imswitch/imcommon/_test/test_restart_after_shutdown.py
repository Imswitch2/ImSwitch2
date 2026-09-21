"""Restarting ImSwitch without leaving the hardware where it stood.

``ostools.restartSoftware`` replaces the process image on the spot, so nothing
is finalized: detectors keep their acquisition, a laser stays at whatever power
it was emitting, and the new process inherits all of it. ``requestRestart``
instead defers the re-exec to ``launchApp``, which runs it only after the
modules have shut down.
"""
import sys
from unittest.mock import Mock, patch

import pytest

from imswitch.imcommon import applaunch
from imswitch.imcommon.model import ostools, shutdownState


@pytest.fixture(autouse=True)
def _no_restart_left_armed():
    ostools.cancelRestart()
    yield
    ostools.cancelRestart()


class _NeverReturns(BaseException):
    """Stands in for a call that ends the process: execv, os._exit, sys.exit."""


def _run_launch_app(*, finalizationAllowed=True):
    """Drive launchApp with everything below it stubbed out.

    Returns the ordered log of what happened, so that the ordering the feature
    depends on is what is actually asserted. The three ways out of the function
    do not return in production, and the stubs do not either -- otherwise the
    test sees a fall-through the real thing never reaches.
    """
    log = []
    app = Mock()
    app.exec_.return_value = 0
    mainView = Mock()

    def _shutdown(controllers, logger=None):
        log.append('shutdown')

    def _record(label):
        def stub(arg='imswitch'):
            log.append(f'{label}:{arg}')
            raise _NeverReturns
        return stub

    with patch.object(applaunch, 'shutdownModules', _shutdown), \
            patch.object(applaunch.ostools, 'restartSoftware', _record('restart')), \
            patch.object(applaunch.sys, 'exit', _record('sys.exit')), \
            patch.object(applaunch.os, '_exit', _record('os._exit')), \
            patch.object(shutdownState, 'hardwareFinalizationAllowed',
                         return_value=finalizationAllowed):
        with pytest.raises(_NeverReturns):
            applaunch.launchApp(app, mainView, [])
    return log


def test_without_a_request_the_app_just_exits():
    assert _run_launch_app() == ['shutdown', 'sys.exit:0']


def test_the_restart_happens_after_the_modules_have_shut_down():
    ostools.requestRestart()
    assert _run_launch_app() == ['shutdown', 'restart:imswitch']


def test_a_stuck_script_still_restarts_rather_than_only_exiting():
    """execv drops the stuck thread exactly as os._exit would."""
    ostools.requestRestart()
    log = _run_launch_app(finalizationAllowed=False)
    assert log == ['shutdown', 'restart:imswitch']


def test_a_stuck_script_without_a_request_still_hard_exits():
    log = _run_launch_app(finalizationAllowed=False)
    assert log == ['shutdown', 'os._exit:1']


# ── Request-and-close, the one way anything restarts ──────────────────────
def test_request_and_close_arms_the_restart():
    closed = []
    ostools.restartAfterShutdown(lambda: closed.append(True) or True)

    assert closed, 'the application was never asked to close'
    assert ostools.restartRequested() == 'imswitch'


def test_a_vetoed_close_withdraws_the_request():
    """Nothing is restarting, so nothing must stay armed for the next close."""
    ostools.restartAfterShutdown(lambda: False)
    assert ostools.restartRequested() is None


def test_a_close_that_reports_nothing_is_not_read_as_a_veto():
    """Only an explicit False is a veto; a window returning None is not."""
    ostools.restartAfterShutdown(lambda: None)
    assert ostools.restartRequested() == 'imswitch'


# ── The request itself ────────────────────────────────────────────────────
def test_a_request_can_be_withdrawn():
    ostools.requestRestart()
    ostools.cancelRestart()
    assert ostools.restartRequested() is None


def test_the_command_line_is_carried_across_the_restart():
    """A session started with --debug or --scale must come back the same."""
    with patch.object(ostools.sys, 'argv', ['imswitch', '--debug', '--scale', '0.8']), \
            patch.object(ostools.os, 'execv') as execv:
        ostools.restartSoftware()

    argv = execv.call_args.args[1]
    assert argv[-3:] == ['--debug', '--scale', '0.8']
    assert '-m' in argv and 'imswitch' in argv


def test_a_frozen_build_re_execs_itself_rather_than_python_dash_m():
    with patch.object(ostools.sys, 'frozen', True, create=True), \
            patch.object(ostools.sys, 'argv', ['ImSwitch.exe', '--debug']), \
            patch.object(ostools.sys, 'executable', '/Applications/ImSwitch.app/ImSwitch'), \
            patch.object(ostools.os, 'execv') as execv:
        ostools.restartSoftware()

    executable, argv = execv.call_args.args
    assert argv == [executable, '--debug']
    assert '-m' not in argv
