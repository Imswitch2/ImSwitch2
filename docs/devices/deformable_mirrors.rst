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
  for one unit of mode ``firstMode + k``. ``setModes`` sends the flat plus
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
   * - ``maxCommand``
     - float
     - The largest absolute value sent to any actuator. Default 1.

**Low-level dependencies**

None. Needs the ALPAO SDK on the computer (its installer puts the Python
bindings in ``Samples\Python3``).

**Source**

`AlpaoDMManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/deformableMirrors/AlpaoDMManager.py>`_
