*******************************
Adding support for more devices
*******************************

Imswitch2's hardware control module supports four main device types:
**detectors**, **lasers**, **positioners**, and **rotators**.
In order to add support for a new device,
a corresponding device manager class must be implemented in Imswitch2's code.

For practical, task-oriented guides, see also:

* :doc:`how-to/wire-teensy` — wire a Teensy pulse generator into your setup.
* :doc:`how-to/add-pulse-generator-backend` — implement a new ``PulseGeneratorManager`` backend.
* :doc:`how-to/port-from-third-party` — port a driver from a sibling project, with patterns and pitfalls drawn from the WidefieldStarss integration.

For per-device JSON config reference (what fields each existing manager accepts), see:

* :doc:`devices/detectors` — every ``DetectorManager`` with its ``managerProperties``.
* :doc:`devices/lasers` — every ``LaserManager``.
* :doc:`devices/positioners` — every ``PositionerManager``.
* :doc:`devices/rotators` — every ``RotatorManager``.


How device managers are implemented
===================================

Detector support is implemented in device manager classes derived from the abstract base class ``DetectorManager``.
The corresponding parent class for lasers is ``LaserManager``,
for positioners it is ``PositionerManager``,
and for rotators it is ``RotatorManager``.
These derived classes are placed in the ``detectors``, ``lasers``, ``positioners`` and ``rotators`` sub-modules respectively in the ``imswitch.imcontrol.model.managers`` module.

The required constructor signature for the device managers is ``__init__(deviceInfo, name, **lowLevelManagers)``.
``deviceInfo`` is the ``DetectorInfo``, ``LaserInfo`` or ``PositionerInfo`` object which represents the device's entry in the setup file
(see :doc:`the hardware control setup page <imcontrol-setups>` for further information).
Inside it, the ``managerProperties`` dict field may contain manager-specific properties.
``name`` is a unique name that is used to identify the device,
which is defined by the key of the device's entry in the setup file.
``lowLevelManagers`` is a dict containing objects that facilitate low-level device interaction,
which are documented :ref:`here <Available low-level managers>`.
Note that ``super().__init__`` has a different signature, depending on which base class is used.

When creating a new device manager,
you will need to implement all the abstract methods and properties defined in the base class.
You should avoid overriding non-abstract properties.
Overriding non-abstract methods is generally fine,
but you should make sure that they continue to work as expected.
The device manager class must be placed in a .py file with the same name as the class,
in the appropriate location as outlined above.
No other action is required for the device manager to be available to use;
it will automatically be managed by a multi-manager as outlined in `the original ImSwitch JOSS paper <https://doi.org/10.21105/joss.03394>`_.

A simple reference implementation lives in-tree at
``imswitch/imcontrol/model/managers/positioners/NidaqPositionerManager.py``.


Base class documentation
========================

DetectorManager
---------------

.. autoclass:: imswitch.imcontrol.model.managers.detectors.DetectorManager.DetectorManager
   :members:
   :special-members: __init__

.. autoclass:: imswitch.imcontrol.model.managers.detectors.DetectorManager.DetectorAction
   :members:
   :inherited-members:

.. autoclass:: imswitch.imcontrol.model.managers.detectors.DetectorManager.DetectorParameter

.. autoclass:: imswitch.imcontrol.model.managers.detectors.DetectorManager.DetectorNumberParameter
   :members:
   :inherited-members:
   :show-inheritance:

.. autoclass:: imswitch.imcontrol.model.managers.detectors.DetectorManager.DetectorListParameter
   :members:
   :inherited-members:
   :show-inheritance:


LaserManager
------------

.. autoclass:: imswitch.imcontrol.model.managers.lasers.LaserManager.LaserManager
   :members:
   :special-members: __init__


PositionerManager
-----------------

.. autoclass:: imswitch.imcontrol.model.managers.positioners.PositionerManager.PositionerManager
   :members:
   :special-members: __init__


RotatorManager
--------------

.. autoclass:: imswitch.imcontrol.model.managers.rotators.RotatorManager.RotatorManager
   :members:
   :special-members: __init__


Available low-level managers
============================

lowLevelManagers['nidaqManager']
--------------------------------

.. autoclass:: imswitch.imcontrol.model.managers.NidaqManager.NidaqManager
   :members: setAnalog, setDigital


lowLevelManagers['rs232sManager']
---------------------------------

.. autoclass:: imswitch.imcontrol.model.managers.RS232sManager.RS232sManager
   :members:

.. autoclass:: imswitch.imcontrol.model.managers.rs232.RS232Manager.RS232Manager
   :members:


lowLevelManagers['pulseGeneratorManager']
-----------------------------------------

Backend-agnostic digital pulse generator (Teensy / Arduino today;
PulseStreamer once migrated; future NI / FPGA backends).  Available
when ``setupInfo.teensyPulse`` (or future equivalent) is configured;
``None`` otherwise.  Managers that depend on it should follow the
mock-mode fallback pattern from
:class:`~imswitch.imcontrol.model.managers.lasers.PulseGeneratorLaserManager.PulseGeneratorLaserManager`:

.. code-block:: python

    self._pulseGen = lowLevelManagers.get('pulseGeneratorManager')
    self._isMock = self._pulseGen is None

See :doc:`how-to/wire-teensy` for a worked example of consuming the
pulse generator from a laser manager, and
:doc:`how-to/add-pulse-generator-backend` for implementing a new
backend.

.. autoclass:: imswitch.imcontrol.model.managers.pulsegen.PulseGeneratorManager.PulseGeneratorManager
   :members:
