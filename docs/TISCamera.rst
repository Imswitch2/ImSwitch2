The Imaging Source (TIS) cameras
================================

ImSwitch2 can drive The Imaging Source cameras in two ways:

``"managerName": "TISManager"``
    The built-in manager.  It uses the legacy IC Imaging Control 3
    TISGrabber C library through a bundled ``ctypes`` wrapper, and runs on
    Windows only.  It reads a single frame buffer that the driver overwrites
    in place, so it cannot deliver one distinct frame per hardware trigger:
    a triggered scan returns the same image at every position, and a
    triggered recording repeats frames.  Live view, snapshots and focus
    lock use only the latest frame and are not affected.

``"managerName": "tis.camera-ic4"``
    The ``imswitch-device-tis`` device plugin, built on IC Imaging Control 4
    (``imagingcontrol4``).  It keeps every delivered frame until it is read
    and can arm and disarm the hardware trigger itself, so use it for
    triggered scans and recordings.  It does not take over setups that name
    ``TISManager``: switching is a change to the setup file.

``TISManager``'s setup fields are listed in :doc:`devices/detectors`; the
plugin's are in ``examples/plugins/imswitch-device-tis/README.md`` in the
source repository.  Device plugins in general are described in
:doc:`devices/plugins`.


The IC4 plugin (``tis.camera-ic4``)
-----------------------------------

The plugin is bundled in the source repository and is not published on PyPI,
so ``pip install imswitch-device-tis`` does not find it.  From a source
checkout, install it together with the vendor SDK:

.. code-block:: bash

   pip install "./examples/plugins/imswitch-device-tis[hardware]"

The ``hardware`` extra installs the Python bindings only.  The camera also
needs the **IC4 GenTL Producer (USB3 Vision)** from The Imaging Source,
installed separately; without it the camera does not enumerate.  A camera
still bound only to the legacy IC3 driver does not appear either.

A detector entry, as in the plugin's README:

.. code-block:: json

   "detectors": {
       "TISCam": {
           "managerName": "tis.camera-ic4",
           "managerProperties": {
               "cameraSerial": "43710086",
               "cameraPixelSizeUm": 0.15,
               "pixelFormat": "Mono16",
               "defaults": {
                   "exposure_us": 5000,
                   "trigger_mode": "Off",
                   "trigger_source": "Line1",
                   "trigger_activation": "RisingEdge"
               }
           },
           "forAcquisition": true
       }
   }

``cameraSerial`` selects the camera by serial number (``null`` opens the
first one found; a value starting with ``MOCK_`` loads a simulated camera).
Set ``trigger_activation`` explicitly: at least some cameras ship set to
``FallingEdge``, which latches on the trailing edge of a trigger pulse.
Start with ``trigger_mode`` ``"Off"`` and switch to ``Hardware`` in the
detector settings once a free-running image looks right.

The DLL steps below do not apply to this plugin.


Legacy ``TISManager`` on Windows
--------------------------------

For setups using ``TISManager`` (including a focus-lock camera configured
with it), install the TIS camera driver and the legacy TISGrabber C DLL
before starting ImSwitch2 on a new Windows computer.

If startup logs say that a TIS or focus-lock camera failed to initialize and
ImSwitch2 loads a mock camera instead, first check whether
``tisgrabber_x64.dll`` can be found.

Required TIS software:

* The correct Windows device driver for the camera model from the
  `The Imaging Source downloads page <https://www.theimagingsource.com/en-us/support/download/>`_.
* `IC Imaging Control C Library 3.4.0.51 <https://www.theimagingsource.com/en-us/support/download/tisgrabberdll-3.4.0.51/>`_.

Use the C Library package, not the C++ Class Library. ``TISManager`` loads
``tisgrabber_x64.dll`` (``tisgrabber.dll`` on 32-bit Python) directly
through Python ``ctypes``, so it needs the TISGrabber C DLL.

After installing, make sure the folder containing ``tisgrabber_x64.dll`` is on ``PATH``. A common
install location is:

.. code-block:: text

   C:\Users\<username>\Documents\The Imaging Source Europe GmbH\TIS Grabber DLL\bin\x64

To test temporarily in PowerShell before changing Windows settings:

.. code-block:: powershell

   $tis = 'C:\Users\<username>\Documents\The Imaging Source Europe GmbH\TIS Grabber DLL\bin\x64'
   $env:PATH = "$tis;$env:PATH"
   python -c "import ctypes.util; print(ctypes.util.find_library('tisgrabber_x64.dll'))"

If ImSwitch2 is run from a conda environment, run the same check with that environment, for example:

.. code-block:: powershell

   conda run -n imswitch python -c "import ctypes.util; print(ctypes.util.find_library('tisgrabber_x64.dll'))"

The command should print the full path to ``tisgrabber_x64.dll``. If it prints ``None`` or
``where.exe tisgrabber_x64.dll`` cannot find the DLL, add the ``bin\x64`` folder to the user or
system ``PATH`` and restart the terminal before starting ImSwitch2.

From a source checkout, ``python utility_scripts/diagnose_tis_camera.py``
runs the same DLL lookup outside ImSwitch2 and then initialises the library
and lists the connected cameras, which separates a driver or USB problem
from an ImSwitch2 one.
