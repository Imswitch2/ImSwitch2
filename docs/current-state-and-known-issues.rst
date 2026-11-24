Current State and Known Issues
==============================

This page summarizes where ImSwitch2 stands as of 25 September 2026. It is a
quick orientation for maintainers, contributors and coding agents before
starting new work; ``ROADMAP.md`` in the repository has the milestones in
full.

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

The main open workstream is the scanning, galvo and DAQ modernization (see
*Scanning and galvo modernization* below).  Real-world setup validation —
the per-setup checklists that sign ImSwitch2 off on each lab's microscope —
has not started, and much of what is on ``main`` has only been tested
against mocks.  ``ROADMAP.md`` tracks the rest.

Known Issues and Limitations
----------------------------

Hardware validation
~~~~~~~~~~~~~~~~~~~

Features developed against the mock devices and simulated DAQ still need to
be confirmed on the rigs they are meant for, especially anything that times
lasers, scans, triggers or stage moves.  Treat a feature as unvalidated on
hardware unless its pull request says otherwise.

Scanning and galvo modernization
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This is red-zone work. Open issues include the detector-sync offset
(``phase_delay`` is applied on the detector side by the APD and PMT managers
but not to the ``line_clock`` the TimeTagger follows), the linestep
``line_clock`` count, and galvo trajectory magic numbers.  These must be
addressed with narrow no-hardware tests first and physical hardware
verification before enabling behavior changes.

Two related ``GalvoScanDesigner`` defects were measured on 2026-09-25, by
comparing the generated waveform at each pixel's read time with the nominal
pixel grid:

- **Pixel pitch.** The swept fast axis realizes a pitch about 0.6 % (40 µs
  dwell) to 1 % (20 µs) larger than the requested step. The line is centred
  correctly, so pixels drift from the nominal grid towards both ends. Images
  are therefore slightly larger in reality than their recorded OME pixel size
  says.
- **Read offset.** For a line length that is not a whole number of steps
  (10 µm at 0.3 µm), the first pixel is read about 0.7 pixel before the sweep
  reaches its start, while the mirror is still accelerating.

Both are pinned as strict expected failures in
``imswitch/imcontrol/_test/unit/test_scan_frame_geometry.py``. Fixing them
changes the waveforms of every galvo scan, so it is separate work.

**Loading a scan into the Advanced panel merges instead of replacing**
(found 2026-09-29). ``AdvancedScanParameterSerializer.apply`` sets line-step
enables and pulse windows only for the TTL devices a scan names, and leaves
every other device as it was. The Advanced panel's own saved files name only
the lasers the scan fires. So loading a scan that fires 405 into an Advanced
panel where 488 is ticked leaves 488 ticked, and the loaded scan fires 488
too. The SimplePointScan panel's scans name every TTL device (the ones they
do not fire, switched off), so they load exactly; Advanced's own files and
the serializer are unchanged, as fixing it changes how Advanced loads.

Communication backbone
~~~~~~~~~~~~~~~~~~~~~~

``CommunicationChannel`` still carries legacy compatibility signals
(``DEPRECATED_SIGNALS``) and broad cross-controller coordination. Deprecated
signals should not be removed until public API compatibility and
representative no-hardware startup/widget-set tests are in place.

Event-triggered workflows
~~~~~~~~~~~~~~~~~~~~~~~~~

EtSTED and EtMonalisa share the core event-triggered implementation. The
no-hardware contract is strong, but hardware-facing behavior still requires
physical review because these workflows involve lasers, scans, microscope stand
state, and DAQ-triggered acquisition.

Widget layout
~~~~~~~~~~~~~

Several widgets still contain legitimate but review-worthy fixed sizes, graph
heights, and dense grid layouts. Broader layout modernization should be done
widget-by-widget with screenshots or constrained-viewport smoke tests.  The
BeadRec widget still starts and stops with a *Run* checkbox rather than
Start/Stop buttons.

Rules for New Work
------------------

Before starting a task:

1. Check the red-zone rules in :ref:`red-zone-work`
2. Prefer no-hardware tests first
3. Keep changes narrowly scoped
4. Do not remove compatibility signals or public APIs without review
5. Document risks in PR descriptions for any hardware-adjacent change
