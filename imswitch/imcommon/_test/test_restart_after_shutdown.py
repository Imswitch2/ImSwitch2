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


# ── the interpreter path, and the quotes that broke the second restart ────
# On the rig: Pick setup -> restart worked; then the config editor -> restart
# died with FileNotFoundError. The first exec passed argv[0] as '"…/python"',
# quotes included; the restarted Python took that as its sys.executable, a
# path that does not exist, and the second exec could not find it.
def test_posix_argv0_is_the_bare_interpreter_path():
    with patch.object(ostools.sys, 'platform', 'darwin'), \
            patch.object(ostools.sys, 'argv', ['imswitch']), \
            patch.object(ostools.os, 'execv') as execv:
        ostools.restartSoftware()

    executable, argv = execv.call_args.args
    assert executable == sys.executable
    assert argv[0] == sys.executable
    assert not any('"' in arg for arg in argv)


def test_windows_quotes_every_argument_with_a_space_and_nothing_else():
    with patch.object(ostools.sys, 'platform', 'win32'), \
            patch.object(ostools.sys, 'executable', r'C:\Program Files\Python\python.exe'), \
            patch.object(ostools.os.path, 'exists', return_value=True), \
            patch.object(ostools.sys, 'argv', ['imswitch', '--config', r'C:\My Setups\rig.json', '--debug']), \
            patch.object(ostools.os, 'execv') as execv:
        ostools.restartSoftware()

    executable, argv = execv.call_args.args
    assert executable == r'C:\Program Files\Python\python.exe'
    assert argv == ['"C:\\Program Files\\Python\\python.exe"', '-m', 'imswitch',
                    '--config', '"C:\\My Setups\\rig.json"', '--debug']


def test_an_executable_left_quoted_by_an_old_restart_is_unquoted(tmp_path):
    """A process the old code restarted carries the quoted path; its next
    restart must still find the interpreter."""
    real = tmp_path / 'python'
    real.write_text('')
    inherited = str(tmp_path) + '/"' + str(real) + '"'  # what the exec'd Python computed
    with patch.object(ostools.sys, 'executable', inherited), \
            patch.object(ostools.sys, 'platform', 'darwin'), \
            patch.object(ostools.sys, 'argv', ['imswitch']), \
            patch.object(ostools.os, 'execv') as execv:
        ostools.restartSoftware()

    executable, argv = execv.call_args.args
    assert executable == str(real) and argv[0] == str(real)


def test_a_missing_interpreter_is_passed_through_so_the_error_names_it():
    with patch.object(ostools.sys, 'executable', '/nowhere/python'), \
            patch.object(ostools.os, 'execv') as execv:
        ostools.restartSoftware()
    assert execv.call_args.args[0] == '/nowhere/python'


def test_a_failed_exec_exits_cleanly_instead_of_crashing():
    """The rig saw the FileNotFoundError escape launchApp: no restart, no exit."""
    log = []
    app = Mock()
    app.exec_.return_value = 0

    def _failing_restart(module='imswitch'):
        log.append('restart-attempt')
        raise FileNotFoundError(2, 'No such file or directory')

    def _exit(code):
        log.append(f'sys.exit:{code}')
        raise _NeverReturns

    ostools.requestRestart()
    with patch.object(applaunch, 'shutdownModules', lambda *a, **k: log.append('shutdown')), \
            patch.object(applaunch.ostools, 'restartSoftware', _failing_restart), \
            patch.object(applaunch.sys, 'exit', _exit), \
            patch.object(shutdownState, 'hardwareFinalizationAllowed', return_value=True):
        with pytest.raises(_NeverReturns):
            applaunch.launchApp(app, Mock(), [])
    assert log == ['shutdown', 'restart-attempt', 'sys.exit:0']
