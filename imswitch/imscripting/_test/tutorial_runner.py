"""Run one scripting tutorial against a hardware setup, headless.

    python -m imswitch.imscripting._test.tutorial_runner SETUP SCRIPT [--timeout S]

SETUP is a file name from ``_data/user_defaults/imcontrol_setups`` or a path.
ImControl is built from it offscreen, the script gets the scope the Scripting
tab gives it (``api``, ``controllers``, ``mainWindow`` and the actions such as
``getLogger`` and ``runScanAndWait``), and it runs on ImScripting's own
executor thread while this thread pumps the Qt events it needs. The last line
printed is a JSON summary; the exit code is 0 only when the script succeeded.

One process per script: ImControl's managers are not built to be booted twice
in one interpreter, and a hung script must not take the next one with it.
``HOME`` is always a scratch directory (``--home``, or a new temporary one):
the user-files folder, ``~/ImSwitchConfig`` with the recordings, is created
under it, and a real one must never be read -- its saved widget states would
be applied to the setup, and its warnings arrive as modal boxes.
"""

import argparse
import faulthandler
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def _setupPath(setup):
    path = Path(setup)
    if path.is_absolute() or path.exists():
        return path
    import imswitch

    return Path(imswitch.__file__).parent / '_data' / 'user_defaults' / 'imcontrol_setups' / setup


class _MainWindowStub:
    """What a script may ask of the window: switching the visible module."""

    def __init__(self):
        self.currentModule = None

    def setCurrentModule(self, moduleId):
        self.currentModule = moduleId


def runTutorial(setup, script, timeout=120.0):
    """Boot ImControl on ``setup``, run ``script``, return a result dict."""
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

    # Only the napari/vispy canvas needs replacing: it cannot get an OpenGL
    # context offscreen and takes the interpreter down trying.
    from imswitch.test_no_hardware_ui_smoke import _install_gui_dependency_stubs

    _install_gui_dependency_stubs(matplotlib=False)

    from imswitch import imcontrol
    from imswitch.imcommon import prepareApp
    from imswitch.imcommon.controller import ModuleCommunicationChannel
    from imswitch.imcommon.model import pythontools
    from imswitch.imcontrol.controller.ImConMainController import ImConMainController
    from imswitch.imcontrol.model import Options
    from imswitch.imcontrol.view import ViewSetupInfo
    from imswitch.imscripting.model.ScriptExecutor import ScriptExecutor

    # Modal boxes nobody can answer offscreen: "Save the current widget
    # state?" on close, and restore warnings on startup.
    ImConMainController._shouldSaveWidgetStateOnClose = lambda self: False
    ImConMainController._showWidgetStateRestoreWarnings = lambda self, *a, **k: None

    setupPath = _setupPath(setup)
    scriptPath = Path(script).resolve()
    app = prepareApp()
    moduleCommChannel = ModuleCommunicationChannel()
    moduleCommChannel.register(imcontrol)
    view, controller = imcontrol.getMainViewAndController(
        moduleCommChannel,
        overrideSetupInfo=ViewSetupInfo.from_json(setupPath.read_text(), infer_missing=True),
        overrideOptions=Options(setupFileName=setupPath.name),
    )
    app.processEvents()

    scope = {
        'moduleCommChannel': moduleCommChannel,
        'mainWindow': _MainWindowStub(),
        'controllers': pythontools.dictToROClass({'imcontrol': controller}),
        'api': pythontools.dictToROClass({'imcontrol': controller.api}),
    }
    executor = ScriptExecutor(scope)
    started = time.monotonic()
    result = executor.execute(str(scriptPath), scriptPath.read_text())
    while result.status.value == 'running' and time.monotonic() - started < timeout:
        app.processEvents()
        time.sleep(0.01)
    timedOut = result.status.value == 'running'
    if timedOut:
        executor.cancel()
        deadline = time.monotonic() + 10
        while result.status.value == 'running' and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
    # Let queued cross-thread work (writer terminals, UI resets) land.
    for _ in range(20):
        app.processEvents()
        time.sleep(0.01)

    summary = {
        'status': 'timeout' if timedOut else result.status.value,
        'seconds': round(time.monotonic() - started, 2),
        'error': result.error,
        'currentModule': scope['mainWindow'].currentModule,
    }
    try:
        executor.shutdown(3000)
        view.close()
        app.processEvents()
    except Exception as error:  # A teardown hiccup must not hide the result.
        summary['teardownError'] = repr(error)
    return summary, result.stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('setup')
    parser.add_argument('script')
    parser.add_argument('--timeout', type=float, default=120.0)
    parser.add_argument('--home', help='scratch HOME (default: a new temporary one)')
    args = parser.parse_args(argv)

    if os.name == 'nt':
        # dirtools asks Windows for the Documents folder; HOME cannot move it.
        sys.exit('tutorial_runner: not supported on Windows -- it cannot keep '
                 'away from the real Documents\\ImSwitchConfig there.')
    # Before anything imports imswitch: its user-files path is fixed at import.
    os.environ['HOME'] = args.home or tempfile.mkdtemp(prefix='imswitch-tutorial-')

    # A wedged Qt teardown must still end the process, with a stack to read.
    faulthandler.dump_traceback_later(args.timeout + 60, exit=True)
    summary, stdout = runTutorial(args.setup, args.script, args.timeout)
    print('----- script output -----')
    print(stdout)
    print(json.dumps(summary))
    sys.stdout.flush()
    # os._exit: ImControl's worker threads can keep a normal exit waiting.
    os._exit(0 if summary['status'] == 'succeeded' else 1)


if __name__ == '__main__':
    main()
