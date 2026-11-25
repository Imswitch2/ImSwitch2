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

The policy (``docs/design/plans/device-reconnect-2.0.md``, section 4.0):

* **Startup never crashes, and what did not start is reported.** A device that
  fails is listed in the log at startup, in a notice in the main window, and
  in Hardware status. A device whose manager cannot even be constructed (a
  vendor library that does not load, a setup error) is logged with its
  traceback, left out of its group, and reported as an error; the other
  devices still start.
* **Detectors fall back to a reported mock.** An acquisition needs a
  detector, so a camera that cannot be opened is replaced by a mock whose
  model name starts with ``MOCK (real camera not connected)``, whose
  recordings carry ``recording:detector_simulated`` and the reason, and which
  the camera's reconnect replaces by the real one. ``useMockOnFailure: false``
  opts out (the detector is left out and reported).
* **Everything else is absent, not mocked.** A laser, stage, rotator, flip
  mirror, pulse generator, SLM or stand that cannot be opened is not
  connected: its panel is greyed with the reason, commands are refused, and
  Hardware status reconnects it without a restart. A mock is used only where
  the setup configures one (a ``Mock*Manager``, a ``MOCK_`` serial,
  ``simulation: true``) or opts in with ``useMockOnFailure: true``. A device
  on a serial transport that failed to open is not usable either.

Every device kind above takes ``useMockOnFailure: true`` as the explicit
opt-in for a reported mock; a ``MOCK_`` serial or port and ``simulation: true``
are configured mocks and are not failures. A device that is not there never
becomes a mock on its own (lasers, stages, rotators, SLMs, stands, flip
mirrors); a reconnect from Hardware status brings it back OFF / unmoved.

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
     - A ``cameraListIndex`` starting with ``mock`` configures the mock (the
       camera the shipped no-hardware setups use). A real camera that fails
       to open falls back to the mock unless ``useMockOnFailure`` is
       ``false``; then startup reports a device-named error and the camera is
       left out.
     - No (free-runs).
   * - ``PiCamManager``
     - ``MockCameraTIS``
     - Same rule as ``AVManager``: a ``cameraHost`` starting with ``mock``, or
       a failed real camera with ``useMockOnFailure`` true (the default).
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
     - ``"cameraListIndex": "mock"`` configures it; a real camera that fails
       to open falls back to it unless ``useMockOnFailure`` is ``false`` (then
       a device-named startup error, camera left out).
     - Not in a simulated scan. The mock itself queues frames with
       ``mockTrigger(n)`` in either external trigger mode and free-runs on
       wall-clock at the exposure cadence with the internal trigger, but
       ``PhotometricsManager`` does not connect it to the simulated scan's
       frame triggers, so nothing calls ``mockTrigger``. Emits ``uint16``.
   * - ``ThorCamTSIManager``
     - ``MockThorTSICamera``
     - A ``cameraSerial`` starting with ``MOCK_`` configures it; a real
       camera that fails to open falls back to it unless ``useMockOnFailure``
       is ``false`` (then a device-named startup error, camera left out).
     - Partial — the repository note
       ``docs/advanced_scan_triggered_recording_audit.md`` (not part of the
       published documentation) audits this manager's mock/real
       triggered-recording paths.
   * - ``SwabianTimeTaggerManager``
     - ``MockTimeTaggerApi`` / ``MockTimeTagger``
       (``imswitch.imcontrol.model.interfaces.timetagger_mock``) — a stand-in
       for the vendor ``TimeTagger`` module and the card it opens, held by
       the shared ``TimeTaggerManager`` (the setup's ``timeTagger`` block).
     - ``"simulation": true`` in the ``timeTagger`` block; or
       ``"useMockOnFailure": true`` there when the library or card is
       missing, honoured only while ``nidaq.simulation`` is also true.
     - Yes. A ``SignalModel`` behind the card has two regimes: a *parked
       beam* (a photon rate with one lifetime, an IRF, dark counts and
       afterpulsing, the sync at its rep rate) for the device-level
       tutorials, and *scanning*, where the shared ``TimeTaggerManager``
       feeds the card the scan designer's own ``line_clock`` and
       ``frame_start_clock`` waveforms as edge timestamps at
       ``sigScanBuilt`` and ``Flim`` draws a Poisson TCSPC cube from the
       sample's rate and lifetime maps (``timeTagger.mockSample``: beads
       with two lifetimes, uniform, or a gradient). Trigger levels and
       polarity, dead time, the card's USB tag budget (overflows) and the
       conditional filter (which reverses the TCSPC direction) are
       modelled; ``timeTagger.mockFaults`` breaks the line clock, delays
       it (``line_delay_ps``: how late the clock reaches the card, negative
       = early), flips the sync polarity, kills the photons or changes the
       rep rate or the card model on purpose. While a scan runs the scan
       inputs count, period and skew from the loaded edges as the card
       would (``Counter``, ``TimeDifferences``, ``Histogram`` between two
       clocks, ``Scope``), timed from ``sigScanStarted`` so a measurement
       sees the edges that fall into its window; ``sample_truth`` and
       ``set_fault`` are the hooks tutorial 09 uses. Analytic or seeded: no thread,
       no wall clock. With an ``inputs`` block every detector input images a
       sample of its own (its ``mockSample``, else the block's: the first
       detector input the preset itself, each further one the preset's
       lifetimes plus 0.75 ns per input and a brightness pattern of its own),
       so images from two inputs differ and a cross-wired detector is
       caught; ``sample_truth(ny, nx, channel)`` gives each one's truth.
       ``CountBetweenMarkers`` counts an input between the pixel markers
       for counting detectors.
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
default range ``-5.0..5.0`` V). With a ``mockSample`` block both instead
image the virtual sample below; so does a mock ``HamamatsuManager`` camera,
for MoNaLISA. The mock camera's ``timing_readout_time`` is in seconds and
follows the ROI height (10 ms for the full 2048 rows).

The virtual sample
^^^^^^^^^^^^^^^^^^

A ``mockSample`` block in a detector's ``managerProperties`` (simulated
NI-DAQ only; ignored against real hardware) makes the detector image one
synthetic specimen, placed by the scan's own waveforms: the beam position of
every APD/PMT sample, the stage position of every camera frame. The pieces
are in ``imcontrol/model/managers/detectors/``: ``_mock_sample.py`` (the
specimen), ``_mock_optics.py`` (what the scan's signals do to it, and the
point-scan signal), ``_mock_lattice.py`` (the MoNaLISA camera).

**The specimen** is a map of fluorophores in µm (60 µm across, 25 nm texels),
drawn from a ``preset``:

* ``cells`` (default) -- cells with membrane, cytoplasm, nucleus and nuclear
  envelope, microtubules from a centrosome, stress fibres, vesicles below
  and above the resolution limit; 40, 100 and 200 nm beads between them.
* ``filaments`` -- microtubule asters, long stiff and short floppy
  filaments crossing at all angles.
* ``beads`` -- rows of 40 nm, 100 nm, 200 nm, 500 nm and 1 µm beads, pairs
  of 40 nm beads 600 nm down to 40 nm apart, and a field of 40 nm beads.
* ``resolution`` -- a 36-spoke Siemens star (6 µm radius) at the centre,
  line pairs and bead pairs 600 nm down to 40 nm apart, and gratings of
  500 nm down to 100 nm period.

``seed`` places the random structures. Each texel also records which
structure dominates it, so per-structure properties (a lifetime, a
spectrum) can be added later without redrawing the specimen; none are
modelled yet, so every laser excites every structure alike.

**The optics** are not baked into the specimen: each detector blurs it by its
own excitation PSF (cached, a few PSFs per specimen) and reads it with
bilinear interpolation. The excitation is peak-normalised: a point emitter
reads the same under any PSF, an extended structure collects more under a
wider one, and 1 is about the brightest structure under the default PSF
(235 nm FWHM), on an autofluorescence background of 0.03.

What each detector takes from the scan signals (``signalDict``), and what it
cannot:

* **Position** -- the x/y scanner waveforms (``axes``, µm per volt); an axis
  the scan does not drive reads 0 µm.
* **Defocus** -- with ``zAxis`` (``{"<z device>": <µm per V>}``) the z
  waveform, less ``focusUm``, widens the PSF over ``rayleighRangeUm``
  (default 0.4 µm) and lowers its peak; a point detector also loses the
  out-of-focus light at its pinhole. The live view of the camera keeps the
  z of the last scan: a z moved outside a scan is not in any scan signal.
* **Excitation** -- ``excitation`` names the lasers that light the sample,
  as a list or ``{"<laser>": <efficiency>}``. A laser lights it while its
  TTL in the scan (``TTLCycleSignalsDict``) is high or, without one (live
  view, a laser the scan does not switch), while it is switched on; at its
  power: the scan's analog power waveform where the scan carries one
  (Advanced line-step power, SimpleMoNaLISA, SimplePointScan channel
  power), else the power set in the Laser widget. The set state reaches the
  mock detectors through the shared attributes the laser controller
  already publishes for every laser (``("Laser", <name>, "Enabled")`` and
  ``"Value"``): the master controller hands each detector with a virtual
  sample a read-only view of them and of the lasers' configured ranges
  (``LaserStates`` in ``_mock_optics.py``, ``attachMockLaserStates``). It
  works for any laser manager, and no driver or hardware path is involved.
  A laser with no published state -- no Laser widget, or the power of a
  binary laser -- counts as on at full power. Without ``excitation`` the
  sample is always lit, as before.
* **Laser power** is a fraction of the laser's configured range
  (``valueRangeMin`` to ``valueRangeMax``) times its ``efficiency``; there
  is no power calibration yet (``Laser.intensity`` is the seam where one
  would turn a setting into physical power). ``fullScaleV`` in an
  ``excitation`` entry sets what voltage of a scan's power waveform is full
  power when the laser's range is not known. A laser with a calibration
  lookup table is read by its drive voltage against its range.
* **Photophysics** (with ``excitation``) -- fluorophores saturate: a
  fluorophore emits as I / (I + I_sat), scaled to the nominal brightness at
  intensity 1 (a laser of efficiency 1 at the top of its range);
  ``saturationPower`` is I_sat in the same units (default 0.2 for a point
  scan, 0.3 for the camera's readout, whose light is spread over its foci;
  ``null`` turns saturation off). A saturated focus is wider: the
  excitation PSF is replaced by the Gaussian of the same peak and volume as
  the saturated one, sigma times sqrt((1 + x) ln(1 + x) / x), x = I / I_sat.
  The background -- autofluorescence (0.03) plus ``scatterBackground``
  (default 0.12) at intensity 1 -- grows in proportion to the light and does
  not saturate. With Poisson noise on all of it, dark counts, camera offset
  and read noise, image quality has an optimum: too little light and shot
  noise dominates; too much and the signal stops growing while the focus
  widens and the background keeps growing. ``saturationPower: null,
  scatterBackground: 0`` gives the plain linear gating of earlier versions.
* **Photobleaching** -- ``bleachHalfLifeS``: seconds of emission at the
  nominal brightness that halve the fluorophores -- so more power bleaches
  faster, as much faster as the fluorophores emit more, up to their
  saturation -- accumulated on a 100 nm grid in the order the
  scan exposes the sample (per line read for a point detector, per frame
  for the camera), for the life of the detector. The live view shows the
  bleached sample but does not bleach it. Off by default.

Point detectors (APD, PMT) add ``psfFwhmUm`` (default 0.235 µm). The APD's
mean count per sample is ``mockPhotonCountMean`` times the brightness, plus
``darkCountRate`` (counts per second, default 0) that no light changes. The
PMT adds ``photonsPerSample`` Poisson photons at brightness 1 (default 20),
each worth ``signalVolts / photonsPerSample`` volts (default 1 V in all),
to its voltage noise.

**The MoNaLISA camera** shows a square lattice of foci fixed on the camera
(``latticePeriodUm``, ``latticeAngleDeg``), the sample moved under it by the
stage position at each camera TTL edge. Each focus excites the sample
through a Gaussian excitation PSF (``excitationFwhmUm``, default 0.2 µm);
its spot on the camera is the light of the emitters it excites, imaged by
the detection PSF and centred on them rather than on the focus -- the spots
move with the specimen, as image-scanning microscopy expects.
``spotSigmaUm`` (default 0.12 µm) keeps its meaning: the spot of the
confocal readout in focus, as the frames show it; the detection PSF is what
it leaves after the excitation PSF.
``switching`` makes the sample reversibly switchable (RESOLFT):
``{"on": [<lasers>], "off": [<lasers>]}``, with rates ``onPerMs`` (5),
``offPerMs`` (20) and ``readoutPerMs`` (0.3) per ms of light. From the TTLs
between two frames the camera reads how long the ON and OFF pulses were
and from the exposure how long the readout (``excitation``) was lit; the
OFF pattern has its zeros at the foci, so the effective PSF narrows by
``sqrt(1 + zeta pi^2 sigma^2 / period^2)``, ``zeta`` the OFF dose. With the
shipped profile (ON 1 ms, OFF 2 ms) that is about 80 nm FWHM; the confocal
mode (no OFF step) stays at 200 nm. A cycle with no ON pulse reads what the
earlier readouts left on. Each laser's power acts on its own step: the ON
dose (``onPerMs`` x power x duration) sets how many emitters are on; the OFF
dose sets the resolution, so too little OFF power reads confocal-like; the
readout power sets the signal, which saturates with the emitters while the
readout switches them off as fast as they emit and the background keeps
growing, so its signal-to-noise ratio peaks in between (about 30 % of
range by default). The camera counts Poisson photons
(``peakCounts`` at brightness 1 for the confocal readout in focus), adds
``offsetCounts`` (100) and Gaussian ``readNoiseCounts`` (1.5), and seeds
its noise with the manager's ``mockRandomSeed``.

What this leaves out, to stay at tens of milliseconds per 512 x 512 frame:
each spot is one Gaussian placed and sized from the first moments of the
excited emitters, not their full image; a focus sees only its own zero of
the OFF pattern; the switching state is carried from frame to frame as one
number at the focus centre, not per emitter; switching fatigue and
OFF-light bleaching are not modelled, so more OFF power only ever sharpens;
saturation is per laser sum, not per wavelength or per structure; and the
camera's live view (no scan) shows the confocal readout of the readout
lasers as set, ignoring the ON and OFF lasers.

What a user sees out of the box: lasers start off and at their lowest
power, as ImControl sets them on startup. The SimpleMoNaLISA acquisition
fires its lasers at full power through its own power waveform, so a mock
recording shows the sample at once; its live view, and a SimplePointScan
overview without channel power, stay dark until a laser is raised in the
Laser widget -- the mock logs which lasers to raise. ``test_mock_monalisa_reconstruction.py`` runs
mock scans of both modes through the real ``reconstruct_scan`` and checks
each recovers the specimen under its own effective PSF.

Positioners / stages
~~~~~~~~~~~~~~~~~~~~~

A stage that is not there is reported not connected and refuses moves with its
name; ``useMockOnFailure: true`` is the opt-in for a reported mock. The mocks
that exist:

* ``MockPositionerManager`` — a standalone one-axis manager (no serial/DAQ
  backing at all) used directly in setup JSON for repeat/timelapse-style
  mock stages. Correctly tracks position in ``self._position``.
* ``NidaqPositionerManager`` — runs against a simulated NI-DAQ
  (``nidaq.simulation: true``); no physical AO/DO channels are opened.
* ``MHXYStageManager`` (Marzhauser XY), ``PiezoconceptZManager`` /
  ``PiezoconceptZManager2`` and ``SQUIDStageManager``: a ``rs232device`` that
  cannot be looked up is an error naming it; with ``useMockOnFailure`` a
  ``MockRS232Driver`` stands in (for SQUID it carries a small mock board that
  accepts and logs moves). A serial port that fails to open is the transport's
  failure: the stage reports itself not connected (the Marzhauser stage by
  refusing to sync a position from a placeholder port) and reconnects with
  the port.
* ``KinesisStageManager``, ``PIStageManager`` and ``KDC101PositionerManager``:
  a ``MOCK_`` serial / port is a configured mock; a stage that fails to open
  is not connected (PI keeps its last known position and refuses moves), and a
  reconnect re-reads the position and moves nothing.
* ``GRBLStageManager``: no mock. After a reconnect the board's position is
  unknown: moves are refused until the user references the stage (the
  Positioner panel's Reference action homes the board).
* ``SmarACTPositionerManager`` — no mock. A missing MCSControl library makes
  the module unavailable, and a stage that is not found fails the manager's
  construction; either way the stage is left out and reported as an error.
* Rotators: ``KinesisRotatorManager``, ``StandaRotatorManager`` and
  ``ElliptecRotatorManager`` use a mock only for a ``MOCK_`` serial / port, or
  with ``useMockOnFailure: true``; otherwise they are not connected, refuse
  moves, and reconnect from Hardware status (the Elliptec bus re-reads every
  address once and never homes or moves).

Lasers
~~~~~~

There is no dedicated mock laser manager and no ``laser.simulation`` flag
analogous to ``nidaq.simulation`` (the Cobolt managers have their own
``simulation``). A laser that is not there is never a mock unless the setup
says so: it is reported not connected, its commands are refused with its name,
and a reconnect brings it back OFF (its cached setpoint is kept but not
applied, so a later power change cannot light it).

* ``Cobolt0601LaserManager`` (and its ``LantzLaserManager`` base),
  ``Cobolt0601NewLaserManager``, ``CoolLEDLaserManager``,
  ``ESP32LEDLaserManager`` and ``PyMicroscopeLaserManager``: absent by
  default; ``useMockOnFailure: true`` runs a reported mock. ``simulation:
  true`` (Cobolt) is a configured mock.
* ``PulseStreamerLaserManager``: ImSwitch2 never builds a
  ``pulseStreamerManager``, so this laser is reported as not available (use
  ``PulseGeneratorLaserManager``); a mock only with the opt-in.
* Lasers driven over RS232 depend on the RS232 manager they are bound to,
  which leaves a placeholder ``MockRS232Driver`` port if it cannot be opened
  so that they can still be built; a device on a failed transport is not
  usable (its refusal names the transport) and recovers when the transport
  reconnects.
* ``GRBLLaserManager`` is bound to a ``GRBLManager`` board, which has no mock:
  a port that cannot be opened leaves the board not connected and the laser
  refusing commands until the board is reconnected (then OFF).
* ``PulseGeneratorLaserManager`` (and any manager built against
  ``lowLevelManagers['pulseGeneratorManager']``) degrades to a no-op mock
  only when the setup has no pulse generator at all. When a Teensy is
  configured but its port cannot be opened (and ``useMockOnFailure`` is
  false) the Teensy is not connected and reported, the laser is still built,
  and its commands raise ``DeviceNotConnectedError`` until the Teensy is
  reconnected. A Teensy with no port, or with ``useMockOnFailure`` true, runs
  on the mock driver.

SLMs
~~~~

``HamamatsuSLMdviManager`` / ``HamamatsuSLMusbManager`` expose an explicit
``mockermode`` manager property. ``HamamatsuSLMusbManager`` whose DLL cannot
be loaded (or lacks the expected functions) is reported not connected:
``upload_pattern`` refuses with the SLM's name and the SLM panel's Connect
retries the DLL and the USB device (it never uploads by itself). With
``useMockOnFailure: true`` it switches to a reported mock instead. The DVI
manager mocks only when ``mockermode`` is set.

Flip mirrors / stands / serial
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* ``ThorlabsMFFMockManager`` (→ ``MockThorlabsMFFManager``) is a dedicated
  mock flip-mirror manager, selected explicitly via ``managerName`` rather
  than a hardware-failure fallback.
* ``LeicaDMIStandMockManager`` (→ ``MockLeicaDMIStandManager``) is the
  configured mock stand. The real ``LeicaDMIStandManager`` and
  ``LeicaDMIZPositionerManager`` have no mock: a stand whose serial port did
  not open is reported not connected and refuses commands by name.
* A stand manager class that cannot be loaded is reported as failed (a stand
  that does nothing stands in so stand controllers keep working); its
  ``<managerName>_mock`` twin is loaded only with ``useMockOnFailure: true``.
* ``MockRS232Driver`` is the placeholder serial driver ``RS232Manager``
  leaves in place when a port cannot be opened (so that devices on it can be
  built) and the opt-in driver of the stages above. It logs writes/queries and
  always returns ``None`` from ``read()`` (matching a real timeout), rather
  than modeling any device protocol; devices on it do not look connected.

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

Missing NI-DAQmx is not simulation
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

``nidaq.simulation: false`` means that the setup requests real NI-DAQ
operation. If the ``nidaqmx`` Python backend cannot be imported, ImControl no
longer aborts startup: the configured NI-DAQ infrastructure is reported as
unavailable and the rest of Hardware Control can continue to load. This is a
degraded real-hardware state, **not** an automatic mock fallback.

In that state, one-shot AO/DO writes report failure and an NI-DAQ scan is
refused before it arms. The scan simulator, virtual camera triggers and
synthetic APD/PMT scan data are enabled only by the explicit
``nidaq.simulation: true`` setting. This avoids producing plausible simulated
data when a real microscope merely lost its NI-DAQ dependency.

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

* ``simple_mock_setup.json`` — mock camera (``AVManager``) for live widefield and one simulated
  APD imaging the synthetic sample (``mockSample``) through the
  SimplePointScan panel; galvos reaching 100 µm, three gated lasers,
  ``nidaq.simulation: true``.
* ``galvo_apd_simple_mock_scan_setup.json`` — the same panel with two APDs
  and no camera, for the panel's reference-detector choice and its tests.
* ``simple_monalisa_mock_scan_setup.json`` — the SimpleMoNaLISA panel on a
  mock Hamamatsu camera (Live switches it to the internal trigger, an
  acquisition to the external frame trigger) whose frames show the virtual
  sample under a 0.72 µm foci lattice (``mockSample``), so a recording
  reconstructs to cells and beads; the sample switches with the profile's
  ON/OFF/readout TTLs, so the RESOLFT mode reconstructs sharper than the
  confocal one. Simulated NI-DAQ axes and lasers.
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
* ``galvo_flim_mock_scan_setup.json`` — the same, plus a simulated Swabian
  Time Tagger (``timeTagger.simulation``) and a ``SwabianTimeTaggerManager``
  FLIM detector on it: a lifetime scan runs, fits and records end to end
  with no hardware, imaging two bead populations of 1.5 ns and 4.0 ns.
* ``galvo_two_apd_counting_mock_setup.json`` — two single-photon detectors on
  one simulated Time Tagger, each counting photons per pixel (no laser sync,
  no FLIM): one scan, two images, one card.
* ``galvo_two_colour_flim_mock_setup.json`` — two FLIM detectors on one
  simulated Time Tagger against one laser sync.

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
* **Only some mock detectors image a virtual sample.** A simulated APD or
  PMT and a mock Hamamatsu camera with a ``mockSample`` block derive their
  data from the virtual sample at the positions the scan waveforms give (see
  *The virtual sample* above); a mock MoNaLISA scan reconstructs to that
  sample through the real ``reconstruct_scan``
  (``test_mock_monalisa_reconstruction.py``). Every other mock detector --
  the AV/TIS/other mock cameras, and an APD, PMT or Hamamatsu without
  ``mockSample`` -- still returns bounded noise tuned by mean/max/seed
  knobs: enough to validate frame counts, dtypes and timing contracts, but
  not to exercise reconstruction or segmentation logic (e.g. BeadRec)
  beyond "did a frame arrive."
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
