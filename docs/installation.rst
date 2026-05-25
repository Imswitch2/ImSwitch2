************
Installation
************

Imswitch2 is a Python application.  There is no Windows ``.exe`` bundle —
the original ImSwitch project shipped one, but it is **no longer
maintained** in Imswitch2.  Install from source or PyPI instead.

Requirements
============

* **Python 3.10 or later** (CPython, 64-bit).
* **PyQt5** (installed automatically as a dependency).
* Windows, macOS, or Linux.  A few optional components (the image
  reconstruction module, TIS cameras) are Windows-only — everything
  else runs on all three platforms.


Option A: Install from PyPI
===========================

.. code-block:: bash

   pip install imswitch

Then launch:

.. code-block:: bash

   imswitch

.. note::

   PyPI releases trail the ``main`` branch.  For the latest fixes,
   prefer the source install below.


Option B: Install from source (recommended for developers)
==========================================================

.. code-block:: bash

   git clone https://github.com/Imswitch2/Imswitch2.git
   cd Imswitch2
   pip install -e .

   # Optional extras
   pip install -e ".[hardware]"   # NI-DAQ, pyVISA, vendor drivers
   pip install -e ".[full]"       # also napari, OpenCV, vispy

   # Developer toolchain (tests, lint, docs)
   pip install -r requirements-dev.txt

Launch:

.. code-block:: bash

   python -m imswitch

On first launch Imswitch2 creates ``~/ImSwitchConfig/`` (or
``%USERPROFILE%\Documents\ImSwitchConfig\`` on Windows) and opens a
setup-picker dialog.  Pick ``example_no_hardware.json`` to explore the
UI without any device connected.
