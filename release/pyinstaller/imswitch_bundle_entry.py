"""Entry point used *only* by the PyInstaller bundles.

Kept as a tracked file rather than generated into a temp directory at build
time (which is what the ImSwitch 1.x spec did): PyInstaller's module graph is
easier to reason about when its root script is a real, reviewable file, and a
build failure points at a path that still exists afterwards.

Two things this adds over ``imswitch.__main__``:

* the ``IMSWITCH_IS_BUNDLE`` marker, which tells the update check to look at
  GitHub releases instead of PyPI (see ``CheckUpdatesController``);
* ``--bundle-selftest``, which checks the handful of things that freezing
  silently breaks.  It lives here rather than in ``imswitch.__main__`` because
  it is build tooling, not a product feature -- the wheel has no use for it.
"""

import multiprocessing
import os
import sys

# FIRST, before ImSwitch is imported and before anything else runs.
#
# multiprocessing starts its resource tracker by spawning
# ``sys.executable -c "from multiprocessing.resource_tracker import main..."``.
# In a frozen app ``sys.executable`` *is* the app and the bootloader ignores
# ``-c``, so that spawn starts a second ImSwitch2 -- which imports zarr, hence
# numcodecs, which creates a ``multiprocessing.Lock()`` at import time, which
# starts a resource tracker, which spawns a third ImSwitch2.  The result is a
# new window every couple of seconds, each process parented by the last, until
# the user kills them all by hand.
#
# PyInstaller's multiprocessing runtime hook already knows how to divert that
# child: it recognises the command, exec()s it and exits.  But it does so from
# inside ``multiprocessing.freeze_support()``, which the hook *replaces* and
# which somebody still has to call.  This is that call.
#
# Nothing here is macOS-specific.  CPython's own ``freeze_support()`` is a no-op
# off Windows, so the name is misleading: what makes this work is PyInstaller's
# replacement, and a frozen build needs it on every platform.
multiprocessing.freeze_support()

os.environ['IMSWITCH_IS_BUNDLE'] = '1'

import imswitch  # noqa: E402  (must follow the env var)
import imswitch.__main__  # noqa: E402

#: npe2 plugins that ship as hard dependencies of napari.  If these are missing
#: from a bundle, napari's metadata was not collected and the viewer comes up
#: with no layer controls and a dead console -- a failure that looks like a UI
#: bug rather than a packaging one.
_REQUIRED_NPE2_PLUGINS = {'napari', 'napari-console', 'napari-svg'}


def _selftest():
    """Check what freezing typically breaks.  Returns a process exit code.

    Importing this module has already exercised the heavy part: ``__main__``
    pulls in the whole Qt/napari/vispy GUI stack at module scope, so a bundle
    with a missing hidden import never gets this far.  What is left to check is
    the things that fail *quietly*.
    """

    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    headless = os.environ['QT_QPA_PLATFORM'] in ('offscreen', 'minimal')
    failures = []

    def say(line):
        # Unbuffered: a check that takes the process down with it (an OpenGL
        # one can) would otherwise lose the whole report, which is precisely
        # the moment you need to know how far it got.
        print(line, flush=True)

    def check(label, fn, needs_gl=False):
        if needs_gl and headless:
            say(f'skip  {label} (needs a display; QT_QPA_PLATFORM='
                f'{os.environ["QT_QPA_PLATFORM"]})')
            return
        try:
            detail = fn()
        except Exception as exc:
            failures.append(f'{label}: {type(exc).__name__}: {exc}')
            say(f'FAIL  {label}: {type(exc).__name__}: {exc}')
        else:
            say(f'ok    {label}{f" ({detail})" if detail else ""}')

    def _bundle_flag():
        from imswitch.imcommon.model.dirtools import isBundle

        if not isBundle():
            raise RuntimeError('isBundle() is False inside a bundle')
        return None

    def _own_metadata():
        # The update check asks PyPI about __distname__ and the device plugin
        # registry enumerates the `imswitch.manifest` entry point group; both
        # go through importlib.metadata, which only sees distributions whose
        # .dist-info the spec copied in.  A bundle built from an *editable*
        # install silently lacks this, which is why the build scripts install
        # a wheel instead.
        import importlib.metadata as md

        version = md.version(imswitch.__distname__)
        if version != imswitch.__version__:
            raise RuntimeError(
                f'metadata says {imswitch.__distname__} {version}, '
                f'but imswitch.__version__ is {imswitch.__version__}'
            )
        return f'{imswitch.__distname__} {version}'

    def _data_files():
        from imswitch.imcommon.model.dirtools import DataFileDirs

        setups = os.path.join(DataFileDirs.UserDefaults, 'imcontrol_setups')
        count = len(os.listdir(setups))
        if count == 0:
            raise RuntimeError(f'no default setups under {setups}')
        return f'{count} default setups'

    def _module_packages():
        # main() imports the enabled module packages by name.  Doing the same
        # here reaches the real manager and widget trees -- and through the
        # recording managers, zarr and numcodecs.  Importing only
        # imswitch.__main__, which this module does at the top, does not: that
        # is how a fork bomb triggered by numcodecs at startup got past this
        # self-test once already.
        import importlib

        for module_id in ('imcontrol', 'improcess', 'imscripting'):
            importlib.import_module(f'imswitch.{module_id}')
        return 'imcontrol, improcess, imscripting'

    def _no_self_respawn():
        # The failure this guards against: multiprocessing starts a helper by
        # re-running the frozen executable, the bootloader ignores the -c it was
        # handed and starts the whole application instead, that copy imports
        # numcodecs and spawns another helper, and so on.  One legitimate
        # resource-tracker child is expected; growth is not.
        import time

        import psutil

        # Force the spawn rather than hoping something above caused it.
        # numcodecs creates a multiprocessing.Lock() at import time; registering
        # that semaphore is what starts the resource tracker.  zarr needs
        # numcodecs, which is why every ImSwitch2 build has it -- but none of the
        # module packages imported above pull it in, so without this line the
        # check watches an idle process and proves nothing.  (Measured: that is
        # exactly what it did on the first attempt.)
        import numcodecs  # noqa: F401

        me = psutil.Process()

        def describe(procs):
            out = []
            for proc in procs:
                try:
                    out.append(' '.join(proc.cmdline()[:4]) or proc.name())
                except psutil.Error:
                    out.append(f'pid {proc.pid} (gone)')
            return '; '.join(out) or 'none'

        before = me.children(recursive=True)
        time.sleep(2.0)
        after = me.children(recursive=True)
        if len(after) > len(before) or len(after) > 1:
            # Don't leave a bomb running behind a failed check.
            for proc in after:
                try:
                    proc.kill()
                except psutil.Error:
                    pass
            raise RuntimeError(
                f'child processes {len(before)} -> {len(after)}, expected at most one '
                f'resource tracker and no growth: {describe(after)}'
            )
        return f'{len(after)} child process(es) after forcing a tracker spawn, stable'

    def _npe2_plugins():
        # Entry-point discovery reads distribution metadata, which PyInstaller
        # drops by default.  This is the single most likely bundle-only defect.
        from npe2 import PluginManager

        pm = PluginManager.instance()
        pm.discover()
        found = {manifest.name for manifest in pm.iter_manifests()}
        missing = _REQUIRED_NPE2_PLUGINS - found
        if missing:
            raise RuntimeError(f'npe2 discovered {sorted(found)}, missing {sorted(missing)}')
        return ', '.join(sorted(found))

    def _vispy_backend():
        import vispy.app

        return vispy.app.use_app().backend_name

    def _napari_viewer():
        from imswitch.imcommon.view.guitools.naparitools import EmbeddedNapari

        viewer = EmbeddedNapari()
        try:
            import numpy as np

            viewer.add_image(np.zeros((8, 8), dtype='uint16'), name='selftest')
            return f'{len(viewer.layers)} layer'
        finally:
            viewer.close()

    say(f'ImSwitch2 {imswitch.__version__} bundle self-test')
    say(f'  frozen={getattr(sys, "frozen", False)}  prefix={sys.prefix}')
    check('bundle flag', _bundle_flag)
    check('own distribution metadata', _own_metadata)
    check('data files', _data_files)
    check('module packages', _module_packages)
    check('npe2 plugin discovery', _npe2_plugins)
    check('vispy backend', _vispy_backend)
    # Building a viewer needs a real GL context.  Under the offscreen platform
    # Qt reports "QOpenGLWidget is not supported on this platform" and the
    # process segfaults -- not a bundle defect, so don't attempt it there.
    # Run with QT_QPA_PLATFORM=cocoa (or windows) on a machine with a display
    # to exercise it.
    check('napari viewer', _napari_viewer, needs_gl=True)
    # Last, so every import above has had its chance to spawn something.
    check('no self-respawn', _no_self_respawn)

    if failures:
        say(f'\n{len(failures)} check(s) failed')
        return 1
    say('\nall checks passed')
    return 0


if __name__ == '__main__':
    if '--bundle-selftest' in sys.argv:
        sys.exit(_selftest())
    imswitch.__main__.main()
