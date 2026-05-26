No-Hardware Validation
======================

ImSwitch2 includes a no-hardware validation profile for testing configuration
parsing, manager construction, controller contracts, and pure model logic
without connected microscope hardware.

.. important::

   No-hardware validation is not hardware safety validation. It must not enable
   lasers, move stages, start DAQ tasks, or communicate with physical devices.

Run the suite
-------------

Run the current no-hardware suite from the repository root:

.. code-block:: bash

   QT_QPA_PLATFORM=offscreen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytestqt.plugin \
     imswitch/imcontrol/_test/unit \
     imswitch/test_no_hardware_profile.py -q

``QT_QPA_PLATFORM=offscreen`` keeps Qt headless. This is required for CI and
for local runs where no display server should be used.

``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` prevents unrelated third-party pytest
plugins from changing collection or Qt fixture behavior. ``pytest-qt`` is then
loaded explicitly with ``-p pytestqt.plugin``.

Scope
-----

No-hardware tests may:

* parse setup/configuration files,
* instantiate managers with mock devices or simulation mode,
* test controller logic through mock signals,
* test pure model functions and state persistence,
* verify source-level contracts such as signal names and method existence.

No-hardware tests must not:

* enable lasers,
* move stages or positioners,
* start DAQ acquisition or generate physical pulses,
* require RS232, USB, NI-DAQ, camera, or other physical devices,
* depend on hardware timing or synchronization,
* modify real user hardware configuration outside the test environment.

Configuration profile
---------------------

The baseline profile is:

``imswitch/_data/user_defaults/imcontrol_setups/example_no_hardware.json``

It uses mock/simulated devices and must remain free of physical IO channels.
The corresponding validation entry point is:

``imswitch/test_no_hardware_profile.py``

Adding tests
------------

When adding new no-hardware tests:

* keep imports narrow and avoid importing the full GUI stack during collection,
* prefer pure model tests or source-level contract tests where possible,
* use mock widgets/controllers/managers for controller behavior,
* isolate temporary files with ``tmp_path`` or monkeypatched state directories,
* add explicit assertions that no physical channel or hardware action is used.

The detailed Markdown guide lives at ``docs/no-hardware-validation.md`` for
agent-facing workflows and examples.
