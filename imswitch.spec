# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone ImSwitch2 bundles (Windows and macOS).

Build it through the wrapper scripts, which set up a clean environment first:

    release/windows/create_installer_windows.bat   -> dist/ImSwitch2/ + Setup .exe
    release/macos/create_macos_dmg.sh              -> dist/ImSwitch2.app + .dmg

or directly, from a checkout with ImSwitch2 installed (``pip install -e .``):

    pyinstaller imswitch.spec --noconfirm

See ``docs/packaging.rst`` for the full procedure and for what a bundle can and
cannot do (a frozen app has no site-packages, so pip-installed device plugins
and arbitrary imports in user scripts are not available).

The installed artifacts are named ``ImSwitch2``, not ``ImSwitch``, so they can
sit beside a bundle of the upstream project without fighting it over the
install directory, the Start Menu entry or the macOS bundle identifier.  The
console script and the in-app window title are still ``imswitch``/``ImSwitch``.

Notes on the differences from the ImSwitch 1.x spec this replaces:

* ``matplotlib`` is no longer excluded.  ``imcommon/view/guitools/naparitools``
  imports it at module scope and ~15 widgets import naparitools, so the old
  exclusion would abort the bundle on the first widget import.
* napari's plugin engine (npe2) discovers contributions from *distribution
  metadata*, which PyInstaller drops by default.  Without the ``copy_metadata``
  calls below the viewer starts but registers no plugins, which shows up as
  missing layer controls and a dead console rather than as an error.
* The Windows CRT DLLs (``api-ms-*``, ``vcruntime*``, ``msvcp*``, ...) are no
  longer stripped.  Stripping them saves ~2 MB and makes the bundle depend on
  the target machine having a matching VC++ redistributable -- a bad trade for
  rig PCs that are often freshly imaged.
* UPX is off.  It corrupts Qt plugin DLLs often enough to be a known support
  burden, and UPX-packed executables are a common antivirus false positive.
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

# Analysis, PYZ, EXE, COLLECT and BUNDLE are injected into the spec namespace by
# PyInstaller itself; importing them from PyInstaller.building.* is what broke
# the 1.x spec across PyInstaller majors, so don't.

IS_WINDOWS = sys.platform == 'win32'
IS_MACOS = sys.platform == 'darwin'

APP_NAME = 'ImSwitch2'
# Reverse-DNS, and stable across releases: macOS keys window state, permissions
# and Launch Services entries off it.  github.io form rather than a bare domain
# because the project does not own imswitch2.org.
BUNDLE_ID = 'io.github.imswitch2.imswitch2'

PROJECT_ROOT = Path(SPECPATH).resolve()  # noqa: F821  (SPECPATH is injected)
ENTRY_SCRIPT = str(PROJECT_ROOT / 'release' / 'pyinstaller' / 'imswitch_bundle_entry.py')

# Read straight from the source tree so the bundle cannot drift from the
# checkout it was built out of.  Same trick the publish workflow uses, and for
# the same reason: these are needed before the package is importable.
_ns = {}
exec((PROJECT_ROOT / 'imswitch' / '__init__.py').read_text(encoding='utf-8'), _ns)
VERSION = _ns['__version__']
DISTNAME = _ns['__distname__']  # `imswitch2`; the import package is `imswitch`

# A console window is genuinely useful on a rig: ImSwitch2 logs hardware faults
# there, and "read me the last line in the black window" is a workable remote
# support step.  Override with IMSWITCH_BUNDLE_CONSOLE=0 for a silent build.
CONSOLE = os.environ.get('IMSWITCH_BUNDLE_CONSOLE', '1') != '0' and not IS_MACOS


def _optional(collector, package, **kwargs):
    """Run a PyInstaller collector, tolerating a package that isn't installed.

    Optional extras (opencv, napari-storm, vendor SDKs) legitimately vary
    between build machines; a missing one should shrink the bundle, not fail
    the build.
    """
    try:
        return collector(package, **kwargs)
    except Exception as exc:  # pragma: no cover - build-time only
        print(f'[imswitch.spec] skipping {package}: {exc}')
        return []


# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------
# Packages with no hook in PyInstaller or pyinstaller-hooks-contrib, whose data
# files therefore have to be asked for by name.  (napari and vispy are the
# important ones: neither ships a hook, and both are pure breakage without
# their resources.)
DATA_PACKAGES = [
    'imswitch',
    'napari',
    # The "napari" distribution's own npe2 manifest lives in a *separate*
    # top-level package: its napari.manifest entry point resolves to
    # napari_builtins:builtins.yaml.  Collecting napari alone leaves npe2
    # unable to read it, which costs every built-in reader and writer.
    'napari_builtins',
    'napari_console',
    'napari_svg',
    'napari_storm',   # the optional `storm` extra; absent unless installed
    'npe2',
    'vispy',
    'magicgui',
    'app_model',
    'superqt',
    'qtawesome',    # icon fonts
    'qdarkstyle',   # .qss stylesheets and rc resources
    'colour',
    'ome_zarr',
]

datas = []
for package in DATA_PACKAGES:
    datas += _optional(collect_data_files, package)

# The source tree carries ~65 MB of Windows-only native libraries: the MoNaLISA
# GPU reconstruction DLL with its CUDA runtime and OpenCV 3.4 under
# imswitch/_data/libs, plus the Hamamatsu SLM DLLs under imcontrol/model/
# interfaces.  A .dll cannot be loaded on macOS or Linux under any
# circumstances, so this is a third of the download for code that can never run.
if not IS_WINDOWS:
    _kept, _dropped = [], []
    for entry in datas:
        (_dropped if entry[0].lower().endswith('.dll') else _kept).append(entry)
    datas = _kept
    _mb = sum(os.path.getsize(entry[0]) for entry in _dropped) / 1e6
    print(f'[imswitch.spec] excluded {len(_dropped)} Windows-only DLLs ({_mb:.0f} MB)')

# ---------------------------------------------------------------------------
# Distribution metadata
# ---------------------------------------------------------------------------
# npe2 enumerates entry points in group "napari.manifest"; ImSwitch2's own
# device plugin discovery does the same for "imswitch.manifest".  Both read
# importlib.metadata, which needs the .dist-info directories copied in.
METADATA_PACKAGES = [
    DISTNAME,
    'napari',
    'npe2',
    'napari-console',
    'napari-svg',
    'napari-plugin-engine',
    'napari-storm',
    'magicgui',
    'app-model',
    'superqt',
    'vispy',
    'imageio',
]
for package in METADATA_PACKAGES:
    datas += _optional(copy_metadata, package)

# ---------------------------------------------------------------------------
# Hidden imports
# ---------------------------------------------------------------------------
# ImSwitch2 resolves managers, widgets and controllers by name at runtime, so
# static analysis sees almost none of imcontrol's model layer.
hiddenimports = collect_submodules('imswitch')

# napari leans on lazy_loader and deferred imports throughout; the module graph
# that PyInstaller can see statically is a small fraction of what it uses.
for package in ('napari', 'napari_builtins', 'napari_console', 'napari_svg',
                'napari_storm', 'npe2', 'magicgui', 'app_model'):
    hiddenimports += _optional(collect_submodules, package)

# vispy picks its backend at runtime by trying imports in order, so none of them
# are reachable statically.  (The 1.x spec also listed vispy.ext._bundled.six,
# which vispy dropped years ago; PyInstaller reports it as a missing hidden
# import, which is noise in an already noisy build log.)
try:
    from vispy.app.backends import CORE_BACKENDS

    hiddenimports += ['vispy.app.backends.' + backend[1] for backend in CORE_BACKENDS]
    hiddenimports += ['vispy.app.backends._test']
except Exception as exc:  # pragma: no cover - build-time only
    print(f'[imswitch.spec] could not enumerate vispy backends: {exc}')

hiddenimports += collect_submodules('pyqtgraph.console')
hiddenimports += [
    'matplotlib.backends.backend_qt5agg',
    'PyQt5.Qsci',          # imscripting's editor; imported through qtpy
    'Pyro5.serializers',   # picked by name from the Pyro5 config
]

# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------
# Development-only packages that are commonly present in a working environment.
# Deliberately conservative: anything napari's console needs (IPython, jedi,
# qtconsole, pygments) has to stay.
excludes = [
    'pytest',
    '_pytest',
    'debugpy',
    'cmake',
    'sphinx',
    'sphinx_rtd_theme',
]

a = Analysis(
    [ENTRY_SCRIPT],
    # No `pathex=[PROJECT_ROOT]`.  It would put the source checkout ahead of
    # site-packages in PyInstaller's search path, so the bundle would contain
    # the working tree rather than the wheel that was just installed -- and
    # which half it got would depend on where the build was invoked from.
    # PROJECT_ROOT is still used above, but only to *read* files.
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    icon=str(PROJECT_ROOT / 'imswitch' / '_data' / 'icon.ico') if IS_WINDOWS else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

if IS_MACOS:
    _icns = PROJECT_ROOT / 'release' / 'macos' / 'ImSwitch2.icns'

    # A reverse-DNS bundle identifier is not cosmetic: Qt asks CoreFoundation
    # for the main bundle during static initialisation, and a bundle that does
    # not present a well-formed identifier can hand back NULL and take the
    # process down before any ImSwitch2 code runs.  The 1.x spec passed None
    # here, which is exactly that failure mode.
    app = BUNDLE(  # noqa: F821
        coll,
        name=f'{APP_NAME}.app',
        icon=str(_icns) if _icns.is_file() else None,
        bundle_identifier=BUNDLE_ID,
        version=VERSION,
        info_plist={
            'CFBundleShortVersionString': VERSION,
            'CFBundleVersion': VERSION,
            'NSPrincipalClass': 'NSApplication',
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '11.0',
            # Suppresses "Python" in the menu bar and the dock.
            'CFBundleName': APP_NAME,
            'CFBundleDisplayName': APP_NAME,
        },
    )
