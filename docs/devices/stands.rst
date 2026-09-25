******************************
Microscope stands - reference
******************************

This page documents microscope stand managers configured through the
top-level ``"microscopeStand"`` setup section.  Stand managers do not live
inside a named device map like ``"positioners"`` or ``"lasers"``; the setup
contains at most one stand object, whose ``managerName`` selects the
implementation and whose ``managerProperties`` holds stand-specific options.

For the complete top-level setup shape, see
:doc:`/setupinfo-reference`.


How stands are configured
=========================

``microscopeStand`` deserialises into ``MicroscopeStandInfo``
(``imswitch.imcontrol.model.SetupInfo``).  Its
top-level ``rs232device`` field names the RS-232 connection used by the stand
manager; manager-specific fields stay under ``managerProperties``.

.. code-block:: json

    "microscopeStand": {
        "managerName": "<one of the classes below>",
        "rs232device": "LeicaStand",
        "managerProperties": { "...": "..." }
    }

``managerName`` is resolved like the other device kinds: first through the
device plugin registry (see :doc:`plugins`), then as a module of the same
name in ``imswitch.imcontrol.model.managers.stands``.  When neither finds
it, the loader tries a mock counterpart before giving up: the registry
name ``<managerName>_mock``, then a module ``<managerName>_mock`` with a
class ``Mock<managerName>``.  It logs a warning when it loads one.  In
the current tree only the legacy name ``LeicaDMIManager`` has such a
counterpart, so a setup naming it loads the mock stand described below.


Mock stand
==========

``LeicaDMIStandMockManager`` is a built-in stand for setups without Leica
hardware; the config editor offers it as the default ``managerName`` for a
new ``microscopeStand`` section.  It is also registered under the aliases
``LeicaDMIManager_mock``, ``MockLeicaDMIManager`` and
``builtin.leica-dmi-stand-mock``.  It reads no ``managerProperties``.  It
writes plain numeric commands to the ``rs232devices`` entry that
``rs232device`` names; when that entry does not exist it logs an error
and uses an in-process mock port instead.


LeicaDMIStandManager
====================

Leica DMI microscope stand adapter over the shared Leica DMI RS-232 hardware
interface.  It exposes stand and accessory operations such as filter-cube
selection, transmitted-light shutter control, objective turret position and
motorized correction-collar control.

**Setup JSON**

.. code-block:: json

    "microscopeStand": {
        "managerName": "LeicaDMIStandManager",
        "rs232device": "LeicaStand",
        "managerProperties": {
            "availableCubes": {
                "1": "BF",
                "2": "GFP",
                "3": "RFP"
            }
        }
    }

**managerProperties**

.. list-table::
   :widths: 25 12 18 45
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``availableCubes``
     - dict
     - ``{}``
     - Optional mapping from Leica cube slot numbers to display names.  Slot
       keys are parsed as integers and invalid entries are ignored with a
       warning.

**MicroscopeStandInfo fields used**

* ``managerName`` selects this manager.
* ``rs232device`` is required and must name an entry in ``rs232devices``.
* ``managerProperties`` supplies the fields above.

**Low-level dependencies**

* ``rs232sManager[<rs232device>]`` - used to create or reuse the shared
  Leica DMI hardware interface.
* ``imswitch.imcontrol.model.interfaces.LeicaDMIHardware_private`` - private
  hardware implementation loaded by ``createLeicaDMIHardware``.  If the
  private implementation or RS-232 channel is unavailable, the stand remains
  unavailable and reports ``connectionError``.

**Vendor library**

None in the public manager.  Leica DMI transport details live behind the
shared hardware interface.

**Gotchas**

Use ``LeicaDMIZPositionerManager`` in ``positioners`` when the Leica DMI Z
focus drive should also be exposed as a positioner.  Both managers can share
the same Leica DMI hardware interface when they reference the same RS-232
connection.

**Source**

`LeicaDMIStandManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/stands/LeicaDMIStandManager.py>`_
