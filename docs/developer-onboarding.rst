**********************
Developer Onboarding
**********************

Welcome to ImSwitch2 development! This guide covers the essentials for setting
up your environment, running tests, and contributing safely.

.. note::

   **Before you start:** ImSwitch2 controls real microscope hardware. Incorrect
   changes can damage equipment or create unsafe conditions. Always follow the
   safety guidelines in this document (see :ref:`red-zone-work`).


Quick Start
============

1. **Clone and install** (see :doc:`installation` for details):

   .. code-block:: bash

      git clone https://github.com/Imswitch2/ImSwitch2.git
      cd ImSwitch2
      pip install -e ".[test]" ruff

2. **Run the no-hardware tests** the way CI does, to verify your setup:

   .. code-block:: bash

      QT_QPA_PLATFORM=offscreen MPLBACKEND=Agg PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
      python -m pytest -p xdist.plugin -p pytestqt.plugin -p pytest_timeout -n auto --timeout=180 \
          imswitch/test_no_hardware_profile.py imswitch/imcontrol/_test/unit \
          imswitch/imcontrol/controller/_test imswitch/improcess/_test \
          imswitch/imcommon/_test imswitch/imscripting/_test \
          --ignore=imswitch/improcess/_test/test_snouty.py

   :doc:`no-hardware-validation` explains what these tests cover and what
   they cannot.

3. **Run linting** to check code style:

   .. code-block:: bash

      ruff check imswitch/

4. **Launch the application** with no-hardware mode:

   .. code-block:: bash

      python -m imswitch

   Select ``example_no_hardware.json`` from the setup picker, or one of the
   ``*mock*`` setups (for example ``mock_scan_setup.json``) to try scanning.


Understanding the Codebase
===========================

Before making changes, familiarize yourself with the architecture:

* **Architecture overview:** start with :ref:`architecture-at-a-glance` on
  the home page, which maps ImControl, ImProcess, ImScripting, ImCommon and
  PluginAPI onto the model-view-presenter layers.  Then read
  ``docs/design/ARCHITECTURE.md`` for the full dependency detail:

  * Manager inventory (hardware abstraction layer)
  * Controller→Manager matrix (which controllers use which hardware)
  * Startup flow (initialization sequence)

* **Red-zone rules:** :ref:`red-zone-work` below

  * Red-zone files (hardware control, requires mandatory human review)
  * No-hardware vs hardware testing boundaries

* **No-hardware validation:** :doc:`no-hardware-validation`

  * How to run tests without physical hardware
  * What tests may and may not do
  * Adding new tests safely


Development Workflow
====================

No-Hardware vs Hardware Work
------------------------------

ImSwitch2 distinguishes between **no-hardware work** (safe for all developers)
and **hardware/red-zone work** (requires physical hardware access and expert review).

No-Hardware Work (Safe)
^^^^^^^^^^^^^^^^^^^^^^^^

**What it includes:**

* Configuration parsing and validation
* Controller logic (without triggering hardware)
* Widget state persistence
* Pure model functions (calculations, transformations)
* UI layout and responsiveness improvements
* Documentation updates

**Testing:** Run the no-hardware tests (the command under *Quick Start*).

**No physical hardware required.** Mock devices and simulated DAQ are sufficient.

.. _red-zone-work:

Hardware/Red-Zone Work (Requires Expert Review)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

**What it includes:**

* DAQ timing and synchronization
* Laser control (power, enable/disable, modulation)
* Galvo scan generation (voltage waveforms, acceleration)
* TTL pulse generation (trigger sequences)
* Stage movement (positioning, velocity, limits)
* Hardware initialization and configuration

**Safety rules:**

1. **Never modify red-zone files** without explicit maintainer approval
2. **Never change hardware timing constants** without values from a maintainer
3. **Flag all red-zone changes** in PR descriptions with detailed risk explanations
4. **Request review** from someone with physical hardware access

**Red-zone files include** (but are not limited to):

* Files containing ``DAQ``, ``NI``, ``nidaq`` in names or imports
* Files in ``laser/``, ``positioner/``, ``stage/``, ``scanner/``, ``galvo/`` directories
* Files with ``TTL``, ``trigger``, ``pulse``, ``waveform`` in names
* Hardware manager initialization code

When in doubt, treat a file as red-zone and say so in the pull request.


Key Validation Commands
========================

Before Opening a Pull Request
-------------------------------

Run these checks locally before pushing:

1. **Linting** (CI checks critical errors only — syntax errors and undefined
   names, as configured in ``pyproject.toml`` — and they must be zero):

   .. code-block:: bash

      ruff check imswitch/

   Fix any issues with:

   .. code-block:: bash

      ruff check --fix imswitch/

2. **No-hardware tests** (must all pass):

   .. code-block:: bash

      QT_QPA_PLATFORM=offscreen MPLBACKEND=Agg PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
      python -m pytest -p xdist.plugin -p pytestqt.plugin -p pytest_timeout -n auto --timeout=180 \
          imswitch/test_no_hardware_profile.py imswitch/imcontrol/_test/unit \
          imswitch/imcontrol/controller/_test imswitch/improcess/_test \
          imswitch/imcommon/_test imswitch/imscripting/_test \
          --ignore=imswitch/improcess/_test/test_snouty.py

   The display-dependent UI tests (``imswitch/imcontrol/_test/ui`` and
   ``imswitch/test_no_hardware_ui_smoke.py``) are not in CI; run them locally
   when you change widgets.

3. **Whitespace check** (no trailing whitespace, newline at EOF):

   .. code-block:: bash

      git diff --check

4. **Compile check** (Python syntax validation):

   .. code-block:: bash

      python -m compileall -q imswitch/

5. **Type annotation** (if you added new functions):

   Add type hints to all new public functions and methods:

   .. code-block:: python

      def calculate_power(voltage: float, gain: float) -> float:
          """Calculate laser power from voltage and gain."""
          return voltage * gain


Pre-Commit Checklist
^^^^^^^^^^^^^^^^^^^^^

Before committing, verify:

.. code-block:: text

   ☑ All no-hardware tests pass
   ☑ Linting passes (ruff check imswitch/)
   ☑ No whitespace issues (git diff --check)
   ☑ Python compiles (python -m compileall -q imswitch/)
   ☑ Type hints added to new functions
   ☑ Docstrings added to new classes/functions
   ☑ No hardware-active states in test fixtures
   ☑ No red-zone files modified (or flagged with risk explanation)


Commit Message Guidelines
^^^^^^^^^^^^^^^^^^^^^^^^^^

Start the subject with the area the change touches and say what now
happens, in a sentence; the body explains why, and what was wrong before:

.. code-block:: text

   Scan: a refused scan design ends the request with its reason

   A scan longer than scan.maxScanTimeMin used to fail with a TypeError
   from makeFullScan. The request now ends with ScanRequestRejectedError
   carrying the designer's reason, so a script can report it.

Conventional-commit prefixes (``fix(cobolt): …``, ``docs: …``) are also in
use in the history and are fine.  Keep one logical change per commit.


Common Development Tasks
========================

Adding a New Test
------------------

1. **Determine test category:**

   * ``@pytest.mark.nohardware``: Config parsing, manager construction, logic
   * ``@pytest.mark.ui``: Widget behavior, controller-view contracts (not in CI yet)
   * ``@pytest.mark.redzone``: Hardware timing simulation (not in CI yet)
   * ``@pytest.mark.hardware``: Physical device communication (never in CI)

2. **Write the test** in ``imswitch/imcontrol/_test/unit/``:

   .. code-block:: python

      import pytest

      pytestmark = pytest.mark.nohardware


      def test_laser_power_calculation():
          """Test laser power calculation without hardware."""
          from imswitch.pluginapi import LaserManager
          
          # Test logic here
          assert True

3. **Run locally:**

   .. code-block:: bash

      QT_QPA_PLATFORM=offscreen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytestqt.plugin \
        imswitch/imcontrol/_test/unit/test_your_new_file.py -v

4. **Add to PR** with clear description of what's tested.  A test in a
   *new* directory only runs in CI once that directory is added to one of
   the lanes in ``.github/workflows/ci.yml``.


Modifying Configuration Files
-------------------------------

ImSwitch2 uses JSON files for hardware configuration (``~/ImSwitchConfig/imcontrol_setups/``).

**To add a new configuration option:**

1. **Update the model** in ``imswitch/imcontrol/model/SetupInfo.py`` (or the
   manager that reads the property)
2. **Add validation** if needed (range checks, type validation)
3. **Regenerate the config-editor schemas** with
   ``python tools/extract_manager_schemas.py --write`` and commit them; CI
   fails while they are out of date
4. **Update documentation** in :doc:`setupinfo-reference` (and the device
   card in ``docs/devices``, which a test checks against the schemas)
5. **Add a test** verifying the new option parses correctly
6. **Provide example** in ``example_no_hardware.json`` or a new example file


Debugging Tips
--------------

**Enable debug logging:**

.. code-block:: bash

   python -m imswitch --debug

**Use the mock camera for UI testing:**

In your setup JSON:

.. code-block:: json

   {
     "detectors": {
       "Mock Camera": {
         "managerName": "AVManager",
         "managerProperties": {
           "cameraListIndex": "mock",
           "avcam": {"exposure": 100, "gain": 1}
         }
       }
     }
   }

**Check CI logs:**

If tests fail in CI but pass locally, check:

1. Are you using ``QT_QPA_PLATFORM=offscreen``?
2. Are you explicitly loading pytest-qt?
3. Are you importing napari/matplotlib during test collection?

See :doc:`no-hardware-validation`.


Pull Request Guidelines
========================

What to Include
----------------

* **Clear title** describing the change
* **Description** explaining:

  * What problem this solves
  * How you tested it
  * Any breaking changes
  * Red-zone files touched (if applicable)

* **Risk explanation** if modifying hardware control code:

  * What could go wrong?
  * What hardware could be damaged?
  * Who reviewed the timing/voltage changes?

* **Tests** covering your changes (no PR without tests)

What NOT to Do
---------------

* **Don't push to main** — always use a feature branch
* **Don't merge without review** — all PRs require approval
* **Don't skip tests** — "it works on my machine" isn't enough
* **Don't modify red-zone files** without explicit approval
* **Don't commit generated files** — no ``__pycache__``, ``.pyc``, build artifacts


Branch Naming
-------------

Use descriptive branch names:

.. code-block:: text

   feat/laser-state-persistence
   fix/scan-widget-layout
   docs/update-installation-guide
   chore/pypi-release


Getting Help
============

* **Read the docs first:**

  * :doc:`installation` — Setup and dependencies
  * :doc:`contributing` — General contribution guidelines
  * :doc:`adding-device-support` — Hardware integration
  * ``docs/design/ARCHITECTURE.md`` — System architecture (in the repository)
  * :doc:`no-hardware-validation` — Testing guide

* **Check existing issues:**
  `GitHub Issues <https://github.com/Imswitch2/ImSwitch2/issues>`_

* **Open an issue** before starting large changes:

  * Propose your approach
  * Get feedback from maintainers
  * Avoid wasted effort on rejected designs


Resources
=========

**Documentation:**

* :doc:`installation` — Installation and setup
* :doc:`contributing` — How to contribute
* :doc:`adding-device-support` — Adding new hardware
* :doc:`no-hardware-validation` — Testing without hardware
* ``docs/design/ARCHITECTURE.md`` — Architecture overview (in the repository)
* ``ROADMAP.md`` — Project roadmap and milestones (in the repository)

**External:**

* `ImSwitch2 GitHub <https://github.com/Imswitch2/ImSwitch2>`_
* `Code of conduct <https://github.com/Imswitch2/ImSwitch2/blob/main/CODE_OF_CONDUCT.md>`_


Welcome Aboard!
===============

ImSwitch2 is built by the microscopy community, for the microscopy community.
Your contributions — whether bug fixes, new features, documentation, or testing
— help make open-source microscopy better for everyone.

**Thank you for contributing!**
