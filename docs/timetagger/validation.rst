**********************************
Validating a FLIM setup on the rig
**********************************

The Lifetime 2.0 work was built and tested against the simulated card.
Before the FLIM row of the etSTED validation (ROADMAP 13.A) is closed, the
numbers below are measured on the real card and written into the sign-off
log, ``docs/setup-validation/etsted.md``. Every step is a shipped tutorial
or a widget action; nothing here needs code.

Prerequisites
=============

* The setup file has a ``timeTagger`` block (:doc:`index`) with every
  cabled role, ``simulation`` off, and ``Lifetime`` in ``availableWidgets``.
* A reference dye of known lifetime under the beam for steps 6 and 10
  (fluorescein in 0.1 M NaOH, 4.0 ns, or any dye with a single lifetime
  the lab trusts), a scattering target for the IRF, and the usual sample
  for the scans.
* The Recording widget pointed at the folder the files should land in.

Steps and acceptance criteria
=============================

.. list-table::
   :header-rows: 1
   :widths: 6 34 40 20

   * - Step
     - Tutorial / action
     - Passes when
     - Writes
   * - 1
     - ``01_meet_the_card``
     - every cabled role counts; the test signal counts on an input and is
       off again afterwards
     - --
   * - 2
     - ``02_trigger_levels_and_dead_time``
     - the photon and sync sweeps show a plateau; the chosen levels sit
       inside it
     - ``photonsTriggerV``, ``laserSyncTriggerV``, ``photonsDeadtimePs``
   * - 3
     - ``03_dark_counts_and_afterpulsing``
     - the dark rate is stable over repeats and below a few percent of the
       lit rate at working power
     - ``background_rate_hz``
   * - 4
     - ``04_laser_sync``
     - the measured rate is within 0.5 % of the laser's own display; the
       period jitter is at or near the card's floor
     - ``laser_rep_rate_mhz``
   * - 5
     - ``05_bandwidth_and_the_filter``
     - no overflows over 2 s at working power; on a Time Tagger 20 or Ultra
       the filter is on and the direction reads reverse
     - ``filterSyncByPhotons``
   * - 6
     - ``06_irf_and_t0`` on the scattering target
     - the IRF FWHM is what the detector's data sheet says (a SPAD: 50 to
       350 ps); running it twice suggests the same ``t0_ps`` within a bin
     - ``t0_ps``, ``timetagger_irf.npz``
   * - 7
     - ``07_line_clock_during_a_scan``
     - edges in one scan equal the designed Ny; the period equals Nx x dwell
       plus the designer's flyback within 1 %; the line level sits on its
       plateau
     - ``lineClockTriggerV``
   * - 8
     - ``08_frame_clock_and_pixel_markers``
     - the frame level sees its one edge; the signed frame-to-line skew is
       below ``pixelPatternOffsetPs`` (frame leads pixel 0); the final frame
       is closed by the card (``frame_closed_by_card`` true)
     - ``frameClockTriggerV``, ``pixelPatternOffsetPs`` if raised
   * - 9
     - ``09_line_delay_alignment``
     - the second run's shift against the APD image is 0 px
     - ``lineClockDelayPs``
   * - 10
     - ``10_flim_preflight`` with ``EXTENDED = True`` and
       ``REFERENCE_TAU_NS`` set to the dye's lifetime
     - the pre-flight is green; in the convergence table every method's
       median lifetime has converged (the last two accumulations agree
       within the tolerance); each method's bias against the reference is
       recorded: the moment and the phasor read low by the window's
       truncation and the IRF width, ``exp1`` reads high while the tail's
       bins are sparse and its bias must fall from one accumulation to
       the next
     - ``flim_convergence.json``
   * - 11
     - ``11_binned_photon_arrivals``
     - the file reads back with ``load_products``; the cube is integer; the
       gates' resolved bounds are the configured ones plus the peak
     - ``tutorial11_cube_*.h5``
   * - 12
     - ``12_gated_sted`` and the Lifetime widget in Gated STED mode with the
       same preset, Run once, Save
     - the two HDF5 files carry the same gate attributes
       (``start_ns``, ``stop_ns``, ``reference``, ``resolved_*``), the same
       ``fit`` attributes and the same ``time_tagger`` conditioning; the
       late/early ratio medians agree within the photon noise of two scans
     - ``tutorial12_gated_*.h5`` and the widget's file
   * - 13
     - ``13_tau_sted``
     - the per-pixel median lifetime agrees with step 10's ``exp1`` for the
       same dye; ``pileup_max`` is below 5 % at working power
     - ``tutorial13_tau_*.h5``
   * - 14
     - the Lifetime widget, Live for a minute on the reference dye with the
       intensity-weighted overlay
     - the overlay's hue is uniform over the field; the status strip shows
       no overflows; Stop ends the run within one scan
     - --

Recording the result
====================

The sign-off log ``docs/setup-validation/etsted.md`` takes one row per
step: the commit, the date, the tester and the measured numbers. The
follow-up plan (``docs/design/plans/lifetime-2-1.md``) takes the rig
numbers it depends on: the IRF width, the dark rate, the line period and
flyback, the frame-to-line skew, the line delay, and the convergence
table.

What a failure means
====================

A red step points at its tutorial, whose output names the setting; the
symptom table in :doc:`debugging` covers the rest. Two failures are
expected to need code rather than a setting: a linestep scan (``S > 1``)
is refused until ROADMAP M9 carries one line edge per linestep, and a
reverse-mode card that reorders filtered timestamps (the vendor documents
this on some models) shows in step 5 as a decay that reads wrong and is
the acceptance test the Lifetime 2.0 plan (section 3.2) keeps open.
