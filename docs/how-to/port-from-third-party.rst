*************************************************
Port a device manager from a third-party project
*************************************************

When a sibling project (in-house code, a fork, an open-source
microscopy stack) has a driver for hardware ImSwitch doesn't support
yet, the most efficient path is usually to wrap it as an ImSwitch
manager rather than rewrite from scratch.

This guide distills the patterns we used porting five managers from
the WidefieldStarss project — see ``docs/design/plans/ws-integration.md``
for the full historical plan.


When porting is the right call
==============================

Port (don't rewrite) if the third-party driver:

* talks to the hardware *correctly* (sane errors, no leaks, no
  device-state surprises), and
* is reasonably small (a few hundred lines at most), and
* has no hard dependencies on its surrounding application (Qt
  widgets, app config singletons, app-level signal buses).

If any of those break down, treat the third-party code as a *reference
implementation* rather than something to copy verbatim.  Read it,
understand what the wire protocol or SDK call sequence actually does,
then write a clean ImSwitch-shaped wrapper around the same library.


Step 1 — Identify the device category
=====================================

ImSwitch has fixed abstract bases for each device kind.  Pick the one
that matches:

.. list-table::
   :widths: 15 60
   :header-rows: 1

   * - Category
     - Base class
   * - ``detector``
     - ``imswitch.imcontrol.model.managers.detectors.DetectorManager``
   * - ``laser``
     - ``imswitch.imcontrol.model.managers.lasers.LaserManager``
   * - ``positioner``
     - ``imswitch.imcontrol.model.managers.positioners.PositionerManager``
   * - ``rotator``
     - ``imswitch.imcontrol.model.managers.rotators.RotatorManager``
   * - *other*
     - Promote to a singleton "low-level" manager (see :class:`~imswitch.imcontrol.controller.MasterController.MasterController`)

If the device doesn't fit cleanly — e.g. it's an instrument that
produces scalar measurements rather than images, or it's a timing
device that doesn't map to any existing category — flag that as a
design question before writing code.  The pulse generator subsystem
is an example of how to add a new low-level singleton category.


Step 2 — Decide on the driver / manager split
=============================================

Most ports want two files, not one:

* **Driver** under ``imswitch/imcontrol/model/interfaces/<vendor>.py``
  — pure I/O, no ImSwitch types.  This is where you import the
  vendor SDK (lazily, behind try/except).  This is also where you
  put a hardware-free mock so tests don't need the device.

* **Manager** under ``imswitch/imcontrol/model/managers/<category>/<Name>Manager.py``
  — implements the abstract base, owns ImSwitch-side concerns
  (signal emission, lifecycle, ``managerProperties`` parsing), and
  delegates all hardware talking to the driver.

This split is what makes the result testable.  Concrete examples:

* ``interfaces/teensypulse.py`` (real driver + mock) +
  ``managers/pulsegen/TeensyPulseManager.py``
* ``interfaces/kinesisstage.py`` +
  ``managers/positioners/KinesisStageManager.py``


Step 3 — Implement the manager with a mock fallback
===================================================

The canonical pattern (see ``KinesisRotatorManager.py`` for a minimal
example, ``TeensyPulseManager.py`` for the full one):

.. code-block:: python

    def _open_driver(self, info):
        try:
            return RealDriver(info.port)
        except Exception as e:
            if not info.useMockOnFailure:
                raise
            self._logger.warning(
                f'Failed to open device on {info.port}: {e}; '
                f'falling back to mock'
            )
            return MockDriver()

This gives you three operating modes:

1. **Real hardware connected** — the manager talks to the device.
2. **Hardware unavailable, mock fallback enabled** — manager loads
   silently with a warning; CI and headless development keep working.
3. **Hardware unavailable, mock fallback disabled** — startup fails
   with a clear error.  Useful for production setups where a missing
   device must surface immediately.

Set the default to mock-on-failure ``True`` unless you have a specific
reason otherwise.


Step 4 — Resist policy creep into the driver
============================================

The single biggest cleanup task when porting is removing
application-policy code from what should be a driver.  Typical
things to leave behind:

* **Calibrated positions / setpoints.**  ``move_to_h()`` /
  ``move_to_v()`` belong in user scripts, not in the manager.  Surface
  them via ``move_abs(h_pos)``.
* **Keypress / mouse / widget bindings.**  ImSwitch controllers wire
  UI events, not managers.
* **Plotting and analysis code.**  Drivers produce data; analysis is
  a separate concern.
* **Sleep-based polling for completion in synchronous calls.**
  ImSwitch's manager API usually returns once the device acknowledges;
  long synchronous waits should be made async or thread-pooled.


Step 5 — Plumb the setup file
=============================

Each device gets a JSON entry under the appropriate top-level dict
(``detectors``, ``lasers``, ``positioners``, ``rotators``).  The
``managerName`` must exactly match your class name; ``managerProperties``
is a free-form dict the manager parses.  Example for a rotator port::

    "rotators": {
        "hwp": {
            "managerName": "ElliptecRotatorManager",
            "managerProperties": {
                "port": "COM20",
                "address": 1,
                "scale": "stage"
            }
        }
    }

For singleton low-level managers (NI-DAQ, pulse generator, RS232), add
a top-level field to ``SetupInfo`` as a frozen dataclass — see
``TeensyPulseInfo`` for the template — and wire it in
``MasterController.__init__``.


Step 6 — Test before you trust
==============================

For each port, target **three test layers**:

1. **Driver-level mock tests** — drive the in-process mock through
   its public API; assert state.
2. **Driver-level wire-protocol cross-check** (where applicable) —
   use a fake transport (e.g. a fake ``serial.Serial``) to exercise
   the real driver against a simulated device.  This catches
   wire-format bugs the high-level mock can't.
3. **Manager-level integration tests** — boot the manager through a
   minimal ``setupInfo``-like config, exercise its API, assert the
   downstream mock state.

See ``test_teensypulse_driver.py``, ``test_teensy_pulse_manager.py``,
and ``test_pulse_generator_integration.py`` for a worked example of
all three layers.


Worked example: WidefieldStarss ports
=====================================

The full plan with per-port code review lives in
``docs/design/plans/ws-integration.md``.  Quick summary of what each
port involved:

============================  =============================  =====================================
Port                          Hardest part                   Pattern that made it work
============================  =============================  =====================================
``KinesisRotatorManager``     None — straight wrapping       Lazy vendor import + mock fallback
``ElliptecRotatorManager``    Multidrop bus sharing          Refcounted per-COM-port singleton bus
``JenaPiezoZManager``         Polling/retry settle loop      Configurable timeout + clean timeout error
``KinesisStageManager``       No jog API on the base class   Added default no-op ``jog_start``/``jog_stop``
``ThorCamTSIManager``         DLL bootstrap + threading      Driver/mock split + drop WFS thread model
============================  =============================  =====================================

Common to all: keep the driver pure, keep the manager small, write
the mock alongside the real driver.


Cross-references
================

- :doc:`add-pulse-generator-backend` — adding a new pulse-generator
  backend, including the abstract-base lessons.
- :doc:`/adding-device-support` — base-class reference for detectors,
  lasers, positioners.
- ``docs/design/plans/ws-integration.md`` — full WS integration plan
  with per-port analysis and architectural decisions.
