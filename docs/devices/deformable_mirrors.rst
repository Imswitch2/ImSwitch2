*******************************
Deformable mirrors — reference
*******************************

A deformable mirror takes one value per actuator, sent as a whole
*command*. ImSwitch keeps three things per mirror:

* the **last command** it sent;
* the **flat**: the command that makes the mirror flat (all zeros unless
  ``flatFile`` gives one; *Save as flat* in the panel or ``setFlat`` in a
  script replaces it in memory);
* optionally a **mode matrix** (``modesFile``): row ``k`` is the command
  for one unit of mode ``firstMode + k`` (or of the ``k``-th of
  ``modeNumbers``). ``setModes`` sends the flat plus
  a weighted sum of rows. ALPAO's own ``<serial>-Z2C.csv`` is such a
  matrix, and so is the one the AO calibration script writes.

A command with any value beyond ``±maxCommand`` is refused, never clipped:
nothing is sent and the mirror keeps its last command.

Mirror commands are admitted through the resource registry like the other
devices: while a script or measurement run reserves the mirror, other
commands are refused.


How mirrors are configured
==========================

Mirrors live under the top-level ``"deformableMirrors"`` dict of the setup
JSON, one entry per mirror:

.. code-block:: json

    "deformableMirrors": {
        "DM": {
            "managerName": "AlpaoDMManager",
            "managerProperties": {
                "serialNumber": "BAX123",
                "flatFile": "C:/AO/flat_BAX123.csv",
                "modesFile": "C:/Program Files/Alpao/SDK/Config/BAX123-Z2C.csv",
                "firstMode": 2,
                "maxCommand": 0.9
            }
        }
    }

**Mirror files.**  A command file holds one value per actuator: ``.csv`` or
``.txt`` (numbers separated by commas, semicolons or white space; a first
line that is not numbers is a header, and a column headed ``Push`` or
``Command`` is the command), ``.npy``, or the ``.xlsx`` flat files
Microscope_GUI wrote (first sheet, ``Push`` column). A matrix file is
``.csv``/``.txt`` without a header, ``.npy`` or ``.tif`` (Microscope_GUI's
control matrices); a row per mode and a column per actuator, or the
transpose. ImSwitch writes commands as ``.csv`` (``Actuator,Push``) or
``.npy``.


The panel and scripts
=====================

``"DeformableMirror"`` in ``availableWidgets`` adds the **Deformable
Mirror** panel: per mirror its last command as bars over the actuators (the
flat as dots), **Flat**, **Zero**, **Reset** and **Use as flat**, **Load...**
and **Save...** for command files, a **Poke** of one actuator over the flat,
and, with a mode matrix, an amplitude per mode and **Apply modes**. A command
the mirror refuses is shown under it in red.

Scripts reach the mirrors with or without the panel::

    api.imcontrol.getDeformableMirrorNames()            # ['DM']
    api.imcontrol.getMirrorInfo('DM')                   # actuators, maxCommand, modes, ...
    api.imcontrol.flattenMirror('DM')
    api.imcontrol.setMirrorModes('DM', {5: 0.05, 11: -0.02})
    api.imcontrol.pokeMirrorActuator('DM', 34, 0.1)
    api.imcontrol.setMirrorCommand('DM', command)       # one value per actuator
    api.imcontrol.setMirrorFlat('DM')                   # the last command is the flat
    api.imcontrol.saveMirrorCommand('DM', 'C:/AO/flat_new.csv')
    api.imcontrol.loadMirrorCommand('DM', 'C:/AO/flat_new.csv')

Every command sent is recorded with the frames: ``DeformableMirror:<name>:Command``
(one value per actuator) and ``DeformableMirror:<name>:Modes`` (the
amplitudes, when the command was a sum of modes).

Adaptive optics with a Shack-Hartmann sensor
============================================

The sensor is an ordinary camera behind a lenslet array (on the
polarization rig, its TIS camera behind a Thorlabs MLA150-5C). Four scripts
in ``scripts/workflows/adaptive_optics`` do what Microscope_GUI's AO panel
did, and write everything to ``<ImSwitchConfig>/adaptive_optics/<mirror>/``:

``01_wavefront_sensor.py``
    Finds the lenslet grid in a frame of the flat mirror (or builds the old
    GUI's typed-in grid), takes the reference -- that frame, or the fitted
    lattice of an ideal array -- and saves the sensor. Prints the wavefront
    in Zernike µm RMS.
``02_mirror_calibration.py``
    Pushes each actuator up and down over the flat and saves the influence
    matrix, and a mode matrix: the command for 1 µm RMS of each Zernike mode.
    Set that file as the mirror's ``modesFile`` (``firstMode`` 2) and the
    panel's modes and ``setMirrorModes`` are in µm RMS.
``03_closed_loop.py``
    Measure, correct (an integrator over the calibration's pseudo-inverse),
    repeat, until the sensor reads the reference; optionally keeps the
    result as the mirror's flat.
``04_sensorless_modes.py``
    The old GUI's "Run AO": scans each mode's amplitude, scores the imaging
    camera's image (maximum, sum, high-pass or band ratio of its spectrum),
    and keeps the parabola's peak.

The computations are in ``imswitch.imcommon.algorithms``: ``zernike`` (the
Noll, unit-RMS basis the bead-PSF aberration fit uses too),
``shack_hartmann`` (grid, spot shifts by centroid or cross-correlation,
slopes, Zernike fit, zonal map) and ``adaptive_optics`` (calibration,
control, metrics). They differ from the old GUI where it was broken or
fixed to one rig: the grid is found instead of typed in, the slope scale
comes from the camera pixel and the lenslets' focal length, the closed loop
is an integrator (the old LQG controller read a calibration file nothing
wrote), and the Fourier wavefront reconstruction became a least-squares
one that takes any pupil.

**The rig's own files.**  Microscope_GUI's ``.xlsx`` flat file is a
``flatFile`` as it is. Its modal control matrix (``control_matrix_modal_*.tif``,
69 actuators by 16 modes) is a ``modesFile`` with ``modeNumbers``
``[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16, 17, 22]``, in that GUI's
units; a new calibration gives µm RMS instead.

**Without hardware.**  ``adaptive_optics_mock_setup.json`` has a mock DM69
and a mock TIS camera (``mockShackHartmann``, see :doc:`detectors`) that
shows a simulated sensor behind it, with a fixed aberration the closed loop
removes.


AlpaoDMManager
==============

An ALPAO mirror (DM69, DM97, ...) through the ALPAO SDK's Python bindings,
as the polarization rig's Microscope_GUI drove its DM69: opened by serial
number, the actuator count read from the SDK, commands in -1 to 1, and a
Reset when ImSwitch closes.

**Setup JSON**

.. code-block:: json

    "deformableMirrors": {
        "DM": {
            "managerName": "AlpaoDMManager",
            "managerProperties": {"serialNumber": "BAX123"}
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 12 60
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``serialNumber``
     - str
     - The mirror's serial number (required). One that starts with ``MOCK``
       is a mock mirror.
   * - ``actuators``
     - int
     - The mock mirror's actuator count. Default 69. A real mirror reports
       its own.
   * - ``sdkPath``
     - str
     - The folder that holds the SDK's ``Lib64`` bindings. Default
       ``C:\Program Files\Alpao\SDK\Samples\Python3``.
   * - ``useMockOnFailure``
     - bool
     - A mock when the mirror cannot be opened, instead of a mirror that is
       not connected. Default false.
   * - ``flatFile``
     - str
     - The flat command (see *Mirror files*). All zeros without one.
   * - ``modesFile``
     - str
     - The mode matrix (see *Mirror files*). Without one, ``setModes`` is
       refused.
   * - ``firstMode``
     - int
     - The number of the matrix's first row, e.g. 2 for a Zernike matrix
       that starts at tip (Noll 2). Default 1.
   * - ``modeNumbers``
     - list of int
     - The number of every row, for a matrix whose modes are not
       consecutive; replaces ``firstMode``. Microscope_GUI's modal control
       matrix (``control_matrix_modal_*.tif``) has the 16 modes Noll 1-13,
       16, 17 and 22: ``[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16, 17, 22]``.
   * - ``maxCommand``
     - float
     - The largest absolute value sent to any actuator. Default 1.

**Low-level dependencies**

None. Needs the ALPAO SDK on the computer (its installer puts the Python
bindings in ``Samples\Python3``).

**Source**

`AlpaoDMManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/deformableMirrors/AlpaoDMManager.py>`_
