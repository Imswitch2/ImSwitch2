********************************
Debugging a FLIM setup
********************************

Symptom first, then the tutorial in ``tutorial/timetagger`` (or the setting)
that finds the cause. Start every session with tutorial 10, the pre-flight
checklist: a red line names the tutorial below.

.. list-table::
   :header-rows: 1
   :widths: 30 40 30

   * - Symptom
     - Likely cause
     - Find it with
   * - "Time Tagger is not available" at scan start; the scan rolls back
     - the vendor library is missing or the card is not on the USB
     - the log at startup; ``simulation: true`` on a bench without a card
   * - The FLIM image is all zeros; the log says "zero photons in all pixels"
     - the line clock never fires (not cabled, wrong input, threshold above
       what a DAQ line drives into 50 Ω), or the photon input's trigger has
       the wrong sign
     - tutorial 01 (test signal: does the input count at all?), 02 (photon
       polarity and plateau), 07 (line edges during a scan)
   * - ``line 0 Hz`` on the health strip during a scan
     - the line clock threshold is too high: a DAQ line is about 1 V into
       50 Ω
     - tutorial 02's rule, ``lineClockTriggerV`` around 0.5 V; tutorial 07
   * - Lifetimes all read about the same short value (0.8--1 ns)
     - a histogram window much shorter than the laser period: the decay is
       cut off, and the fits read the cut as a short lifetime
     - tutorial 04 (the rate the window is derived from); leave ``n_bins``
       undeclared
   * - Lifetimes read low across the board
     - a flat background (dark counts, afterpulsing) not subtracted, or a
       wide IRF: the moment and phasor reference the IRF peak
     - tutorial 03 (``background_rate_hz``), tutorial 06 (IRF width)
   * - Lifetimes change with the laser power
     - pile-up: more than a few percent of the laser pulses detect a photon
     - tutorial 10's pile-up line; lower the power
   * - The decay looks reversed, or rises at the end of the window
     - the conditional filter is on but the detector thinks it is off (or
       the reverse), or the window is shorter than a period in reverse mode
     - tutorial 05; ``filterSyncByPhotons`` and the detector's log line
       "forward/reverse TCSPC"
   * - The log reports USB overflows; frames are marked invalid
     - the tag rate is over the card's budget: an unfiltered sync at tens of
       MHz on a Time Tagger 20 or Ultra
     - tutorial 05; ``filterSyncByPhotons: true``, or a dead time on the
       photon input
   * - The image is shifted along x against the APD image
     - the line clock fires early for the galvo's position (the mirror lags
       its command: the known ``phase_delay`` offset) or late (a long cable)
     - tutorial 09; ``lineClockDelayPs`` (positive for an early clock,
       negative for a late one)
   * - The FLIM image misses lines, or the log says the card closed no frame
     - line edges lost at the card: a marginal line trigger level, ringing,
       or more linesteps than the clock carries edges for
     - tutorial 07 (edges counted against Ny); tutorial 08's last-frame
       check
   * - The log says the card did not close the last frame
     - a missing pixel marker, or the frame edge arriving after pixel 0
       (frame-to-line skew above ``pixelPatternOffsetPs``)
     - tutorial 08 (``skew()`` and ``frame_closed_by_card``)
   * - A scan-aware tutorial says "held by a calibration" or the sweep is
       refused
     - the FLIM detector took part in the scan and holds the card
     - the tutorials switch the detector's ``enabled`` parameter off for
       their APD-only runs; do the same for your own measurements
   * - A trigger-level change is refused: "a scan holds the card"
     - a scan is being prepared or has not drained its final frame yet
     - wait for the scan to end; the FLIM detector holds the card until its
       last frame has landed
   * - A scan refuses to start: "held by a calibration"
     - a tutorial's ``rep_rate`` or ``trigger_sweep`` is running
     - let it finish (or Stop it: the transaction is released)
   * - A workflow fails at configure: "owned by another run"
     - another workflow or the Lifetime widget has the detector's product
       session open
     - wait for it; a session is cleared by its owner when it finishes
