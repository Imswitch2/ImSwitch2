*******************************
Adding support for more devices
*******************************

ImSwitch2's hardware control module drives several kinds of device. This
page covers the four with an abstract manager base class: **detectors**,
**lasers**, **positioners** and **rotators**. A setup file can also declare
RS232 devices, SLMs, flip mirrors, a microscope stand and a pulse generator;
see :doc:`setupinfo-reference`.

.. note::

   **For new device support, prefer an external plugin package.** ImSwitch2 can
   discover and load device managers from installed plugins without any change
   to the core repository — see :doc:`devices/plugins`. Implement an in-tree
   manager (as described on this page) only for core/reference devices or
   tightly-coupled infrastructure. In-tree managers and plugin managers share
   the same base-class contracts, so the guidance below applies to both.

In order to add an **in-tree** device, a corresponding device manager class is
implemented in ImSwitch2's code as described below.

.. toctree::
   :hidden:

   devices/plugins

For practical, task-oriented guides, see also:

* :doc:`how-to/wire-teensy` — wire a Teensy pulse generator into your setup.
* :doc:`how-to/add-pulse-generator-backend` — implement a new ``PulseGeneratorManager`` backend.
* :doc:`how-to/port-from-third-party` — port a driver from a sibling project, with patterns and pitfalls drawn from the WidefieldStarss integration.

For per-device JSON config reference (what fields each existing manager accepts), see:

* :doc:`devices/detectors` — every ``DetectorManager`` with its ``managerProperties``.
* :doc:`devices/lasers` — every ``LaserManager``.
* :doc:`devices/positioners` — every ``PositionerManager``.
* :doc:`devices/rotators` — every ``RotatorManager``.
* :doc:`devices/stands` — microscope stand integrations configured through ``microscopeStand``.


How device managers are implemented
===================================

Detector support is implemented in device manager classes derived from the abstract base class ``DetectorManager``.
The corresponding parent class for lasers is ``LaserManager``,
for positioners it is ``PositionerManager``,
and for rotators it is ``RotatorManager``.
These derived classes are placed in the ``detectors``, ``lasers``, ``positioners`` and ``rotators`` sub-modules respectively in the ``imswitch.imcontrol.model.managers`` module.

The required constructor signature for the device managers is ``__init__(deviceInfo, name, **lowLevelManagers)``.
``deviceInfo`` is the ``DetectorInfo``, ``LaserInfo`` or ``PositionerInfo`` object (a plain ``DeviceInfo`` for rotators) which represents the device's entry in the setup file
(see :doc:`the hardware control setup page <imcontrol-setups>` for further information).
Inside it, the ``managerProperties`` dict field may contain manager-specific properties.
``name`` is a unique name that is used to identify the device,
which is defined by the key of the device's entry in the setup file.
``lowLevelManagers`` is a dict containing objects that facilitate low-level device interaction,
which are documented :ref:`here <available-low-level-managers>`.
Note that ``super().__init__`` has a different signature, depending on which base class is used.

When creating a new device manager,
you will need to implement all the abstract methods and properties defined in the base class.
You should avoid overriding non-abstract properties.
Overriding non-abstract methods is generally fine,
but you should make sure that they continue to work as expected.
The device manager class must be placed in a .py file with the same name as the class,
in the appropriate location as outlined above.
At run time nothing else is needed: a setup entry whose ``managerName`` is the class name
loads it, and it is managed by a multi-manager as outlined in `the original ImSwitch JOSS paper <https://doi.org/10.21105/joss.03394>`_.

The test suite keeps an inventory of the in-tree managers, so CI fails for a new one until you also:

* register it in ``imswitch/imcontrol/model/plugins/builtins.py`` (and update the registered count
  asserted in ``imswitch/imcontrol/_test/unit/test_setup_metadata.py``), or add its ``(kind, name)``
  pair to ``_UNREGISTERED_CORE_MANAGERS`` in that test;
* regenerate the configuration-editor schemas with ``python tools/extract_manager_schemas.py --write``
  and update the manager count asserted in
  ``imswitch/imcontrol/_test/unit/test_configeditor_schemas_match_source.py``;
* add a card for it, listing every ``managerProperties`` key it reads, to its page under
  ``docs/devices/`` (checked by ``test_devices_docs_drift.py``), or add its name to that test's
  ``UNDOCUMENTED`` set.

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


.. _available-low-level-managers:

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


lowLevelManagers['triggerScopeManager']
---------------------------------------

Present only when the setup file has a ``triggerScope`` block: a
``TriggerScopeManager`` (``imswitch/imcontrol/model/managers/TriggerScopeManager.py``)
that owns the serial connection to a TriggerScope board and its raw DAC/TTL
primitives.  ``TriggerScopeLaserManager`` and ``TriggerScopePositionerManager``
use it.  Without that block the key is absent, so read it with
``lowLevelManagers.get('triggerScopeManager')``.


lowLevelManagers['pulseGeneratorManager']
-----------------------------------------

Backend-agnostic digital pulse generator.  The low-level manager is
built only from a ``teensyPulse`` block in the setup file, as a
``TeensyPulseManager`` (Teensy / Arduino); it is ``None`` otherwise.
``PulseStreamerManager`` also implements the pulse-generator interface,
but ``MasterController`` does not construct it: a ``pulseStreamer`` block
is ignored, and setup validation says so.  Managers that depend on the
pulse generator should follow the mock-mode fallback pattern from
``PulseGeneratorLaserManager``:

.. code-block:: python

    self._pulseGen = lowLevelManagers.get('pulseGeneratorManager')
    self._isMock = self._pulseGen is None

See :doc:`how-to/wire-teensy` for a worked example of consuming the
pulse generator from a laser manager, and
:doc:`how-to/add-pulse-generator-backend` for implementing a new
backend.

.. autoclass:: imswitch.imcontrol.model.managers.pulsegen.PulseGeneratorManager.PulseGeneratorManager
   :members:
