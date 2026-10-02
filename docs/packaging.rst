*********
Packaging
*********

ImSwitch2 ships through two channels:

* the **Python package** on PyPI (``pip install imswitch2``), which is the
  supported path for rigs and for anyone who needs vendor SDKs, and
* **standalone bundles** for Windows and macOS, built with PyInstaller, for
  people who should not have to own a Python environment.

This page covers the second.  For installing ImSwitch2 normally, see
:doc:`installation`; for publishing to PyPI, see the header comment of
``.github/workflows/imswitch-pypi.yml``.

Publishing a GitHub release builds both bundles and attaches them to it, in
parallel with the PyPI upload.  Nothing extra to run by hand.

.. note::

   The macOS bundle is built and verified end to end on Apple Silicon (see
   `Verifying a bundle`_).  The Windows path shares the same spec and the same
   self-test but has not yet been run on a Windows machine.


Naming
======

The installed artifacts are called **ImSwitch2** -- ``ImSwitch2.exe``,
``ImSwitch2.app``, ``Program Files\\ImSwitch2`` -- with their own Inno Setup
``AppId`` and their own macOS bundle identifier
(``io.github.imswitch2.imswitch2``).

That is deliberate.  Users of this fork may well also have a bundle of the
upstream ImSwitch installed, and sharing an install directory, a Start Menu
entry, an uninstaller registration or a bundle identifier with it would make
the two fight.  The console script (``imswitch``) and the in-app window title
are unchanged.


What a bundle can and cannot do
===============================

A frozen application has no ``site-packages`` and no ``pip``.  Everything it
can ever import was decided at build time.  That has consequences worth being
explicit about, because each one looks like a bug when you hit it:

**Device plugins installed with pip are invisible.**
    :func:`~imswitch.imcontrol.model.plugins.discovery.discover_contributions`
    enumerates the ``imswitch.manifest`` entry point group through
    ``importlib.metadata``.  In a bundle, only distributions whose metadata was
    copied in at build time exist.  A bundled ImSwitch2 therefore supports
    exactly the device plugins that were installed in the build environment.

**Scripts can only import what was bundled.**
    ``imscripting`` executes arbitrary user Python.  In a source install a user
    can ``pip install`` anything and use it from a script; in a bundle the
    module set is fixed.

**Vendor SDKs are still out of band.**
    ``thorlabs_tsi_sdk``, ``nidaqmx``, ``imagingcontrol4`` and friends need
    system-wide drivers and, in several cases, a manual SDK install.  The
    bundle cannot carry those.  A bundled ImSwitch2 is for a machine whose
    drivers are already in place -- or for the analysis modules, which need
    none.

**ImProcess drop-in plugins still work.**
    Those are loaded by file path out of ``ImSwitchConfig/improcess_plugins``,
    not through entry points, so they behave the same way frozen.  They can
    only import modules the bundle already contains.

User configuration is unaffected: it lives in
``%USERPROFILE%\\Documents\\ImSwitchConfig`` (Windows) or ``~/ImSwitchConfig``,
never inside the installation directory, so a bundle installs cleanly into
``Program Files`` or ``/Applications`` and an uninstall leaves setup files
alone.


Building on Windows
===================

Requirements: 64-bit Python 3.10+ on ``PATH`` and `Inno Setup 6
<https://jrsoftware.org/isdl.php>`_ at its default location.  Both are checked
before the build starts.

.. code-block:: bat

   release\windows\create_installer_windows.bat

Outputs:

* ``build\windows\dist\ImSwitch2\`` -- the unpacked bundle
* ``build\windows\ImSwitch2-<version>-win64-setup.exe`` -- the installer


Building on macOS
=================

Requirements: macOS 11+ and Python 3.10+.  Nothing else -- the image is built
with ``hdiutil`` from the base system.  Installing ``create-dmg``
(``brew install create-dmg``) is optional and only buys a laid-out window with
an *Applications* drop target.

.. code-block:: bash

   bash release/macos/create_macos_dmg.sh

Outputs ``build/macos/dist/ImSwitch2.app`` and
``build/macos/ImSwitch2-<version>-macOS-<arch>.dmg``.

The build is **not universal** -- it produces a bundle for the architecture of
the machine that built it.  Build on Apple Silicon unless you specifically need
an Intel bundle.

Two knobs:

``PYTHON=/path/to/python3.10``
    Which interpreter the build environment is created from.  Worth setting:
    the ``python3`` on ``PATH`` is often not the version the project targets,
    and it decides the Python the bundle ships.

``IMSWITCH_SKIP_DMG=1``
    Stop after the ``.app``, skip imaging it.


Why the scripts build a wheel
=============================

Both scripts create a throwaway virtualenv, build a wheel and install *that*,
rather than running PyInstaller in your development environment.  Three
reasons, in order of how much trouble they cause:

#. **Distribution metadata.**  An editable install registers itself through an
   import hook, and PyInstaller's ``copy_metadata`` cannot collect what is not
   a real ``.dist-info``.  Without it the bundle has no ``imswitch2`` metadata,
   so the update check and the device plugin registry both come up empty.
#. **Module graph.**  PyInstaller resolves imports by inspection, and the
   editable-install finder is one more indirection it handles poorly.
#. **Size.**  PyInstaller collects what it finds.  A development environment
   carries pytest, sphinx, debugpy, jupyter and whatever a past experiment left
   behind.

They build it with plain ``python -m build`` rather than ``python -m build
--wheel``, which matters more than it looks.  ``--wheel`` builds in-tree and
reuses ``<repo>/build/lib``, so **a module deleted since the last in-tree build
comes back from the dead** -- a stale ``build/lib`` here resurrected five
managers and a controller that no longer exist.  Plain ``python -m build``
makes an sdist first and builds the wheel from that, unpacked in a temp
directory.  ``imswitch-pypi.yml`` does the same, which is why what ships to
PyPI is a faithful snapshot of its tagged commit.

.. warning::

   Those resurrected modules do not break the build -- PyInstaller logs them as
   unresolvable hidden imports among thousands of lines of output and carries
   on.  If you ever build a wheel by hand to inspect or ship, use
   ``python -m build``, or delete ``build/lib`` first.

To build by hand anyway, from an environment with ImSwitch2 and PyInstaller
installed:

.. code-block:: bash

   pyinstaller imswitch.spec --noconfirm --clean

``IMSWITCH_BUNDLE_CONSOLE=0`` builds without a console window on Windows.  The
default keeps it, because "read me the last line in the black window" is a
workable remote support step for a rig, and ImSwitch2 logs hardware faults
there.

.. note::

   ``.gitignore`` carries a blanket ``*.spec`` from the standard Python
   template.  ``imswitch.spec`` is tracked and so unaffected, but a *new* spec
   file will be silently ignored until you ``git add -f`` it.


Verifying a bundle
==================

Building successfully proves very little -- the interesting failures are the
quiet ones, where the app starts but napari has no plugins.  Every bundle
carries a self-test, and both build scripts and CI run it:

.. code-block:: bash

   ./dist/ImSwitch2/ImSwitch2 --bundle-selftest        # macOS / Linux
   dist\ImSwitch2\ImSwitch2.exe --bundle-selftest      # Windows

A passing run::

   ImSwitch2 0.2.0 bundle self-test
     frozen=True  prefix=.../dist/ImSwitch2/_internal
   ok    bundle flag
   ok    own distribution metadata (imswitch2 0.2.0)
   ok    data files (18 default setups)
   ok    module packages (imcontrol, improcess, imscripting)
   ok    npe2 plugin discovery (napari, napari-console, napari-svg)
   ok    vispy backend (PyQt5)
   ok    napari viewer (1 layer)
   ok    no self-respawn (1 child process(es) after forcing a tracker spawn, stable)

   all checks passed

Every check runs; the process exits non-zero if any failed.

The viewer check needs a real GL context.  Under ``QT_QPA_PLATFORM=offscreen``
Qt cannot provide one and the process segfaults, so the self-test reports that
check as skipped instead of attempting it -- which is why CI verifies
everything *except* the viewer.  To cover it, run the self-test on a machine
with a display and no ``QT_QPA_PLATFORM`` override.

The flag is defined in ``release/pyinstaller/imswitch_bundle_entry.py``, which
is only part of the bundle -- ``pip install imswitch2`` does not grow a
``--bundle-selftest`` option.


The frozen-multiprocessing trap
===============================

A frozen build **must** call ``multiprocessing.freeze_support()`` before it does
anything else.  ``release/pyinstaller/imswitch_bundle_entry.py`` does, as its
first statement, and it has to stay there.

Why it matters here, concretely.  ``multiprocessing`` starts its resource
tracker by spawning ``sys.executable -c "from multiprocessing.resource_tracker
import main..."``.  In a frozen app ``sys.executable`` *is* the app, and the
PyInstaller bootloader ignores ``-c`` -- so that spawn starts a second
ImSwitch2.  The second copy imports zarr, hence ``numcodecs``, which creates a
``multiprocessing.Lock()`` at import time; registering that semaphore starts a
resource tracker, which spawns a third ImSwitch2.  A new window every couple of
seconds, each process parented by the last, until they are killed by hand.

PyInstaller's own multiprocessing runtime hook already knows how to divert that
child -- it recognises the command, ``exec()``\ s it and exits.  But it does so
from inside ``multiprocessing.freeze_support()``, which the hook *replaces* and
which somebody still has to call.  Leave the call out and the hook never runs.

Two things worth knowing if you meet this again:

* CPython's own ``freeze_support()`` is a no-op off Windows, so the name
  suggests it is not needed on macOS or Linux.  It is: what does the work in a
  frozen build is PyInstaller's replacement, on every platform.
* The trigger is a *transitive* dependency.  Nothing in ImSwitch calls
  ``multiprocessing``; ``numcodecs`` does, at import, and ``zarr`` needs
  ``numcodecs``.  Auditing our own code for ``multiprocessing`` use would not
  have found it.

The ``no self-respawn`` self-test check exists for exactly this: it counts child
processes, waits, and counts again, failing on growth or on more than the one
legitimate resource tracker.  The ``module packages`` check sits before it to
make sure the imports that trigger the spawn have actually happened --
``imswitch.__main__`` alone does not reach zarr, which is how this got past the
self-test the first time.


Size
====

An Apple Silicon bundle measures about **380 MB** unpacked.  PyQt5, scipy,
scikit-image and napari account for most of it; there is no meaningful trimming
left that does not cost a feature.

The spec drops every ``.dll`` from non-Windows builds, which saves 68 MB: the
MoNaLISA GPU reconstruction library and its CUDA runtime, OpenCV 3.4, and the
Hamamatsu SLM libraries.  None of them can load outside Windows.


Code signing
============

Neither bundle is signed today.

**Windows.**  The installer is unsigned, so SmartScreen shows an
"unrecognized app" warning on download; users click *More info* then *Run
anyway*.  Removing that needs an OV code-signing certificate (roughly
EUR 300-500/year), which since 2023 has to live on a hardware token or a cloud
HSM -- which in turn means signing cannot happen on a plain CI runner without
extra setup.

**macOS.**  The ``.app`` is ad-hoc signed (``codesign --sign -``).  That is
enough for the loader to accept the binary on Apple Silicon, but not for
Gatekeeper: on first launch users must right-click the app and choose *Open*,
or run

.. code-block:: bash

   xattr -dr com.apple.quarantine /Applications/ImSwitch2.app

Removing that needs an Apple Developer Program membership (USD 99/year), a
Developer ID certificate, and notarisation of every build.

Both are money-and-process decisions rather than technical ones.  Picasso, the
closest comparable project, ships unsigned on Windows and ad-hoc signed on
macOS, and documents the bypass.


Continuous integration
======================

``.github/workflows/imswitch-bundle.yml`` builds both bundles on
``windows-latest`` and ``macos-14``, runs the self-test, and on a published
release attaches the ``.exe`` and ``.dmg`` to it with the run's own
``GITHUB_TOKEN``.  ``workflow_dispatch`` builds without touching any release,
which is how to test a change to the spec or to either build script.

It is a separate workflow from ``imswitch-pypi.yml`` on purpose: the two fail
for unrelated reasons, and a broken bundle should not block a PyPI upload or
the reverse.

Like the publish workflow, a release runs this file **as it exists at the
tagged commit** -- so changes here have to be merged before tagging.
