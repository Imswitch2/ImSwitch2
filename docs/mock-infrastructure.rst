Mock Infrastructure
====================

ImSwitch2 can run most of its device managers against **mock/simulated
implementations** instead of physical hardware. This page is an inventory of
what exists today, how each mock is enabled, and what it deliberately does
not simulate. It complements :doc:`no-hardware-validation`, which documents
the *test policy* around hardware-free runs; this page documents the
*mechanisms* that policy relies on.

.. important::

   Mocking exists for development, CI, and no-hardware validation. It is not
   a hardware safety layer and it is not a physics simulation of a real
   microscope. Synthetic data is plausible, not accurate.

Why mocks exist
----------------

* Let contributors run and develop ImSwitch2 without owning the referenced
  hardware.
* Let CI validate configuration parsing, manager construction, controller
  logic, and workflow orchestration (see :doc:`no-hardware-validation`).
* Give the live-recording / live-reconstruction work a repeatable,
  hardware-free way to exercise the record → watch-folder → reconstruction
  loop.
* Let scan-triggered recording (ScanOnce/ScanLapse) be debugged without a
  connected NI-DAQ or camera.

There is no single ``MockDeviceProtocol`` that every family implements.
Mocking grew organically per device family, so the enablement convention and
the fidelity of each mock differ. That inconsistency is itself tracked below
under Limitations.

Mock inventory by device family
--------------------------------

Detectors
~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 20 25 40 15

   * - Manager
     - Mock class
     - How it's enabled
     - Trigger-aware?
   * - ``AVManager``
     - ``MockCameraTIS`` (``imswitch.imcontrol.model.interfaces.tiscamera_mock``)
     - Always: no video driver is bundled, so every ``cameraListIndex``
       loads the mock. This is the camera the shipped no-hardware setups use.
     - No (free-runs).
   * - ``TISManager``
     - ``MockCameraTIS``
     - Automatic fallback: loaded whenever the real camera fails to
       initialize, for example with ``"cameraListIndex": "mock"``.
     - No (free-runs).
   * - ``HamamatsuManager``
     - ``MockHamamatsu`` (``imswitch.imcontrol.model.interfaces.hamamatsu_mock``)
     - Same fallback pattern; ``"cameraListIndex": "mock"``.
     - Yes. ``mockTrigger(n)`` queues externally-triggered frames;
       ``getFrames()`` drains the queue in external-trigger mode and only
       free-runs on wall-clock in internal-trigger mode. Emits ``uint16``.
   * - ``PhotometricsManager``
     - ``MockPhotometrics`` (``imswitch.imcontrol.model.interfaces.photometrics_mock``)
     - Same fallback pattern; ``"cameraListIndex": "mock"``.
     - Not in a simulated scan. The mock itself queues frames with
       ``mockTrigger(n)`` in either external trigger mode and free-runs on
       wall-clock at the exposure cadence with the internal trigger, but
       ``PhotometricsManager`` does not connect it to the simulated scan's
       frame triggers, so nothing calls ``mockTrigger``. Emits ``uint16``.
   * - ``ThorCamTSIManager``
     - ``MockThorTSICamera``
     - Automatic when ``cameraSerial`` starts with ``MOCK_``, or when the
       real SDK/serial fails to open.
     - Partial — the repository note
       ``docs/advanced_scan_triggered_recording_audit.md`` (not part of the
       published documentation) audits this manager's mock/real
       triggered-recording paths.
   * - ``APDManager`` / ``PMTManager``
     - No separate mock *class* — the real manager generates synthetic
       scan-shaped samples in place.
     - Automatic whenever ``nidaqManager.isSimulated`` is true (i.e.
       ``nidaq.simulation: true``), or forced via
       ``"simulation_mode": true`` in ``managerProperties`` against real
       hardware.
     - Yes: in simulation each starts its own synthetic scan when the scan
       is built (see *NI-DAQ / scan simulation* below).

APD synthetic data is modeled as a monotonic cumulative counter that
reconstructs to bounded non-negative integer photon counts
(``mockPhotonCountMean``, ``mockPhotonCountMax``, ``mockRandomSeed``). PMT
synthetic data is bounded ``float32`` voltage samples averaged per pixel
(``mockVoltageMin``/``mockVoltageMax``/``mockVoltageMean``/``mockVoltageNoiseStd``,
default range ``-5.0..5.0`` V).

Positioners / stages
~~~~~~~~~~~~~~~~~~~~~

* ``MockPositionerManager`` — a standalone one-axis manager (no serial/DAQ
  backing at all) used directly in setup JSON for repeat/timelapse-style
  mock stages. Correctly tracks position in ``self._position``.
* ``NidaqPositionerManager`` — runs against a simulated NI-DAQ
  (``nidaq.simulation: true``); no physical AO/DO channels are opened.
* ``MHXYStageManager`` (Marzhauser XY over RS232) — falls back to
  ``MockRS232Driver`` when the configured ``rs232device`` fails to
  initialize. Position bookkeeping happens in the manager's own
  ``self._position``, independent of the driver, so this path tracks
  position correctly even though the RS232 calls are no-ops.
* ``PiezoconceptZManager`` / ``PiezoconceptZManager2`` (Z-piezo over RS232)
  — same ``MockRS232Driver`` fallback as ``MHXYStageManager`` when the
  configured ``rs232device`` is missing or fails to initialize.
* Rotators (``StandaRotatorManager``, ``ElliptecRotatorManager``,
  ``KinesisRotatorManager``) each implement their own real-then-mock
  fallback (``MockElliptecBus``/``MockElliptecMotor``, mock Kinesis motor,
  Standa mocker) triggered when the real driver import/connect fails.

Lasers
~~~~~~

There is no dedicated mock laser manager and no ``laser.simulation`` flag
analogous to ``nidaq.simulation``. Headless-safe laser operation happens
incidentally through two other fallbacks:

* Lasers driven over RS232 fall back to ``MockRS232Driver`` the same way
  ``MHXYStageManager`` does, if their serial device is missing.
* ``PulseGeneratorLaserManager`` (and any manager built against
  ``lowLevelManagers['pulseGeneratorManager']``) follows the mock-mode
  fallback pattern documented in :doc:`adding-device-support`: when the
  pulse generator low-level manager is unavailable, ``self._isMock =
  self._pulseGen is None`` and the manager degrades to a no-op.

SLMs
~~~~

``HamamatsuSLMdviManager`` / ``HamamatsuSLMusbManager`` expose an explicit
``mockermode`` manager property (also auto-enabled if the real device fails
to open), independent from the ``cameraListIndex: "mock"`` and RS232
fallback conventions used elsewhere.

Flip mirrors / stands / serial
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* ``ThorlabsMFFMockManager`` (→ ``MockThorlabsMFFManager``) is a dedicated
  mock flip-mirror manager, selected explicitly via ``managerName`` rather
  than a hardware-failure fallback.
* ``LeicaDMIStandMockManager`` (→ ``MockLeicaDMIStandManager``) falls back
  to ``MockRS232Driver`` when its configured RS232 device can't be reached.
* ``MockRS232Driver`` is the shared fallback serial driver used by several
  of the families above. It logs writes/queries and always returns ``None``
  from ``read()`` (matching a real timeout), rather than modeling any device
  protocol.

NI-DAQ / scan simulation
~~~~~~~~~~~~~~~~~~~~~~~~~

``nidaq.simulation: true`` is the setup-level switch that puts scanning
itself into software-only mode
(``imswitch/imcontrol/model/managers/mockscan/``):

.. code-block:: python

   class SimulatedScanPlan:
       """Per-scan plan for software-only NI-DAQ simulation."""
       # nominal duration, scan position count, per-detector frame counts,
       # derived from setupInfo + the scan's signal/TTL dictionaries.

   class ScanSimulationCoordinator:
       """Owns software-only scan timing and virtual frame-trigger dispatch."""

``NidaqManager`` delegates to the coordinator instead of building real
AO/DO tasks. For each scan the coordinator builds a ``SimulatedScanPlan``
and runs it on a worker thread, which spreads every detector's planned frame
count over a wall-clock duration (the plan's duration, clamped to
0.2–5 s):

* The coordinator emits ``sigFrameTrigger(detectorName, n)`` as frames fall
  due; ``NidaqManager`` re-emits it as ``sigSimScanFrameTrigger``.
  ``HamamatsuManager`` connects to that signal when its camera is the mock
  and passes ``n`` on to the camera's ``mockTrigger(n)``, which queues
  exactly ``n`` frames. No other detector manager listens to it.
* ``APDManager`` and ``PMTManager`` do not use the frame triggers. In
  simulation mode they call their own ``mockStartScan()`` when the scan is
  built, which runs their scan worker on synthetic samples.
* When the plan has run, the worker emits ``sigDone``, which is connected to
  ``NidaqManager.scanDone()``. That completes the scan exactly once.

The managers' ``mockStopScan()`` and ``mockScanDone()`` methods are used
only by tests. This design replaced an earlier one where
``registerExternalScanDriver()`` globally suppressed camera triggering
whenever an APD/PMT was present, which broke mixed detector setups;
``registerExternalScanDriver()`` is now a no-op kept for compatibility.

Workflow facade
~~~~~~~~~~~~~~~

``imswitch/imcontrol/model/workflows/mock_facade.py`` provides
``build_mock_facade()``: a ``MicroscopeFacade`` stand-in that records every
call (name, args, kwargs) and returns canned camera data, used by headless
workflow unit tests. This is a *test utility*, not a runtime setup-config
mock — it's instantiated directly in test code, not selected through a
setup JSON.

Plugin-repo device mocks
~~~~~~~~~~~~~~~~~~~~~~~~~

Out-of-tree device plugins invented their own conventions, neither of which
matches core:

* ``imswitch-device-thorlabs`` (ThorCam TSI): mock is selected by
  ``cameraSerial`` starting with ``MOCK_``.
* ``imswitch-zhinst-devices`` (lock-in demodulator): mock is selected by an
  explicit ``useMock`` / ``useMockOnFailure`` manager property, with
  ``mockAmplitude``/``mockFrequencyHz``/``mockNoiseStd``/``mockSeed`` tuning
  knobs.

See :doc:`devices/plugins` for the plugin authoring contract in general; it
does not yet prescribe a mock convention.

Setup templates
----------------

Ready-to-use hardware-free setup files, all under
``imswitch/_data/user_defaults/imcontrol_setups/``:

* ``example_mock.json`` — minimal mock camera (``AVManager``) + mock XY
  stage, general-purpose GUI smoke setup.
* ``example_no_hardware.json`` — the baseline profile for
  :doc:`no-hardware-validation`; no lasers, simulated NI-DAQ, all physical
  I/O channels ``null``.
* ``mock_scan_setup.json`` — pure software MoNaLISA/live-reconstruction
  setup: mock camera, mock positioners, no physical AO/DO channels,
  ``nidaq.simulation: true``.
* ``hamamatsu_mock_scan_setup.json`` — scan setup with a trigger-aware
  ``HamamatsuManager`` mock camera in external frame-trigger mode; the
  simulation coordinator drives its virtual camera triggers.
* ``mixed_hamamatsu_apd_mock_scan_setup.json`` — trigger-gated mock
  Hamamatsu camera *and* synthetic APD scan data in the same setup, the
  regression case for the mixed-detector race fixed in the scan simulation
  coordinator work.
* ``galvo_apd_mock_scan_setup.json`` — an APD with ``NidaqPositionerManager``
  galvo/piezo axes and ``NidaqLaserManager`` lasers on a simulated NI-DAQ,
  for designing, running and recording Advanced (galvo-designer) scans,
  including single-axis scans.

Treat "null AO/DO channels" (no physical scan output built) and "fake
``Dev1/...`` detector input names" (manager configuration placeholders only,
required by ``APDManager``/etc. field validation) as two distinct kinds of
"fake". The ``__comment__`` entry at the top of each scan setup file
describes what that file fakes.

Limitations
------------

These are current, real gaps — not aspirational — as of 2026-09-25:

* **No unified mock contract.** Enablement conventions differ per family:
  ``cameraListIndex: "mock"`` (cameras), automatic try/except fallback
  (rotators, stands, some lasers/stages), an explicit ``mockermode``
  property (SLM), a ``MOCK_``-prefixed serial string (ThorCam TSI plugin),
  and a ``useMock`` property (zhinst plugin). Nothing declares "is this
  manager mockable and how" in one discoverable place.
* **ScanLapse (timelapse) has no stable in-suite regression through the real
  ``RecordingManager``/``WriterThread``.** A standalone two-cycle Hamamatsu
  ScanLapse reproduction completes cleanly, but the pytest version reliably
  aborts during teardown (writer thread blocked on its queue while the main
  thread waits in ``RecordingManager.endRecording()``) — a Qt/pytest
  lifecycle issue, not a trigger-delivery or cycling-logic bug. Every such
  test was written, hit this, and was removed rather than left flaky; the
  root cause (real ``threading.Thread`` writer teardown vs. pytest's Qt event
  loop) is still undiagnosed. What *is* now covered:
  ``test_scanlapse_two_cycle_drain_and_progression`` drives the controller's
  two-cycle lapse state machine (``nextLapse``/``scanDone``/
  ``recordingCycleEnded``, drain-retry, per-cycle savename/``recLapseIndex``,
  final-cycle teardown) against a fake ``RecordingManager`` with no real
  ``QThread``/``WriterThread`` involved, so it can't hit the teardown abort.
  It locks down the cycling contract; it does not exercise real HDF5/writer
  finalization across cycles.
* **Aborting does not stop a running scan, real or mock.** The NI-DAQ scan
  controllers share ``abortScan()`` from ``SuperScanController``; it
  cancels pending repeat, sequence and follow-up runs, but a scan already
  running continues to its full programmed length and the run ends on the
  NI-DAQ's own ``sigScanDone``. This isn't mock-specific: real NI-DAQ AO/DO
  waveform tasks aren't interrupted early either, by the same design. The
  mock side is already ready for this if it's ever added:
  ``ScanSimulationCoordinator.stop()`` → ``SimulatedScanWorker.stop()`` sets
  a flag checked once per ~30 ms tick, so an external call aborts within one
  tick (measured ~17 ms), not the 5 s duration cap, and correctly suppresses
  ``sigDone`` when aborted mid-run. (An earlier version of this page claimed
  the opposite — that abort only unblocks after the capped duration timer —
  based on a stale note written before this coordinator existed; verified
  against the current code and corrected here.)
* **Synthetic detector content isn't a "fake specimen."** APD/PMT and camera
  mock data is bounded noise tuned by mean/max/seed knobs, not derived from
  a simulated sample. It's enough to validate frame counts, dtypes, and
  timing contracts, but it won't exercise reconstruction/segmentation logic
  meaningfully (e.g. BeadRec, live-reconstruction) beyond "did a frame
  arrive."
* **No SLM/rotator simulation of optical effect.** ``mockermode`` and the
  rotator mocks accept commands and hold state, but there's no simulated
  beam/polarization/phase effect behind them.
* **Plugin mock conventions aren't documented as a contract**, so each new
  out-of-tree device plugin is likely to invent a third convention.

Future work
------------

Roughly in priority order:

1. **Diagnose the real-writer-thread ScanLapse pytest teardown abort.** The
   controller-level cycling contract now has coverage (see Limitations); what
   remains is the actual ``WriterThread``/``QThread`` teardown across two
   real recording cycles under pytest. Candidate next step: run the
   writer-thread-wait outside the Qt event loop under test, or add an
   explicit test-only drain/timeout, rather than another retry of the same
   full end-to-end test shape that has aborted every previous time.
2. **Make aborting stop a running scan** (real and mock). This is a
   general scan-lifecycle feature, not mock-only work — see Limitations.
   When it lands, wire it to
   ``NidaqManager``/``ScanSimulationCoordinator.stop()`` for the simulated
   path; no coordinator-side change is needed first, it already aborts
   promptly.
3. **Define one mock-manager contract/property** (e.g. a documented
   ``managerProperties["mock"]`` convention plus a small
   ``MockCapableManager`` mixin/protocol) that every device family — cameras,
   positioners, lasers, rotators, SLMs, stands — is expected to follow, and
   retrofit the existing ad hoc conventions onto it over time. Document it
   in :doc:`adding-device-support` and :doc:`devices/plugins` so new
   in-tree and plugin managers pick a single pattern instead of inventing
   another one.
4. *(dropped by decision, 2026-07-02)* ~~Give the live-reconstruction debug
   loop a synthetic sample.~~ Judged overkill: replaying *real* legacy
   recordings through the real storers/live sources (a local harness, not
   in-repo) validates the streaming
   pipeline end-to-end with data whose reconstruction can actually be
   judged, which covers the need better than a simulated scene would.
5. **Add SLM/rotator optical-effect stubs** where cheap (e.g. rotator mock
   angle affecting a reported polarization value) so downstream logic that
   reacts to those readings has something non-trivial to see in no-hardware
   tests.
6. **Document the plugin mock convention** once (3) exists, and align the
   ThorCam-TSI (``MOCK_`` prefix) and zhinst (``useMock``) plugin examples
   with it as the reference implementation for third-party device authors.

Related documentation
-----------------------

* :doc:`no-hardware-validation` — the test policy and CI command that this
  infrastructure supports.
* :doc:`adding-device-support` — manager authoring guide, including the
  pulse-generator mock-mode fallback pattern.
* :doc:`devices/plugins` — plugin device contract.
* :doc:`current-state-and-known-issues` — broader project status.
