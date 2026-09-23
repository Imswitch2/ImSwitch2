Current State and Known Issues
==============================

This page summarizes the ImSwitch2 migration state as of May 26, 2026. It is
intended as a quick orientation document for maintainers, contributors, and
coding agents before starting new work.

Validation Baseline
-------------------

The current no-hardware validation baseline is:

.. code-block:: bash

   QT_QPA_PLATFORM=offscreen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytestqt.plugin \
     imswitch/imcontrol/_test/unit \
     imswitch/test_no_hardware_profile.py \
     imswitch/test_no_hardware_ui_smoke.py -q

The latest local no-hardware validation run passed with
``322 passed, 4 skipped``.

This validates configuration parsing, mock/simulated manager construction,
controller/model contracts, and no-hardware workflow logic. It does not validate
physical hardware safety, timing, laser emission, stage movement, or DAQ output.
See :doc:`no-hardware-validation` for the detailed workflow.

Stable Areas
------------

The following areas are currently considered stable enough for normal
development, subject to the red-zone rules in ``AGENTS.md``:

* Core import/package baseline with Python ``>=3.10``.
* Optional hardware extras split from the core install.
* No-hardware configuration profile and unit-test suite.
* No-hardware ImControl startup smoke test for the default no-hardware setup.
* Communication-channel signal inventory and compatibility aliases.
* Widget state persistence framework and UI save/load integration.
* Detector and scan controller state persistence for passive settings.
* Tiling/stitching workflow baseline.
* BeadRec 2.0 core architecture pass:

  * pure model helpers,
  * safer ROI/reconstruction handling,
  * worker ownership cleanup,
  * typed result records,
  * passive state persistence,
  * structured worker progress updates.

* Event-triggered EtSTED/EtMonalisa shared base hardening:

  * shared session state,
  * safer stop/close cleanup,
  * binary-mask cleanup,
  * fast-laser enable failure handling,
  * normalized pipeline coordinate output.

Active Work
-----------

The active roadmap areas are:

* ImControl backbone cleanup:

  * workflow services have been introduced,
  * deprecated compatibility signals remain available,
  * broad signal usage should be reduced only where no-hardware tests cover the
    behavior.

* Widget usability and responsiveness:

  * scrollability baseline has landed for several widgets,
  * Advanced Scan center controls allow negative values,
  * partial Advanced Scan BeadRec controls were removed,
  * a source-level sizing audit guards against selected hard-sizing regressions.

* Device-manager ports from WidefieldStarss:

  * several rotator/stage/device-manager branches have been reviewed or opened
    as PRs,
  * hardware verification is still required before treating those ports as
    production-ready.

* Milestone 7 documentation:

  * architecture map, no-hardware validation guide, current-state page,
    SetupInfo reference, developer onboarding, microscope-KB guide, and agent
    task templates are present.

Known Issues and Limitations
----------------------------

Sphinx documentation build
~~~~~~~~~~~~~~~~~~~~~~~~~~

The local documentation build can fail if the environment provides Sphinx 4.x:
``sphinxcontrib.applehelp`` currently requires Sphinx ``>=5.0``. This is a
tooling/environment issue, not a known page syntax issue. The docs environment
should be updated before enforcing a docs-build check locally or in CI.

Communication backbone
~~~~~~~~~~~~~~~~~~~~~~

``CommunicationChannel`` still carries legacy compatibility signals and broad
cross-controller coordination. Deprecated signals should not be removed until
public API compatibility and representative no-hardware startup/widget-set tests
are in place.

Widget layout
~~~~~~~~~~~~~

Several widgets still contain legitimate but review-worthy fixed sizes, graph
heights, and dense grid layouts. The current sizing audit intentionally checks
only narrow banned patterns. Broader layout modernization should be done
widget-by-widget with screenshots or constrained-viewport smoke tests.

BeadRec follow-up work
~~~~~~~~~~~~~~~~~~~~~~

The BeadRec core architecture pass is implemented, but follow-up UX and testing
work remains:

1. Replace the legacy run checkbox with clearer Start/Stop controls
2. Add richer typed worker error reporting
3. Consider optional ROI/default persistence
4. Add fake-controller tests for scan lifecycle and detector chunks

Event-triggered workflows
~~~~~~~~~~~~~~~~~~~~~~~~~

EtSTED and EtMonalisa now share the core event-triggered implementation. The
no-hardware contract is strong, but hardware-facing behavior still requires
physical review because these workflows involve lasers, scans, microscope stand
state, and DAQ-triggered acquisition.

Scanning and galvo modernization
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Milestone 9 is red-zone work. Known issues include detector sync offset,
linestep ``line_clock`` count, inconsistent ``phase_delay`` handling, and
galvo trajectory magic numbers. These must be addressed with narrow
no-hardware tests first and physical hardware verification before enabling
behavior changes.

Recording manager upgrade
~~~~~~~~~~~~~~~~~~~~~~~~~

The upstream live-recording/live-reconstruction work is intentionally deferred.
The current plan is to port the intended functionality later as clean reviewed
commits rather than merging WIP history.

Recommended Near-Term Priorities
--------------------------------

1. Finish Milestone 7 documentation:

   * SetupInfo configuration reference,
   * developer onboarding guide,
   * agent task templates.

2. Extend representative startup/widget smoke coverage beyond the default
   no-hardware widget set.

3. Continue widget usability cleanup in small, isolated passes.

4. Keep Milestone 9 scan/DAQ timing changes under direct maintainer review with
   explicit risk notes and hardware-verification requirements.

Rules for New Work
------------------

Before starting a task:

1. Check ``AGENTS.md`` for red-zone boundaries
2. Prefer no-hardware tests first
3. Keep changes narrowly scoped
4. Do not remove compatibility signals or public APIs without review
5. Document risks in PR descriptions for any hardware-adjacent change
