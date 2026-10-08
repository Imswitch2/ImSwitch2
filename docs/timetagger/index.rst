*******************************
The Swabian Time Tagger
*******************************

One Swabian Time Tagger card serves everything time-resolved in a setup:
the FLIM detector (:ref:`SwabianTimeTaggerManager <swabian-detector>` in
:doc:`../devices/detectors`), the calibration and debugging tutorials, and
the scripting facade. This chapter is the card's own page: how it is
described in the setup file, what the card can do, what ImSwitch2 does with
it, and how a script reaches it. :doc:`debugging` maps symptoms to the
tutorial that finds the cause.

The ``timeTagger`` block
========================

The card is described once, in a top-level ``timeTagger`` block of the setup
JSON (reference: :doc:`../setupinfo-reference`). Its inputs are named by
*role*, and every consumer -- the detector's ``click_role`` /
``start_role`` / ``line_role``, the tutorials, the facade -- refers to the
roles, so re-cabling the card is one edit here:

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
       ``pixelPatternOffsetPs``
     - the scan's ``scan.lineClockLine``; the pixel markers are generated
       from its edges, starting ``pixelPatternOffsetPs`` (default 10 ns)
       after each so the frame edge leads pixel 0. ``lineClockDelayPs``
       moves the markers against the beam: positive when the clock fires
       *early* (a galvo lagging its command; the detector delays its
       pattern), negative when it arrives *late* (a long cable; the card
       moves the timestamps earlier). Tutorial 09 measures it.
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

What the card does for ImSwitch2
================================

``TimeTaggerManager`` (``imswitch.imcontrol.model.managers.TimeTaggerManager``)
is the shared device manager, built by ``MasterController`` and handed to
the detectors like ``nidaqManager``. It

* opens the card (or the mock) once and applies the block's conditioning:
  trigger levels, dead times, delays, the conditional filter;
* resolves roles to channels and records a snapshot of itself into every
  time-resolved product's metadata;
* holds the card for a scan from preparation until the detector's final
  frame has been drained, and refuses trigger-level, delay and dead-time
  changes meanwhile; conversely a *calibration transaction* (the rep-rate
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

   tt.channels()                    # {role: ChannelReport(channel, edge, trigger_v, ...)}
   tt.count_rates(['photons', 'laser_sync'], duration_s=1.0).rates_hz
   tt.rep_rate(duration_s=2.0)      # RepRateResult: rate_hz, period_ps, jitter_ps, card_limited
   tt.histogram(duration_s=2.0)     # HistogramResult in forward time: t_axis_ns, counts, peak_ns, fwhm_ns
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
   tt.sted_pulse_delay(duration_s=1.0)   # HistogramResult of the STED photodiode vs the sync

Every result has a ``summary()`` string and a ``to_dict()``. Every blocking
call sleeps in slices that honour the script's Stop button, waits on the
card in bounded steps (a measurement that does not finish within a few
seconds of its capture is an error, never a worker blocked for good) and
stops its measurement object afterwards. ``rep_rate``, ``histogram``,
``skew``, ``trigger_sweep`` and ``test_signal_on`` run inside a calibration
transaction and put the card back exactly as they found it (the divider
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
    Trigger levels and dead time: ``trigger_sweep`` and the plateau; 50 Ω
    inputs; the SPAD's own dead time.
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

Steps 07 to 13 load ``scan_params/flim_scan_64px.json`` into the Scan
widget for their runs (64 x 64 pixels at 400 us, about 2 s) and put the
previous settings back. Each measurement prints a suggestion and writes it
only when the script's ``APPLY`` is True; a written value holds until
restart, and the printed JSON line goes into the ``timeTagger`` block to
keep it.

``scripts/diagnostics/measure_laser_rep_rate.py`` remains for a rig without
the GUI: it runs the same rep-rate measurement on a bare card.
