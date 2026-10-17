Current State and Known Issues
==============================

This page summarizes where ImSwitch2 stands as of 17 October 2026. It is a
quick orientation for maintainers, contributors and coding agents before
starting new work.  It replaces the milestone roadmap of the migration from
ImSwitch, whose milestones are done or were dropped; the items still open are
listed under *Open work* below.

Validation Baseline
-------------------

Every pull request runs the linter and the no-hardware tests in three lanes
(``unit``, ``improcess`` and ``core``); :doc:`contributing` gives the command
that runs them locally the way CI does.  Tests that need a display
(``imswitch/imcontrol/_test/ui`` and ``imswitch/test_no_hardware_ui_smoke.py``)
are not in CI; they run in the manually started ``imswitch-test`` workflow or
locally.  ``imswitch/improcess/_test/test_snouty.py`` is excluded from CI
because of a known failure in the SNOUTY CPU deskew.

These tests validate configuration parsing, mock and simulated manager
construction, controller and model contracts, and no-hardware workflow logic.
They do not validate physical hardware safety, timing, laser emission, stage
movement, or DAQ output. See :doc:`no-hardware-validation` for the detailed
workflow.

Where the Project Is
--------------------

ImSwitch2 runs the microscopes it was built for.  The real-world setup
validation planned during the migration from ImSwitch — the etSTED,
MoNaLISA 1 and 2, WidefieldSTARSS and SNOUTY rigs — has been carried out
successfully, and pull requests that touch hardware are tested on a real rig
before they merge.  The main open workstream is the scanning engine work
(scan engines and the expert scan, on their own branch) and the remaining
galvo/DAQ items below.

Known Issues and Limitations
----------------------------

Hardware validation
~~~~~~~~~~~~~~~~~~~

The no-hardware tests in CI do not exercise physical timing, laser emission,
stage movement or DAQ output, so anything that times lasers, scans, triggers
or stage moves is tested on a rig before it merges, and its pull request says
which.  The rig runs themselves are not yet written up in the documentation.

Scanning and galvo modernization
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This is red-zone work. Open issues include the detector-sync offset
(``phase_delay`` is applied on the detector side by the APD and PMT managers
but not to the ``line_clock`` the TimeTagger follows), the linestep
``line_clock`` count, and galvo trajectory magic numbers.  These must be
addressed with narrow no-hardware tests first and physical hardware
verification before enabling behavior changes.

Communication backbone
~~~~~~~~~~~~~~~~~~~~~~

The signals nothing emitted or listened to, and the unused domain alias
groups, were removed in October 2026. ``CommunicationChannel`` still carries
broad cross-controller coordination that belongs in services: the
request/response signal pairs for scan parameters and scan frequency, three
event-triggered on/off flags, and the scan-source and BeadRec accessor
registries. Signals exposed through ``signals()`` are public API and change
only with the scripting docs.

Event-triggered workflows
~~~~~~~~~~~~~~~~~~~~~~~~~

EtSTED and EtMonalisa share the core event-triggered implementation, which is
covered by no-hardware contract tests and runs on the etSTED and MoNaLISA
rigs.  Changes to it still need a rig run before they merge: these workflows
involve lasers, scans, microscope stand state and DAQ-triggered acquisition.

Widget layout
~~~~~~~~~~~~~

Several widgets still contain legitimate but review-worthy fixed sizes, graph
heights, and dense grid layouts. Broader layout modernization should be done
widget-by-widget with screenshots or constrained-viewport smoke tests.  The
BeadRec widget still starts and stops with a *Run* checkbox rather than
Start/Stop buttons.

Open work
~~~~~~~~~

Carried over from the migration roadmap; each is a candidate, not a
commitment.

* **Scanning.** Detector sync and the linestep ``line_clock`` count (above);
  a jerk-limited raster in place of the fixed acceleration points
  (``test_galvo_jerk_limit.py`` is skipped: it was written against an API
  that does not exist); galvo position-feedback pixel binning, a sinusoidal
  fast axis and an automatic lag calibration; a DAQ safety layer (voltage
  limits enforced, scan parameters validated in ``SetupInfo``, recovery from
  DAQ failures).
* **Point-scan performance.** Per-line ``d2Step`` event batching, the
  display-frame path and a configurable TimeTagger preview cadence; all
  rig-gated (``docs/design/plans/pointscan-confocal-sted-performance.md``).
* **Recording.** A hard RAM cap for ``SaveMode.RAM`` and an in-memory Zarr
  store; producer-driven acquisition (``ChunkBroker``) is parked.
* **Device plugins.** Publish the plugin packages under ``examples/plugins``
  to PyPI, then remove the in-tree copies.
* **SMLM.** 3D (astigmatic) fitting and throughput-oriented localization.
* **Setup validation write-up.** The per-rig validation is done; recording
  it (what was run, on which commit) in the documentation is not.

The design notes for work in progress are in ``docs/design/plans/`` in the
repository; they are working notes, not part of this documentation.

Rules for New Work
------------------

Before starting a task:

1. Check the red-zone rules in :ref:`red-zone-work`
2. Prefer no-hardware tests first
3. Keep changes narrowly scoped
4. Do not remove compatibility signals or public APIs without review
5. Document risks in PR descriptions for any hardware-adjacent change
