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

import os
import sys

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
    check('npe2 plugin discovery', _npe2_plugins)
    check('vispy backend', _vispy_backend)
    # Building a viewer needs a real GL context.  Under the offscreen platform
    # Qt reports "QOpenGLWidget is not supported on this platform" and the
    # process segfaults -- not a bundle defect, so don't attempt it there.
    # Run with QT_QPA_PLATFORM=cocoa (or windows) on a machine with a display
    # to exercise it.
    check('napari viewer', _napari_viewer, needs_gl=True)

    if failures:
        say(f'\n{len(failures)} check(s) failed')
        return 1
    say('\nall checks passed')
    return 0


if __name__ == '__main__':
    if '--bundle-selftest' in sys.argv:
        sys.exit(_selftest())
    imswitch.__main__.main()
