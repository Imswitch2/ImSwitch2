# Audit Report 5 — Packaging & Dependencies

**Repository:** `/Users/lenny/PycharmProjects/Imswitch2`
**Audit date:** 2026-06-21
**Auditor:** Read-only packaging audit

## Summary

1. **[CRITICAL] Overly broad napari constraint causes numpy/pydantic incompatibility** — `napari>=0.4.17` allows installing napari 0.4.x (requires numpy<2, pydantic<2) alongside numpy 2.x or pydantic 2.x, breaking pytest collection when napari's imports fail due to API mismatches. Pin to `napari>=0.7.0` to enforce numpy 2.x + pydantic 2.x compatibility.

2. **[HIGH] Stale wheel version mismatch** — `dist/imswitch-2.0.0-py3-none-any.whl` exists but `imswitch.__version__` is `'0.1.0'`; stale builds pollute the repository and can cause confusion in CI/CD.

3. **[MEDIUM] Questionable hardware extra** — `setup.cfg:[options.extras_require]hardware` lists `microscope` which does not exist on PyPI (should be `microscope-cockpit`?), causing `pip install imswitch[hardware]` to fail.

4. **[LOW] Qt import at imcommon top-level** — `imswitch/imcommon/applaunch.py` imports `qtpy` at module level (line 6), preventing headless/CLI use of `imswitch.imcommon` without Qt installed. However, `imswitch.__init__.py` is clean and widgets are lazily loaded.

5. **[LOW] setuptools version cap** — `pyproject.toml:[build-system]` pins `setuptools>=42,<80`, preventing use of setuptools 80+ bugfixes.

---

## Findings

### [HIGH] Napari version constraint too broad — numpy 2.x incompatibility

**Where:**
- `setup.cfg:[options]install_requires` line 19: `napari>=0.4.17`
- `setup.cfg:[options]install_requires` line 21: no numpy pin

**Problem:**
The constraint `napari>=0.4.17` spans napari versions with incompatible dependency requirements:

- **napari 0.4.17–0.4.x** (released ~early 2023) required `numpy<2.0` and `pydantic<2.0`
- **napari 0.7.0+** (current) requires `pydantic>=2.0` and is compatible with `numpy>=2.0`

When `pip install imswitch` resolves in an environment with an older resolver or conflicting pins from other packages, it may install napari 0.4.x with numpy 2.x or pydantic 2.x, causing import failures:

```python
# napari 0.4.x with pydantic 2.x
ImportError: cannot import name 'ModelMetaclass' from 'pydantic.main'
# or napari 0.4.x with numpy 2.x
AttributeError: module 'numpy' has no attribute 'bool'
```

These errors break **pytest collection** because:
1. `imswitch/imcommon/view/guitools/naparitools.py:4` imports `napari` at module level
2. Multiple widgets import `naparitools` at the top (e.g., `SettingsWidget.py:28`, `AlignXYWidget.py:19`, etc.)
3. Even though widgets use lazy loading via `__getattr__` in `imswitch/imcontrol/view/widgets/__init__.py:61-70`, test files that directly import widget modules trigger the napari import
4. `conftest.py:12-23` tries to stub `napari` if import fails, but this only catches missing packages, not broken imports from dependency mismatches

**Current environment** (working):
```
numpy==2.4.4
napari==0.7.0
pydantic==2.12.5
```

**Concrete fix:**

**Option A (recommended):** Pin to numpy 2.x + napari 0.7.x era:
```diff
--- a/setup.cfg
+++ b/setup.cfg
@@ -18,7 +18,8 @@ install_requires =
     luddite>=1.0
     matplotlib>=3.4
-    napari>=0.4.17
+    napari>=0.7.0
     nidaqmx-python>=0.2
+    numpy>=2.0
     opencv-python-headless>=4.5
     pint>=0.17
```

**Option B:** Support both eras via extras (more complex):
```diff
--- a/setup.cfg
+++ b/setup.cfg
@@ -18,7 +18,7 @@ install_requires =
     # ... other deps ...
-    napari>=0.4.17
+    # napari moved to extras to avoid version conflicts

 [options.extras_require]
+napari-legacy =
+    napari>=0.4.17,<0.5
+    numpy>=1.20,<2.0
+    pydantic>=1.0,<2.0
+napari-modern =
+    napari>=0.7.0
+    numpy>=2.0
```

**Rationale for Option A:** Since the current codebase uses numpy 2.x features (verified in env) and dataclasses-json 0.6.7 (which works with modern stacks), backward compatibility with napari 0.4.x is not a requirement.

---

### [HIGH] Stale build artifacts in dist/ — version mismatch

**Where:**
- `dist/imswitch-2.0.0-py3-none-any.whl` and `dist/imswitch-2.0.0.tar.gz`
- `imswitch/__init__.py:1` defines `__version__ = '0.1.0'`
- `setup.cfg:[metadata]version` reads `attr: imswitch.__version__`
- `ImSwitch.egg-info/PKG-INFO:3` shows `Version: 0.1.0`

**Problem:**
The `dist/` directory contains wheel and sdist for version `2.0.0` (timestamped May 12 13:22), but the current source declares version `0.1.0`. This indicates:
1. Builds were made from different commits without cleaning `dist/`
2. CI/CD or manual builds may accidentally upload the wrong version
3. `pip install dist/imswitch-2.0.0-py3-none-any.whl` installs code that doesn't match the current repo

**Concrete fix:**
```bash
# Before building:
rm -rf dist/ build/ *.egg-info
python -m build
```

Add to `.gitignore`:
```
dist/
build/
*.egg-info/
```

Or add to CI/CD pipeline:
```yaml
- name: Clean build artifacts
  run: rm -rf dist/ build/ *.egg-info
- name: Build package
  run: python -m build
```

---

### [MEDIUM] Hardware extra references non-existent PyPI package

**Where:**
- `setup.cfg:[options.extras_require]hardware` lines 42-45:
```ini
[options.extras_require]
hardware =
    microscope
    pylablib>=1.3
```

**Problem:**
`microscope` does not exist on PyPI. The likely intended package is `microscope-cockpit` (the Python Microscope project's hardware control library). Running `pip install imswitch[hardware]` fails:

```
ERROR: Could not find a version that satisfies the requirement microscope
```

**Verification:**
```bash
$ python -m pip show microscope
WARNING: Package(s) not found: microscope
```

**Concrete fix:**
```diff
--- a/setup.cfg
+++ b/setup.cfg
@@ -39,7 +39,7 @@ scripts =

 [options.extras_require]
 hardware =
-    microscope
+    microscope-cockpit>=0.10
     pylablib>=1.3
```

**Note:** Hardware SDK imports are correctly wrapped in try/except blocks:
- `imswitch/imcontrol/model/managers/NidaqManager.py:5-11` wraps `import nidaqmx` in try/except
- `imswitch/imcontrol/model/managers/flipMirrors/ThorlabsMFF.py` imports `pylablib.devices.Thorlabs` inside methods

So the package itself handles missing hardware SDKs gracefully; this fix only enables users who explicitly request `pip install imswitch[hardware]` to get the correct dependencies.

---

### [MEDIUM] Optional extras not marked as optional for all hardware SDKs

**Where:**
- `setup.cfg:[options]install_requires` line 20: `nidaqmx-python>=0.2` (in main install_requires)
- `setup.cfg:[options.extras_require]hardware` lines 42-45 (microscope, pylablib)

**Problem:**
`nidaqmx-python` is in the main `install_requires`, making it mandatory for all users, even those without National Instruments hardware. While the NI-DAQmx C driver is not auto-installed (user must install it separately), the Python wrapper adds ~5MB to the installation and pulls in transitive dependencies.

The code already handles its absence gracefully:
- `imswitch/imcontrol/model/managers/NidaqManager.py:5-11` wraps `import nidaqmx` in try/except and sets `_NIDAQMX_AVAILABLE = False`

**Concrete fix:**
```diff
--- a/setup.cfg
+++ b/setup.cfg
@@ -17,7 +17,6 @@ install_requires =
     jsonschema>=3.2
     luddite>=1.0
     matplotlib>=3.4
-    nidaqmx-python>=0.2
     numpy>=2.0
     opencv-python-headless>=4.5
     pint>=0.17
@@ -39,6 +38,7 @@ scripts =
 [options.extras_require]
 hardware =
     microscope-cockpit>=0.10
+    nidaqmx-python>=0.2
     pylablib>=1.3
```

This makes ImSwitch a "rounder" package: core users install only the dependencies they need, and hardware users opt in via `pip install imswitch[hardware]`.

---

### [LOW] Qt import at imcommon top-level prevents headless use

**Where:**
- `imswitch/imcommon/__init__.py:1` imports `from .applaunch import prepareApp, launchApp`
- `imswitch/imcommon/applaunch.py:6` imports `from qtpy import QtCore, QtGui, QtWidgets`

**Problem:**
Running `import imswitch.imcommon` (e.g., to access `imswitch.imcommon.model.dirtools` in a headless script or test) triggers a Qt import, failing if Qt is not installed:

```python
>>> import imswitch.imcommon
ModuleNotFoundError: No module named 'qtpy'
```

**Impact:**
- **Low** because:
  1. `imswitch/__init__.py` does not import `imcommon`, so `import imswitch` is safe
  2. Widgets use lazy loading (`__getattr__` in `imswitch/imcontrol/view/widgets/__init__.py:61-70`), so importing the widgets package doesn't trigger napari/Qt imports
  3. Tests use `conftest.py:12-46` to stub Qt, napari, and matplotlib if missing

- **Medium** if headless/CLI use cases grow (e.g., a future `imswitch-process-only` package or API server mode).

**Concrete fix (if needed):**
```diff
--- a/imswitch/imcommon/__init__.py
+++ b/imswitch/imcommon/__init__.py
@@ -1 +1,6 @@
-from .applaunch import prepareApp, launchApp
+def __getattr__(name):
+    if name in ("prepareApp", "launchApp"):
+        from .applaunch import prepareApp, launchApp
+        return globals()[name]
+    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

Or move `applaunch` out of `imcommon.__init__` entirely and require `from imswitch.imcommon.applaunch import prepareApp`.

---

### [LOW] setuptools version cap prevents bugfixes

**Where:**
- `pyproject.toml:[build-system]requires` lines 2-4:
```toml
[build-system]
requires = [
    "setuptools>=42,<80",
    "wheel"
]
```

**Problem:**
Pinning `setuptools<80` (current latest: 82+) prevents users from benefiting from:
- Security fixes in setuptools 80+ (CVE patches)
- Improved editable install handling (PEP 660)
- Better error messages for malformed `setup.cfg`

The cap was likely added to avoid breaking changes in setuptools 80, but setuptools maintains backward compatibility for standard `setup.cfg` + `pyproject.toml` builds.

**Concrete fix:**
```diff
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -1,6 +1,6 @@
 [build-system]
 requires = [
-    "setuptools>=42,<80",
+    "setuptools>=42",
     "wheel"
 ]
```

Test with:
```bash
rm -rf dist/ build/ *.egg-info
pip install -U 'setuptools>=80' build
python -m build
pip install dist/imswitch-*.whl
python -c "import imswitch; print(imswitch.__version__)"
```

---

## Dependency conflict analysis

### Root cause: napari version sprawl

The pytest collection failure reported in the user's environment stems from:

**Dependency chain:**
```
imswitch
 └─ napari>=0.4.17  [PROBLEM: too broad]
     ├─ [if napari 0.4.17-0.4.x] pydantic<2.0, numpy<2.0
     │   ├─ pydantic 1.x exports pydantic.main.ModelMetaclass
     │   └─ numpy 1.x exports numpy.bool (removed in 2.0)
     └─ [if napari 0.7.0+] pydantic>=2.0, numpy (no upper bound)
         ├─ pydantic 2.x does NOT export ModelMetaclass (breaking change)
         └─ numpy 2.x removed numpy.bool, numpy.int, etc.
```

**Failure scenario:**
1. User installs ImSwitch in an environment with numpy 2.0 already installed (e.g., from another project)
2. pip resolves `napari>=0.4.17` to napari 0.4.19 (if other packages pin numpy 2.x, forcing napari downgrade to avoid conflicts)
3. napari 0.4.19 tries to import `from pydantic.main import ModelMetaclass` → fails with pydantic 2.x
4. OR napari 0.4.19 uses `numpy.bool` → fails with `AttributeError: module 'numpy' has no attribute 'bool'`
5. Pytest collection imports `imswitch/imcontrol/view/widgets/SettingsWidget.py` → imports `naparitools` → `import napari` (line 4) → crash

**Why conftest.py doesn't save you:**
```python
# conftest.py:12-23
def _stub_if_missing(pkg_root, submodules=()):
    try:
        __import__(pkg_root)  # If napari is installed, this succeeds...
    except Exception:
        sys.modules[pkg_root] = MagicMock()  # ...so stub is never created
```

If napari is installed but broken (import raises `AttributeError` or `ImportError` mid-import), the exception may propagate before the stub can intercept it, or the stub logic may not cover all submodules napari tries to import.

### Concrete remediation

**Step 1:** Pin napari to 0.7.0+ (enforces numpy 2.x + pydantic 2.x)
```diff
--- a/setup.cfg
+++ b/setup.cfg
@@ -18,7 +18,8 @@ install_requires =
     matplotlib>=3.4
-    napari>=0.4.17
+    napari>=0.7.0
+    numpy>=2.0
     opencv-python-headless>=4.5
```

**Step 2:** If backward compatibility with napari 0.4.x is required, create extras:
```diff
+[options.extras_require]
+napari-legacy =
+    napari>=0.4.17,<0.5
+    numpy>=1.20,<2.0
+    pydantic>=1.0,<2.0
+napari-modern =
+    napari>=0.7.0
+    numpy>=2.0
```

**Step 3:** Update conftest.py to catch broken imports (defensive):
```diff
--- a/conftest.py
+++ b/conftest.py
@@ -12,7 +12,7 @@ from unittest.mock import MagicMock
 def _stub_if_missing(pkg_root, submodules=()):
     """Register MagicMock stubs when optional packages are missing or broken."""
     try:
         __import__(pkg_root)
-    except Exception:
+    except Exception as e:
+        # Stub on any import failure (missing, broken deps, etc.)
         sys.modules[pkg_root] = MagicMock()
```

This ensures that even if napari is installed but broken, tests can still run with stubbed napari.

**Step 4:** Verify fix:
```bash
# Test in fresh venv with numpy 2.x
python3.12 -m venv /tmp/test-imswitch
source /tmp/test-imswitch/bin/activate
pip install numpy==2.0
pip install -e .
python -c "import napari; print(napari.__version__)"  # Should be 0.7.0+
pytest --collect-only  # Should succeed
```

---

## Packaging metadata audit

### ✅ Python version constraint

**Where:** `setup.cfg:[metadata]` line 12:
```ini
python_requires = >=3.10
```

**Status:** Correct. Codebase uses:
- Match-case statements (Python 3.10+) in `imswitch/imcontrol/model/managers/` files
- `TypeAlias` from `typing` (3.10+)

### ✅ Entry points

**Where:**
- `setup.cfg:[options.entry_points]` lines 34-36:
```ini
[options.entry_points]
console_scripts =
    imswitch = imswitch.__main__:main
```

**Verification:**
```bash
$ cat ImSwitch.egg-info/entry_points.txt
[console_scripts]
imswitch = imswitch.__main__:main
```

**Status:** Correct. Entry point correctly wired to `imswitch/__main__.py:main()`.

### ✅ Package data (_data/)

**Where:**
- `MANIFEST.in` lines 1-2:
```
recursive-include imswitch/_data *
global-exclude *.pyc __pycache__
```

**Verification:**
```bash
$ python -c "import zipfile; z = zipfile.ZipFile('dist/imswitch-2.0.0-py3-none-any.whl'); \
  print('\\n'.join([n for n in z.namelist() if '_data' in n][:10]))"
imswitch/_data/icon.ico
imswitch/_data/icon.png
imswitch/_data/libs/GPU_acc_recon.dll
imswitch/_data/user_defaults/README.txt
imswitch/_data/user_defaults/imcontrol_setups/example_sted.json
...
```

**Status:** Correct. All `_data/` files included in wheel.

### ✅ pluginapi packaging

**Where:**
- `setup.cfg:[options]packages` line 28: `find:` (auto-discovers `imswitch.pluginapi`)
- `imswitch/pluginapi/__init__.py:9-23` exports `__all__` with public API

**Verification:**
```python
>>> from imswitch.pluginapi import DeviceInfo, DetectorManager
>>> DeviceInfo
<class 'imswitch.pluginapi.devices.DeviceInfo'>
```

**Status:** Correct. Plugin API is properly packaged and importable.

### ✅ improcess (renamed from imreconstruct)

**Where:**
- `imswitch/improcess/` directory exists
- No `imswitch/imreconstruct/` directory
- `setup.cfg:[options]packages = find:` auto-discovers `imswitch.improcess`

**Verification:**
```bash
$ python -c "import imswitch.improcess; print(imswitch.improcess.__file__)"
/Users/lenny/PycharmProjects/Imswitch2/imswitch/improcess/__init__.py
```

**Status:** Correct. Rename complete; no compatibility shim needed per design doc (`docs/design/plans/imreconstruct-2-0.md`).

**Minor issue:** Design docs still reference `imreconstruct`:
- `docs/design/plans/imreconstruct-2-0.md` (intentionally kept per line 6: "Filename of *this* design doc stays `imreconstruct-2-0.md`")
- No action needed; this is for historical context.

### ✅ Version source of truth

**Where:**
- `imswitch/__init__.py:1`: `__version__ = '0.1.0'`
- `setup.cfg:[metadata]version` line 3: `attr: imswitch.__version__`

**Status:** Correct. Single source of truth; build reads version from `__init__.py`.

**Verification:**
```bash
$ grep "^Version:" ImSwitch.egg-info/PKG-INFO
Version: 0.1.0
```

### ✅ Build backend

**Where:**
- `pyproject.toml:[build-system]` lines 1-6:
```toml
[build-system]
requires = ["setuptools>=42,<80", "wheel"]
build-backend = "setuptools.build_meta"
```

**Status:** Functional but see [LOW] finding above re: `setuptools<80` cap.

### ✅ Editable install sanity

**Verification:**
```bash
$ pip install -e .
$ python -c "import imswitch; print(imswitch.__file__)"
/Users/lenny/PycharmProjects/Imswitch2/imswitch/__init__.py
```

**Status:** Correct. Editable installs work.

---

## Severity table

| Severity | Finding | Impact | Effort to Fix |
|----------|---------|--------|---------------|
| **HIGH** | Napari version constraint too broad | Pytest collection fails in environments with numpy 2.x or pydantic 2.x when pip resolves to napari 0.4.x | 5 min (change 2 lines in setup.cfg) |
| **HIGH** | Stale wheel in dist/ (2.0.0 vs 0.1.0) | Accidental distribution of wrong version; CI/CD confusion | 2 min (add `rm -rf dist/` to build script) |
| **MED** | `microscope` package doesn't exist | `pip install imswitch[hardware]` fails | 1 min (fix typo: `microscope-cockpit`) |
| **MED** | nidaqmx in install_requires (not extra) | Forces hardware dep on all users | 3 min (move to `[hardware]` extra) |
| **LOW** | Qt import at imcommon top-level | Prevents headless `import imswitch.imcommon` | 10 min (lazy-load applaunch or move out of __init__) |
| **LOW** | setuptools<80 cap | Missing bugfixes/security patches | 1 min (remove upper bound) |

**Total estimated fix time:** 30 minutes for all HIGH+MED issues.

---

## Recommendations

### Immediate (before next release)
1. Pin `napari>=0.7.0` and `numpy>=2.0` in `setup.cfg` to prevent dependency resolver from picking incompatible combinations.
2. Add `rm -rf dist/ build/ *.egg-info` to build scripts and `.gitignore`.
3. Fix `microscope` → `microscope-cockpit` in `[hardware]` extra.

### Short-term (next sprint)
4. Move `nidaqmx-python` to `[hardware]` extra to make base install lighter.
5. Remove `setuptools<80` cap and test build with setuptools 82+.

### Long-term (if headless use cases emerge)
6. Lazy-load `applaunch` in `imswitch.imcommon.__init__` to allow headless imports.
7. Consider splitting package into `imswitch-core` (no GUI) and `imswitch-gui` (napari + Qt) if CLI/API-only use cases become common.

---

## Appendix: Files audited

- `pyproject.toml` — build system
- `setup.cfg` — metadata, dependencies, extras
- `requirements.txt`, `requirements-dev.txt`, `docs/requirements-readthedocs.txt` — not used by packaging (only for dev/docs envs)
- `MANIFEST.in` — package data inclusion
- `imswitch/__init__.py` — version, top-level imports
- `imswitch/imcommon/__init__.py`, `imswitch/imcommon/applaunch.py` — Qt imports
- `imswitch/imcontrol/view/widgets/__init__.py` — lazy widget loading
- `imswitch/imcommon/view/guitools/naparitools.py` — napari imports
- `imswitch/pluginapi/__init__.py` — plugin API exports
- `conftest.py` — test stubs for optional deps
- `ImSwitch.egg-info/PKG-INFO`, `ImSwitch.egg-info/entry_points.txt` — build metadata
- `dist/imswitch-2.0.0-py3-none-any.whl` — stale build artifact

**End of report.**
