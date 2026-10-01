************
Installation
************

ImSwitch2 is a Python application.  There is no Windows ``.exe`` bundle —
the original ImSwitch project shipped one, but it is **no longer
maintained** in ImSwitch2.  Install from source or PyPI instead.

Requirements
============

* **Python 3.10 or later** (CPython, 64-bit).
* **PyQt5** (installed automatically as a dependency).
* Windows, macOS, or Linux.  A few optional components (the MoNaLISA
  reconstruction DLLs, TIS cameras) are Windows-only — the generic
  ImProcess shell and most plugins run on all three platforms.


Option A: Install from PyPI
===========================

.. code-block:: bash

   pip install imswitch2

Then launch:

.. code-block:: bash

   imswitch

.. note::

   PyPI releases trail the ``main`` branch.  For the latest fixes,
   prefer the source install below.

.. warning::

   The PyPI name is ``imswitch2``.  ``ImSwitch`` on PyPI is the original
   project, and it installs the same ``imswitch`` package, so asking pip
   for ``imswitch`` -- or installing a device plugin that requires it --
   replaces ImSwitch2 with it.  Only the distribution is renamed: the
   command is still ``imswitch`` and scripts still ``import imswitch``.


Option B: Install from source (recommended for developers)
==========================================================

.. code-block:: bash

   git clone https://github.com/Imswitch2/ImSwitch2.git
   cd ImSwitch2
   pip install -e .

   # Optional extras
   pip install -e ".[hardware]"   # NI-DAQ, pyVISA, vendor drivers
   pip install -e ".[imagej]"     # ImageJ/Fiji .roi and RoiSet.zip import/export in the ROI manager
   pip install -e ".[full]"       # OpenCV, plus what [imagej] installs
   pip install -e ".[storm]"      # napari-storm GPU point-cloud viewer for SMLM results

   # Developer toolchain: the test suite as CI runs it, the linter, the docs build
   pip install -e ".[test]" ruff
   pip install -r docs/requirements-readthedocs.txt

Launch:

.. code-block:: bash

   python -m imswitch

``--debug`` turns on DEBUG-level logging from every manager, and
``--scale 0.8`` draws the whole interface at 80 % (the
``IMSWITCH_UI_SCALE`` environment variable does the same; the flag wins).

On first launch ImSwitch2 creates ``~/ImSwitchConfig/`` (or
``%USERPROFILE%\Documents\ImSwitchConfig\`` on Windows) and opens a
setup-picker dialog.  Pick ``example_no_hardware.json`` to explore the
UI without any device connected.


Vendor SDKs (not installed by pip)
==================================

Some device managers depend on vendor-supplied Python packages that are
**not on PyPI**.  They are not declared as project dependencies — if a
manager needs one, install it manually following the vendor's procedure.
When the SDK is missing, most managers log a warning and fall back to a
mock device, so ImSwitch2 will still start; a few refuse instead (the
Swabian Time Tagger stops the scan that needs it, for example).  Each
device's page under *Hardware reference* says which it does.

The pattern below is illustrative; the same approach applies to other
vendor SDKs (Hamamatsu DCAM, Andor SDK3, Basler pylon, etc.).


Thorlabs Scientific Cameras (Kiralux / Zelux / Quantalux)
---------------------------------------------------------

Used by :class:`~imswitch.imcontrol.model.managers.detectors.ThorCamTSIManager.ThorCamTSIManager`.

**1. Download the SDK.**  Get *ThorCam* from
https://www.thorlabs.com/software-pages/ThorCam
and install it.  Inside the install folder, find
``Scientific Camera Interfaces.zip`` and unzip it somewhere writable
(not inside ``Program Files`` — see troubleshooting below).

**2. Install the Python package.**  Recent SDK releases ship as a source
tree (no ``.whl``).  From a writable copy of the Python Toolkit folder:

.. code-block:: text

   cd C:\dev\thorlabs_tsi_sdk_src    # your writable copy
   pip install .

Verify the import works in your ImSwitch2 env:

.. code-block:: bash

   python -c "from thorlabs_tsi_sdk.tl_camera import TLCameraSDK; print('ok')"

**3. Place the native DLLs.**  The Python package is a thin ctypes wrapper
and needs Thorlabs' native C SDK DLLs at runtime.  Copy the *entire*
``Native_64_lib`` folder from
``Scientific Camera Interfaces\SDK\Native Toolkit\dlls\Native_64_lib\`` to
a folder of your choice, then point the manager at it via the detector's
``dllLocation`` property:

.. code-block:: json

   "detectors": {
       "Kiralux": {
           "managerName": "ThorCamTSIManager",
           "managerProperties": {
               "cameraSerial": "29718",
               "dllLocation": "C:/Thorlabs/Native_64_lib"
           }
       }
   }

Use an absolute path with forward slashes — relative paths are resolved
against ImSwitch2's current working directory at launch time, which is
easy to get wrong.

**4. Test without hardware.**  Set ``cameraSerial`` to any string starting
with ``MOCK_`` (e.g. ``"MOCK_29718"``) and the manager will load a mock
camera regardless of whether the SDK or driver is installed.


Troubleshooting
---------------

``Could not find a version that satisfies the requirement thorlabs_tsi_sdk``
    You ran ``pip install thorlabs_tsi_sdk`` (or an extras group that
    referenced it).  The SDK is not on PyPI; install it from the Thorlabs
    ThorCam download as described above.

``cannot update time stamp of directory 'thorlabs_tsi_sdk.egg-info'``
    Setuptools can't write inside the SDK source tree.  The source is in
    a read-only or sync'd location (``Program Files``, OneDrive, network
    drive), or a stale ``.egg-info`` from a previous install is locked.
    Fix: copy the source out to a normal user directory, delete any
    existing ``build/``, ``dist/``, and ``*.egg-info`` folders, then
    re-run ``pip install .``.

``Could not find thorlabs_tsi_camera_sdk.dll or one of its dependencies``
    The Python wrapper found, but the native DLLs aren't on the search
    path.  Either ``dllLocation`` in the JSON is wrong (try an absolute
    path), the ``Native_64_lib`` folder is incomplete (copy the *whole*
    folder, the DLLs depend on each other), or the Thorlabs Unified USB
    Camera Driver isn't installed (re-run the ThorCam installer; check
    that the camera shows up correctly in Windows Device Manager).

``'int' object has no attribute 'startswith'`` (any manager)
    A property in your setup JSON is typed as an integer where the
    manager expects a string.  For ``cameraSerial`` specifically, wrap
    the number in quotes: ``"cameraSerial": "29718"`` (not ``29718``).
