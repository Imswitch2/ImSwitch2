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

CI runs the no-hardware tests on every pull request in three lanes
(``.github/workflows/ci.yml``):

* ``unit``: ``imswitch/test_no_hardware_profile.py``,
  ``imswitch/imcommon/_test``, ``imswitch/imcontrol/_test/unit`` and
  ``imswitch/imcontrol/controller/_test``;
* ``improcess``: ``imswitch/improcess/_test``, without
  ``imswitch/improcess/_test/test_snouty.py``;
* ``core``: ``imswitch/imcommon/_test`` and ``imswitch/imscripting/_test``.

To run all three the way CI does, from the repository root after
``pip install -e ".[test]"``:

.. code-block:: bash

   QT_QPA_PLATFORM=offscreen MPLBACKEND=Agg PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
   python -m pytest -p xdist.plugin -p pytestqt.plugin -p pytest_timeout -n auto --timeout=180 \
       imswitch/test_no_hardware_profile.py imswitch/imcontrol/_test/unit \
       imswitch/imcontrol/controller/_test imswitch/improcess/_test \
       imswitch/imcommon/_test imswitch/imscripting/_test \
       --ignore=imswitch/improcess/_test/test_snouty.py

``QT_QPA_PLATFORM=offscreen`` keeps Qt headless, and ``MPLBACKEND=Agg`` keeps
matplotlib from loading an interactive backend. This is required for CI and
for local runs where no display server should be used.

``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` prevents unrelated third-party pytest
plugins from changing collection or Qt fixture behavior. ``pytest-xdist``,
``pytest-qt`` and ``pytest-timeout`` are then loaded explicitly with the three
``-p`` options; ``-n auto`` spreads the tests over all CPU cores and
``--timeout=180`` stops a hung test.

``imswitch/test_no_hardware_ui_smoke.py`` (see below) and the UI tests in
``imswitch/imcontrol/_test/ui`` are in none of these lanes. They run only in
the manually started ``imswitch-test`` workflow, which runs the whole package
on a virtual display, or when you run them yourself.

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

``imswitch/test_no_hardware_ui_smoke.py`` constructs the ImControl
view/controller graph with the no-hardware setup. It stubs heavy GUI rendering
dependencies at the test boundary so the smoke test validates ImSwitch2 startup
without requiring a compatible local napari/vispy/matplotlib stack. When it
closes the window, ImControl asks the modal *Save Widget State* question;
the test does not answer it, so run offscreen the test waits there and does
not finish, and ``--timeout`` does not stop it.

Adding tests
------------

When adding new no-hardware tests:

* keep imports narrow and avoid importing the full GUI stack during collection,
* prefer pure model tests or source-level contract tests where possible,
* use mock widgets/controllers/managers for controller behavior,
* isolate temporary files with ``tmp_path`` or monkeypatched state directories,
* add explicit assertions that no physical channel or hardware action is used.

The repository also keeps a longer working note,
``docs/no-hardware-validation.md``, with agent-facing workflows and examples;
it is not part of the published documentation. For an inventory of the mock
managers/classes themselves (what exists per device family, and their known
limitations), see :doc:`mock-infrastructure`.
