*******************************
The Swabian Time Tagger
*******************************

One Swabian Time Tagger card serves everything on it in a setup: one or
several detectors -- FLIM, or photon counting per pixel
(:ref:`SwabianTimeTaggerManager <swabian-detector>` in
:doc:`../devices/detectors`) -- the calibration and debugging tutorials, and
the scripting facade. FLIM and STED are use cases of the card, not what the
card is: a two-APD confocal that counts photons on one card, with no laser
sync, is as much a Time Tagger setup as a gated-STED one. This chapter is
the card's own page: how it is described in the setup file, what the card
can do, what ImSwitch2 does with it, and how a script reaches it.
:doc:`debugging` maps symptoms to the tutorial that finds the cause.

The ``timeTagger`` block
========================

The card is described once, in a top-level ``timeTagger`` block of the setup
JSON (reference: :doc:`../setupinfo-reference`), in one of two forms. The
``inputs`` map declares any number of named inputs, each with its kind
(:ref:`below <timetagger-inputs>`); the older form sets one field group per
*role*. Every consumer -- a detector's ``photon_input`` / ``start_role`` /
``line_role``, the tutorials, the facade -- refers to an input by its name
or by the role it plays, so re-cabling the card is one edit here. The roles
and their fields in the older form:

.. list-table::
   :header-rows: 1
   :widths: 16 30 54

   * - Role
     - Block fields
     - Carries
   * - ``photons``
     - ``photonsChannel``, ``photonsTriggerV``, ``photonsDeadtimePs``
     - the single-photon detector's pulses (a SPAD's NIM pulse is negative:
       channel ``-1``, trigger about ``-0.25`` V)
   * - ``laser_sync``
     - ``laserSyncChannel``, ``laserSyncTriggerV``
     - the laser's sync output, the TCSPC reference
   * - ``line_clock``
     - ``lineClockChannel``, ``lineClockTriggerV``, ``lineClockDelayPs``,
       ``lineClockDeadtimePs``, ``pixelPatternOffsetPs``
     - the scan's ``scan.lineClockLine``; the pixel markers are generated
       from its edges, starting ``pixelPatternOffsetPs`` (default 10 ns)
       after each so the frame edge leads pixel 0. The NI-DAQ plays the
       clocks the scan's phase delay (D2) later, following the beam;
       ``lineClockDelayPs`` moves the markers by what remains: positive
       when the clock still fires *early* (the detector delays its
       pattern), negative when it arrives *late* (a long cable; the card
       moves the timestamps earlier). Tutorial 09 measures it.
       ``lineClockDeadtimePs`` (the frame clock takes it too) swallows the
       bounces of the pulse's falling edge, each of which would count as
       another line start; tutorial 07 shows them and suggests the value.
   * - ``frame_clock``
     - ``frameClockChannel``, ``frameClockTriggerV`` (optional)
     - the scan's ``scan.frameStartClockLine``, if cabled
   * - ``sted_pulse``
     - ``stedPulseChannel``, ``stedPulseTriggerV`` (optional)
     - a photodiode on the STED beam, for the STED-to-excitation delay

A negative channel number selects the falling edge of that input. All
inputs are 50 Ω with a trigger range of ±2.5 V; an NI DAQ line drives about
1--1.5 V into 50 Ω, so clock thresholds stay around 0.5 V. ``serial``
picks a card; ``simulation`` uses the in-process mock (:doc:`../mock-infrastructure`);
``useMockOnFailure`` falls back to it only while the NI-DAQ is simulated
too, so a rig never images a missing card as zeros. ``filterSyncByPhotons``
enables the conditional filter (below). ``mockSample`` and ``mockFaults``
drive the mock.

.. _timetagger-inputs:

Inputs by name, and several detectors on one card
-------------------------------------------------

The ``inputs`` map names every input the card has cabled. Each entry has a
``channel`` (signed: negative is the falling edge), a ``kind`` --
``detector`` (photon pulses; the default), ``laser_sync``, ``line_clock``,
``frame_clock`` or ``marker`` (any other pulse source: a STED-beam
photodiode, a trigger out) -- and its ``triggerV``, ``deadtimePs`` and
``delayPs``. Two detectors counting on one card, with no sync (shipped as
``galvo_two_apd_counting_mock_setup.json``):

.. code-block:: json

   "timeTagger": {
       "inputs": {
           "apd_green":   {"channel": -1, "triggerV": -0.25, "deadtimePs": 50000},
           "apd_red":     {"channel": -2, "triggerV": -0.25, "deadtimePs": 50000},
           "line_clock":  {"channel": 3, "kind": "line_clock", "triggerV": 0.5},
           "frame_clock": {"channel": 4, "kind": "frame_clock", "triggerV": 0.5}
       }
   },
   "detectors": {
       "TT_green": {"managerName": "SwabianTimeTaggerManager",
                    "managerProperties": {"photon_input": "apd_green", "measurement": "counting"}},
       "TT_red":   {"managerName": "SwabianTimeTaggerManager",
                    "managerProperties": {"photon_input": "apd_red", "measurement": "counting"}}
   }

Each detector is its own image channel in a scan, in the live view and in a
recording. Add a ``laser_sync`` input and set ``measurement`` to ``flim``
and each detector builds its own histogram, cube and fit against the shared
sync (shipped as ``galvo_two_colour_flim_mock_setup.json``); a FLIM and a
counting detector can share a scan too.

**Roles.** A role resolves to the input named like it (an input named
``line_clock`` must be a ``line_clock`` input), else to the only input of
its kind -- ``photons`` to the only ``detector`` input; with two detector
inputs a detector must name its ``photon_input``, and the error says so.
``sted_pulse`` resolves by name only (markers are many things).

**The older form keeps working, unchanged.** A block without ``inputs``
means exactly what it always did: one input per configured role, named by
its role, with the same conditioning; the shipped FLIM setup drives the card
call for call as it did before (a golden test records it). With an
``inputs`` map, the role fields left at their defaults are ignored -- a
saved setup file carries ``photonsChannel: 1``, ``laserSyncChannel: 2``,
``lineClockChannel: 3`` whether or not anything is cabled there -- and a
role field set to another value adds that role's input. Refused at load,
with what to change: two inputs on one physical input (either edge: one
input has one trigger level), two line or two frame clocks (one scan, one
raster), a role declared both ways with other values, a level, dead time or
delay field of a role whose channel field is not set.

**Measurements.** ``flim`` builds a ``Flim`` on the detector's photon input
and the sync; ``counting`` builds a ``CountBetweenMarkers`` of the photon
input between the scan's pixel markers -- one count per pixel, no sync, no
fit -- and its image is the counts. (``CountBetweenMarkers`` is the vendor
class we expect to be the most direct per-pixel counter; its behaviour --
fills its values once, ``ready()`` then, ``clear()`` re-arms it -- is to be
confirmed on a real card. ``Flim`` itself would need a sync.)

**One scan session per card.** Every detector of a scan joins the card's
scan session while the scan is prepared, before the NI-DAQ starts the
clocks. The session owns what exists once per card per scan: the *pixel
markers* (one ``EventGenerator`` pair on the line clock, however many
detectors count between them -- a repeat scan of the same geometry keeps
them when every participant's last frame was closed by the card) and the
*hold*: conditioning stays refused until the *last* detector of the scan
has drained its final frame or was aborted or stopped. Every measurement
counts between the same markers, which fire only on line-clock edges, so
all of them start on the same first edge; a detector that would join after
the clocks started is refused. Each detector keeps its own worker, its own
final-frame acknowledgement, its own ``finishTimeoutS`` and its own retired
workers: a slow detector holds the end of the scan for its own deadline,
not the others' frames.

**Per input, per card.**

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Per input
     - trigger level, dead time, delay; a FLIM detector's ``t0_ps`` (applied
       to its own photon input's delay during its scan, put back after).
       One FLIM detector per photon input.
   * - Per card
     - the conditional filter (``filterSyncByPhotons``: every detector input
       triggers it, every sync input is filtered, so the TCSPC direction is
       the card's), the pixel-marker pattern offset
       (``pixelPatternOffsetPs`` plus a positive line-clock delay), the
       markers, the hold, the overflow counter. The frame clock still follows
       the line clock's delay and dead time when either is changed at run
       time.

**Provenance.** A detector shows its photon input's name, channel, level,
dead time and delay as read-only parameters (``click_role``,
``click_channel``, ``click_trigger``, ``click_deadtime_ps``,
``click_delay_ps``) and its ``measurement``; recordings store every
detector's parameters, so each dataset says which input it came from and
how it was conditioned. FLIM products carry the same under
``photon_input`` in their metadata, with the scan session's participants,
next to the card's own snapshot (``time_tagger``, which now lists every
input under ``inputs``).

Which level for which input
===========================

Every input is a comparator: it counts an edge when the signal crosses the
input's *trigger level*. Too close to 0 V it counts electrical noise, above
the pulses it counts nothing, and with the wrong sign it counts nothing at
all. Between the two there is a *plateau*: a range of levels that all count
the same rate, because every pulse clears them and no noise does. A good
level sits in the middle of the plateau, as far from the noise as from the
pulses' top, so a drift, a reflection or a weaker pulse does not move the
count.

All inputs are 50 Ω with a range of ±2.5 V. A source that is specified into
high impedance delivers about half its open-circuit voltage into 50 Ω. The
sign of the channel number in the ``timeTagger`` block is the polarity: a
negative channel triggers on the falling edge and takes a negative level.

.. list-table::
   :header-rows: 1
   :widths: 18 42 40

   * - Signal
     - Typical into 50 Ω
     - Level
   * - Detector pulses (SPAD, hybrid detector, PMT discriminator; any
       photon input)
     - NIM: about -0.4 to -0.8 V, a few ns
     - falling edge (negative channel), about -0.2 to -0.3 V
   * - Laser sync
     - either polarity, depending on the laser: NIM negative, a driver's
       sync +0.5 to +1 V; a photodiode sync can be under 0.1 V
     - the sign of the pulse, the middle of the plateau; under 0.1 V there
       is no safe level (amplify, or attenuate less)
   * - DAQ clocks (the scan's line and frame clocks)
     - an NI-DAQ line: about +1 to +1.5 V, not 3.3 or 5 V; they fire only
       while a scan runs
     - rising edge, about +0.5 V, never above 1 V
   * - Other TTL / pulse sources (a STED-beam photodiode, a trigger out)
     - whatever the source drives into 50 Ω
     - the plateau tells

Finding it
----------

The Lifetime widget's **Signals** panel has a row per configured input with
its live rate, its level and a **Find** button; the **check** column says
what is wrong with the level as it stands (the sign against the channel's
edge, inside the noise, a clock level above what a DAQ line reaches, an
input that counts nothing, a sync rate that disagrees with
``laser_rep_rate_mhz``). **Find** sweeps the input -- the polarity from the
channel's sign, the other sign probed when nothing but noise counts, a
coarse sweep from 20 mV to 2.5 V and finer steps around the plateau's edges
-- and shows the level, the plateau, its width and a verdict. Nothing is
written until you click **Apply** (until restart) or **Apply and keep in
setup file**, which writes the level into the ``timeTagger`` block of the
active setup file; **Copy setup line** copies the line instead. From a
script it is one call, for a role or any channel number::

   result = tt.find_trigger_level('photons')    # or tt.find_trigger_level(-5)
   print(result.summary())                      # verdict, level, plateau, what to do
   result.block_line()                          # '"photonsTriggerV": -0.283'

The level is put back afterwards -- also on an error or a Stop -- unless
``apply=True`` is passed and the verdict is ``good``. It runs inside a
calibration transaction, so it is refused while a scan holds the card.

.. list-table::
   :header-rows: 1
   :widths: 18 40 42

   * - Verdict
     - Means
     - Do
   * - ``good``
     - a plateau at least 0.1 V wide, clear of the noise
     - apply the level
   * - ``too_small``
     - the pulses barely clear the noise: the plateau is narrow or absent
     - attenuate less, amplify, shorten the cable; the level shown is the
       best there is, not a safe one
   * - ``wrong_polarity``
     - nothing counts with the channel's sign, a plateau with the other
     - flip the sign of the channel number in the block (and the level
       with it): the pulses have the other polarity
   * - ``noise_only``
     - counts only next to 0 V, falling off without a plateau
     - nothing usable arrives (nothing cabled, a laser not pulsing) or the
       pulses are under about 0.1 V; ``rep_rate()`` at a small level tells
       a real sync (a stable period) from noise
   * - ``no_signal``
     - no counts at any level of either sign
     - check the cable, the input number, that the source is on
   * - ``needs_scan``
     - a DAQ clock between scans: it fires only while a scan runs
     - keep about +0.5 V; to check it, run the scan with the time-resolved
       detector's ``enabled`` set to ``False`` (the APD images, the card is
       free) and Repeat on, and Find again -- or tutorial 07

A card that only counts photons needs only its photon input: set
``laserSyncChannel`` and ``lineClockChannel`` to ``null`` and the Signals
panel, the pre-flight and the tutorials look at nothing else.

What the card does for ImSwitch2
================================

``TimeTaggerManager`` (``imswitch.imcontrol.model.managers.TimeTaggerManager``)
is the shared device manager, built by ``MasterController`` and handed to
the detectors like ``nidaqManager``. It

* opens the card (or the mock) once and applies the block's conditioning:
  trigger levels, dead times, delays, the conditional filter;
* keys the inputs by name, resolves roles to inputs, and records a
  snapshot of itself (every input's conditioning) into every time-resolved
  product's metadata;
* runs one scan session per scan for every detector on the card: one pair
  of pixel markers, and the card held from preparation until the last
  detector's final frame has been drained, refusing trigger-level, delay
  and dead-time changes meanwhile; conversely a *calibration transaction* (the rep-rate
  measurement, a trigger sweep) owns the conditioning for its duration and
  refuses a scan from starting on temporary settings;
* is the one reader of the card's read-and-clear overflow counter and keeps
  a monotonic total, so a frame's validity and the health strip each keep
  their own baseline and neither can hide an overflow from the other;
* offers ``health()`` (per-role count rates, overflow delta, direction) and
  an optional background sampler for a widget;
* frees the card at shutdown, after the detectors have stopped.

Bandwidth and the conditional filter
------------------------------------

Every tag the card transmits costs USB bandwidth: about 8.5 M tags/s on a
Time Tagger 20, 65--80 M on an Ultra, around 1 G on an X. An 80 MHz laser
sync alone is over the first two. The card's *conditional filter*
(``filterSyncByPhotons``) transmits only the first sync after each photon,
cutting the sync to the photon rate -- and reversing the TCSPC direction,
because the photon now starts the histogram and the sync stops it. The
FLIM detector handles that (swapped ``Flim`` channels, the time axis
mirrored on the laser period, ``t0`` as a circular roll, a window of at
least one period), see *TCSPC direction* in :doc:`../devices/detectors`.
Dividing the sync (``setEventDivider``) is **not** a remedy for FLIM: the
window becomes N periods and nothing folds it; it is only how the rep rate
is measured safely. Reverse mode is pending an acceptance test on a real
card: the vendor documents that the filter can reorder timestamps on some
models, which the mock does not reproduce.

From a script: ``facade.time_tagger``
=====================================

.. code-block:: python

   facade = api.imcontrol.buildWorkflowFacade()
   tt = facade.time_tagger          # None on a setup without a timeTagger block

   tt.inputs()                      # every declared input by name (the roles, in the older form)
   tt.roles(); tt.detector_inputs()  # the roles some input plays; the detector inputs
   tt.channels()                    # {input: ChannelReport(channel, edge, trigger_v, kind, plays, ...)}
   tt.count_rates(['photons', 'laser_sync'], duration_s=1.0).rates_hz   # default: every input
   tt.rep_rate(duration_s=2.0)      # RepRateResult: rate_hz, period_ps, jitter_ps, card_limited
   tt.histogram(duration_s=2.0)     # HistogramResult in forward time: t_axis_ns, counts, peak_ns, fwhm_ns
   tt.find_trigger_level('photons')  # LevelResult: verdict, level_v, plateau_v, block_line()
   tt.find_trigger_level('apd_red')  # any input by name; its line goes into inputs["apd_red"]
   tt.trigger_sweep('photons', levels_v, duration_s=0.2)   # SweepResult with plateau_v
   tt.dark_rates(['photons'], duration_s=2.0)
   with tt.test_signal_on(['line_clock']): ...            # owns the card, off again afterwards
   tt.overflows(); tt.health(); tt.metadata(); tt.tcspc_direction
   tt.set_trigger_level('photons', -0.3); tt.set_delay('line_clock', 800); tt.set_deadtime('photons', 50_000)
   tt.preflight(detector_name='FLIM').summary()

   # while a scan runs (tutorials 07 to 09)
   tt.count_edges('line_clock', duration_s=3.0, start=run.start)   # spans the scan from its first edge
   tt.period('line_clock', duration_s=0.5, expected_period_ps=...)  # PeriodResult: period_ps, jitter_ps
   tt.skew('frame_clock', 'line_clock', added_delay_ps=50_000)    # SkewResult: signed skew_ps
   tt.scope(['frame_clock', 'line_clock'], 'frame_clock', window_ps, detector_name='FLIM')
   tt.pattern_offset_ps(); tt.mock_truth(ny, nx); tt.set_mock_fault('line_delay_ps', -800_000)
   tt.mock_truth(ny, nx, 'apd_red')  # what the mock's detector on that input images
   tt.sted_pulse_delay(duration_s=1.0)   # HistogramResult of the STED photodiode vs the sync

Every result has a ``summary()`` string and a ``to_dict()``. Every blocking
call sleeps in slices that honour the script's Stop button, waits on the
card in bounded steps (a measurement that does not finish within a few
seconds of its capture is an error, never a worker blocked for good) and
stops its measurement object afterwards. ``rep_rate``, ``histogram``,
``skew``, ``trigger_sweep``, ``find_trigger_level`` and ``test_signal_on``
run inside a calibration transaction and put the card back exactly as they found it (the divider
and filter that were set, the input delay, the level), also when the
script is stopped; ``histogram`` measures with the photon input at the
block's configured delay, so its peak is the absolute ``t0_ps``. A
conditioning write while a scan holds the card raises
``TimeTaggerBusyError`` with the holder's name. The scan-clock
measurements (``count_edges``, ``period``, ``scope``) are plain
measurements and run during a scan; ``skew`` is a conditioning write, so
tutorial 08 runs its scan with the FLIM detector's ``enabled`` parameter
off (the APD images, the card is not held). There is no raw handle to the
vendor object: what the facade does not offer, a later release adds here.
``set_mock_laser(on)``, ``set_mock_fault(name, value)`` and
``mock_truth(ny, nx)`` act on the *mock* card only (it cannot see the
rig's lasers; the rig's truth is the APD image) and do nothing, or return
``None``, on a real card.

The tutorials
=============

``tutorial/timetagger`` in your scripts folder, all on
``galvo_flim_mock_scan_setup.json`` (see :ref:`scripting-tutorials`):

01
    Meet the card: roles and channels, count rates, the test signal.
02
    Trigger levels and dead time: the plateau, by hand with
    ``trigger_sweep`` and then for every input with ``find_trigger_level``
    and its verdicts; 50 Ω inputs; the SPAD's own dead time.
03
    Dark counts and afterpulsing with the excitation blocked, into the
    detector's ``background_rate_hz``.
04
    The laser sync: ``rep_rate`` by the divided-and-unfiltered procedure,
    period jitter and the card's own floor, into ``laser_rep_rate_mhz``.
05
    Bandwidth, overflows, the conditional filter and the TCSPC direction.
06
    The IRF and ``t0``: the photon-vs-sync histogram, its peak and width,
    into ``t0_ps`` (an absolute position: running it twice gives the same
    number).
07
    The line clock during a scan: every line edge counted against Ny, the
    period against dwell x Nx, a trigger sweep between APD-only runs, into
    ``lineClockTriggerV``.
08
    The frame clock and the pixel markers: the frame trigger level, the
    signed frame-to-line skew against ``pixelPatternOffsetPs``, a scope
    trace of one line, and the check that the card closes the last frame.
09
    Line-delay alignment: the FLIM image against the APD image (the mock
    sample's truth on the mock), pixels to picoseconds, into
    ``lineClockDelayPs``, and a re-run that shows the shift at 0.
10
    The pre-flight checklist: ``preflight()``, green/red, naming the
    tutorial that fixes each red line.
11
    Binned photon arrivals: the cube and gate images saved to HDF5
    (format version 2) in the Recording folder, read back with
    ``load_products``.
12
    Gated STED: gates from a peak-relative preset (``gate_presets/``, shared
    with the Lifetime widget), the late/early ratio, and the STED pulse
    against the sync (``sted_pulse_delay``, with the optional photodiode).
13
    Tau STED: the per-pixel lifetime image and what the products carry.
14
    Several detectors on one card (on
    ``galvo_two_apd_counting_mock_setup.json``): the ``inputs`` block, a
    level per detector input, two counting detectors in one scan, and the
    check that each image came from its own input.

Steps 07 to 13 load ``scan_params/flim_scan_64px.json`` into the Scan
widget for their runs (64 x 64 pixels at 400 us, about 2 s) and put the
previous settings back. Each measurement prints a suggestion and writes it
only when the script's ``APPLY`` is True; a written value holds until
restart, and the printed JSON line goes into the ``timeTagger`` block to
keep it (for trigger levels, the Signals panel's *Apply and keep in setup
file* does that for you).

``scripts/diagnostics/measure_laser_rep_rate.py`` remains for a rig without
the GUI: it runs the same rep-rate measurement on a bare card.
