**********************
Developer Onboarding
**********************

Welcome to ImSwitch2 development! This guide covers the essentials for setting
up your environment, running tests, and contributing safely.

.. note::

   **Before you start:** ImSwitch2 controls real microscope hardware. Incorrect
   changes can damage equipment or create unsafe conditions. Always follow the
   safety guidelines in this document and ``AGENTS.md``.


Quick Start
============

1. **Clone and install** (see :doc:`installation` for details):

   .. code-block:: bash

      git clone https://github.com/Imswitch2/Imswitch2.git
      cd Imswitch2
      pip install -e .
      pip install -r requirements-dev.txt

2. **Run no-hardware validation** to verify your setup:

   .. code-block:: bash

      QT_QPA_PLATFORM=offscreen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytestqt.plugin \
        imswitch/imcontrol/_test/unit \
        imswitch/test_no_hardware_profile.py -q

3. **Run linting** to check code style:

   .. code-block:: bash

      ruff check imswitch/

4. **Launch the application** with no-hardware mode:

   .. code-block:: bash

      python -m imswitch

   Select ``example_no_hardware.json`` from the setup picker.


Understanding the Codebase
===========================

Before making changes, familiarize yourself with the architecture:

* **Architecture overview:** ``docs/design/ARCHITECTURE.md``
  
  * Manager inventory (hardware abstraction layer)
  * Controller→Manager matrix (which controllers use which hardware)
  * Startup flow (initialization sequence)

* **AI agent rules:** ``AGENTS.md``

  * Red-zone files (hardware control, requires mandatory human review)
  * Coding standards and commit discipline
  * No-hardware vs hardware testing boundaries

* **No-hardware validation:** ``docs/no-hardware-validation.md``

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

**Testing:** Run the no-hardware test suite:

.. code-block:: bash

   QT_QPA_PLATFORM=offscreen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytestqt.plugin \
     imswitch/imcontrol/_test/unit \
     imswitch/test_no_hardware_profile.py -v

**No physical hardware required.** Mock devices and simulated DAQ are sufficient.

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

See ``AGENTS.md`` for the complete list and identification patterns.


Key Validation Commands
========================

Before Opening a Pull Request
-------------------------------

Run these checks locally before pushing:

1. **Linting** (critical lint errors must be zero):

   .. code-block:: bash

      ruff check imswitch/

   Fix any issues with:

   .. code-block:: bash

      ruff check --fix imswitch/

2. **No-hardware tests** (must all pass):

   .. code-block:: bash

      QT_QPA_PLATFORM=offscreen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytestqt.plugin \
        imswitch/imcontrol/_test/unit \
        imswitch/test_no_hardware_profile.py -v

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

Use conventional commit format:

.. code-block:: text

   type(scope): brief description

   Optional detailed explanation.

   Co-authored-by: openhands <openhands@all-hands.dev>

**Types:**

* ``feat:``: New feature
* ``fix:``: Bug fix
* ``docs:``: Documentation changes
* ``test:``: Test additions or updates
* ``refactor:``: Code restructuring without behavior change
* ``style:``: Formatting changes (no logic change)
* ``chore:``: Build, CI, or tooling changes

**Example:**

.. code-block:: text

   feat(laser): add widget state persistence for laser power

   Implements getWidgetState() and setWidgetState() for LaserController.
   Persists laser power values and modulation settings, but NOT enable
   states (safety requirement).

   Co-authored-by: openhands <openhands@all-hands.dev>


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
          from imswitch.imcontrol.model.managers import LaserManager
          
          # Test logic here
          assert True

3. **Run locally:**

   .. code-block:: bash

      QT_QPA_PLATFORM=offscreen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytestqt.plugin \
        imswitch/imcontrol/_test/unit/test_your_new_file.py -v

4. **Add to PR** with clear description of what's tested.


Modifying Configuration Files
-------------------------------

ImSwitch2 uses JSON files for hardware configuration (``~/ImSwitchConfig/imcontrol_setups/``).

**To add a new configuration option:**

1. **Update the model** in ``imswitch/imcontrol/model/SetupInfo.py``
2. **Add validation** if needed (range checks, type validation)
3. **Update documentation** in ``docs/imcontrol-setups.rst``
4. **Add a test** verifying the new option parses correctly
5. **Provide example** in ``example_no_hardware.json`` or a new example file


Debugging Tips
--------------

**Enable debug logging:**

.. code-block:: bash

   python -m imswitch --log-level DEBUG

**Use the mock camera for UI testing:**

In your setup JSON:

.. code-block:: json

   {
     "detectors": {
       "Mock Camera": {
         "managerName": "AVManager",
         "managerProperties": {
           "cameraListIndex": "mock",
           "exposure": 50
         }
       }
     }
   }

**Check CI logs:**

If tests fail in CI but pass locally, check:

1. Are you using ``QT_QPA_PLATFORM=offscreen``?
2. Are you explicitly loading pytest-qt?
3. Are you importing napari/matplotlib during test collection?

See ``docs/no-hardware-validation.md`` troubleshooting section.


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

   feature/laser-state-persistence
   fix/scan-widget-layout
   docs/update-installation-guide
   test/add-positioner-validation


Getting Help
============

* **Read the docs first:**

  * :doc:`installation` — Setup and dependencies
  * :doc:`contributing` — General contribution guidelines
  * :doc:`adding-device-support` — Hardware integration
  * ``docs/design/ARCHITECTURE.md`` — System architecture
  * ``docs/no-hardware-validation.md`` — Testing guide

* **Check existing issues:**

  * `GitHub Issues <https://github.com/Imswitch2/Imswitch2/issues>`_
  * `GitHub Discussions <https://github.com/Imswitch2/Imswitch2/discussions>`_

* **Open a discussion** before starting large changes:

  * Propose your approach
  * Get feedback from maintainers
  * Avoid wasted effort on rejected designs


Resources
=========

**Documentation:**

* :doc:`installation` — Installation and setup
* :doc:`contributing` — How to contribute
* :doc:`adding-device-support` — Adding new hardware
* ``docs/design/ARCHITECTURE.md`` — Architecture overview
* ``docs/no-hardware-validation.md`` — Testing without hardware
* ``AGENTS.md`` — AI agent rules and red-zone files
* ``ROADMAP.md`` — Project roadmap and milestones

**External:**

* `ImSwitch2 GitHub <https://github.com/Imswitch2/Imswitch2>`_
* `Python Community Guidelines <https://www.python.org/psf/conduct/>`_
* `Conventional Commits <https://www.conventionalcommits.org/>`_


Welcome Aboard!
===============

ImSwitch2 is built by the microscopy community, for the microscopy community.
Your contributions — whether bug fixes, new features, documentation, or testing
— help make open-source microscopy better for everyone.

**Thank you for contributing!**
