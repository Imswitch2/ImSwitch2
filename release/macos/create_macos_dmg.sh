#!/usr/bin/env bash
# Build ImSwitch2.app and wrap it in a .dmg.
#
#   bash release/macos/create_macos_dmg.sh
#
# Requirements:
#   * macOS 11+.  The build is not universal: it produces a bundle for the
#     architecture of the machine that builds it.
#   * Python 3.10+.  Set PYTHON=/path/to/python3.x to choose which one; the
#     default `python3` may well be older or newer than the project targets.
#   * Nothing else.  `create-dmg` (brew install create-dmg) is used for a nicer
#     window layout when present, and `hdiutil` from the base system otherwise.
#
# Knobs:
#   PYTHON=...            interpreter to build the venv from (default python3)
#   IMSWITCH_SKIP_DMG=1   stop after the .app, skip imaging it
#
# The resulting .dmg is ad-hoc signed only, so Gatekeeper refuses it on first
# launch.  See docs/packaging.rst for what users have to do and for what
# notarising it properly would take.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_ROOT="$REPO_ROOT/build/macos"
VENV_DIR="$BUILD_ROOT/venv"
DIST_DIR="$BUILD_ROOT/dist"
WORK_DIR="$BUILD_ROOT/work"
STAGING_DIR="$BUILD_ROOT/staging"

APP_NAME="ImSwitch2"
APP_PATH="$DIST_DIR/$APP_NAME.app"
PYTHON="${PYTHON:-python3}"

# ---------------------------------------------------------------------------
# Step 0: check everything up front.
# ---------------------------------------------------------------------------
# A missing prerequisite that only surfaces after a ten-minute PyInstaller run
# is a bad way to learn about it.
if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: this script builds a macOS bundle; run it on macOS." >&2
    exit 1
fi
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "ERROR: $PYTHON not found.  Set PYTHON=/path/to/python3." >&2
    exit 1
fi
"$PYTHON" - <<'EOF' || exit 1
import sys
if sys.version_info < (3, 10):
    sys.exit(f'ERROR: Python 3.10+ required, got {sys.version.split()[0]}. Set PYTHON=...')
print(f'>>> Building with Python {sys.version.split()[0]}')
EOF

VERSION="$("$PYTHON" -c "
import pathlib
ns = {}
exec(pathlib.Path('$REPO_ROOT/imswitch/__init__.py').read_text(), ns)
print(ns['__version__'])
")"
DMG_BASENAME="$APP_NAME-$VERSION-macOS-$(uname -m)"

echo ">>> Building $APP_NAME $VERSION for $(uname -m)"

# ---------------------------------------------------------------------------
# Step 1: a throwaway build environment.
# ---------------------------------------------------------------------------
# Deliberately NOT the development environment.  A dev env carries pytest,
# sphinx, debugpy, jupyter and whatever a past experiment left behind, and
# PyInstaller will happily pull a good deal of it into the bundle.  Installing
# the built wheel (not `pip install -e .`) also avoids the editable-install
# import hook, which PyInstaller's module graph handles poorly, and it is the
# only way the bundle ends up with correct `imswitch2` distribution metadata.
echo ">>> Creating clean build environment in $VENV_DIR"
rm -rf "$BUILD_ROOT"
mkdir -p "$BUILD_ROOT"
"$PYTHON" -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip build "pyinstaller>=6.11"

echo ">>> Building wheel"
# setuptools keeps intermediates in <repo>/build/lib and reuses them, so a file
# deleted since the last in-tree build comes back from the dead.  Plain
# `python -m build` (no --wheel) sidesteps that: it makes an sdist first and
# builds the wheel from *that*, unpacked in a temp dir.  Same as the PyPI
# workflow, which is why what ships to PyPI is clean.  The removal below is
# belt and braces for whatever else may be sitting in there.
rm -rf "$REPO_ROOT/build/lib" "$REPO_ROOT"/build/bdist.*
python -m build --outdir "$BUILD_ROOT/wheel" "$REPO_ROOT"
python -m pip install "$BUILD_ROOT/wheel/"*.whl

# ---------------------------------------------------------------------------
# Step 2: freeze.
# ---------------------------------------------------------------------------
echo ">>> Running PyInstaller"
pyinstaller "$REPO_ROOT/imswitch.spec" \
    --noconfirm --clean \
    --distpath "$DIST_DIR" \
    --workpath "$WORK_DIR"

if [ ! -d "$APP_PATH" ]; then
    echo "ERROR: PyInstaller did not produce $APP_PATH" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 3: qt.conf.
# ---------------------------------------------------------------------------
# Pins Qt's plugin/library lookup to the bundle's own Resources directory
# instead of letting it ask CoreFoundation at load time.  Qt's static
# initialisers run before any of our code does, and when that lookup returns
# something unexpected the process dies with no traceback to work from.
cat > "$APP_PATH/Contents/Resources/qt.conf" <<'EOF'
[Paths]
Prefix = .
EOF
echo ">>> Wrote qt.conf"

# ---------------------------------------------------------------------------
# Step 4: HDF5 library collision.
# ---------------------------------------------------------------------------
# h5py ships its own libhdf5 dylibs.  If any other wheel in the environment
# ships a different HDF5 build, PyInstaller keeps whichever it collected first,
# and h5py then loads against the wrong ABI -- which surfaces much later as a
# corrupt-file error when someone opens a recording.  Force h5py's copies to win.
H5PY_DYLIBS="$(python -c "import h5py, os; print(os.path.join(os.path.dirname(h5py.__file__), '.dylibs'))" 2>/dev/null || true)"
if [ -n "$H5PY_DYLIBS" ] && [ -d "$H5PY_DYLIBS" ]; then
    FRAMEWORKS_DIR="$APP_PATH/Contents/Frameworks"
    [ -d "$FRAMEWORKS_DIR" ] || FRAMEWORKS_DIR="$APP_PATH/Contents/MacOS/_internal"
    if [ -d "$FRAMEWORKS_DIR" ]; then
        echo ">>> Forcing h5py's HDF5 dylibs into the bundle"
        cp -f "$H5PY_DYLIBS"/libhdf5*.dylib "$FRAMEWORKS_DIR/" 2>/dev/null || true
    fi
fi

# ---------------------------------------------------------------------------
# Step 5: re-sign.
# ---------------------------------------------------------------------------
# PyInstaller signs the bundle as it builds it; the two steps above invalidate
# that signature.  On Apple Silicon an invalid signature is not a warning -- the
# loader refuses the binary -- so this has to happen after every mutation.
echo ">>> Re-signing (ad-hoc)"
codesign --force --deep --sign - "$APP_PATH"

# ---------------------------------------------------------------------------
# Step 6: self-test.
# ---------------------------------------------------------------------------
# Catches the quiet failures -- above all napari plugin metadata that did not
# make it into the bundle.  The viewer check needs a GL context, so it reports
# itself skipped when this runs without a display.
echo ">>> Self-testing the bundle"
"$APP_PATH/Contents/MacOS/$APP_NAME" --bundle-selftest

if [ "${IMSWITCH_SKIP_DMG:-0}" = "1" ]; then
    echo
    echo ">>> IMSWITCH_SKIP_DMG=1, stopping after the .app"
    echo "    App: $APP_PATH"
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 7: DMG.
# ---------------------------------------------------------------------------
echo ">>> Building $DMG_BASENAME.dmg"
DMG_PATH="$BUILD_ROOT/$DMG_BASENAME.dmg"
rm -rf "$STAGING_DIR" "$DMG_PATH"
mkdir -p "$STAGING_DIR"
cp -R "$APP_PATH" "$STAGING_DIR/$APP_NAME.app"

if command -v create-dmg >/dev/null 2>&1; then
    # Nicer: a sized window with the app and an Applications drop target.
    create-dmg \
        --volname "$APP_NAME $VERSION" \
        --volicon "$SCRIPT_DIR/$APP_NAME.icns" \
        --window-pos 200 120 \
        --window-size 640 400 \
        --icon-size 96 \
        --icon "$APP_NAME.app" 160 190 \
        --app-drop-link 480 190 \
        --no-internet-enable \
        "$DMG_PATH" \
        "$STAGING_DIR"
else
    # hdiutil ships with macOS, so the build needs no Homebrew.  The image is a
    # plain compressed one: no custom window layout and no Applications alias,
    # which costs presentation but nothing functional.
    echo ">>> create-dmg not found, using hdiutil (plain image)."
    echo "    For the laid-out window instead: brew install create-dmg"
    ln -s /Applications "$STAGING_DIR/Applications"
    hdiutil create \
        -volname "$APP_NAME $VERSION" \
        -srcfolder "$STAGING_DIR" \
        -ov -format UDZO \
        "$DMG_PATH"
fi

rm -rf "$STAGING_DIR"
deactivate

echo
echo "============================================="
echo " Build complete"
echo " App: $APP_PATH"
echo " DMG: $DMG_PATH"
echo
echo " The DMG is ad-hoc signed, not notarised.  On first launch users must"
echo " right-click $APP_NAME.app -> Open, or run:"
echo "   xattr -dr com.apple.quarantine /Applications/$APP_NAME.app"
echo "============================================="
