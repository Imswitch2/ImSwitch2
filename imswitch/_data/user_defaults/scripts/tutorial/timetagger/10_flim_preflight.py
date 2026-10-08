"""Tutorial timetagger 10 -- The FLIM pre-flight checklist.

You will learn
  * ``preflight()``: the one call that runs tutorials 02 to 06's checks and
    prints a green/red list -- the same list the Lifetime widget shows
  * which tutorial fixes which red line
  * that the checks needing a scan (line clock edges, frame clock, last
    frame) are reported as skipped here: tutorials 07 to 09 run them

Setup
  Mock setup:   galvo_flim_mock_scan_setup.json
  It simulates: a well-cabled card: everything is green, with one warning
                until tutorial 03's background rate is set.
  Your own microscope: run this before every FLIM session; a red line
                names the tutorial that finds the cause.

Next: that was the last Time Tagger tutorial of this release. The
      workflows folder has the gated-STED and tau-STED acquisitions.
"""

helpers = importScript('timetagger_helpers.py')
tt = helpers.findTimeTagger()
DETECTOR = helpers.findFlimDetector()

report = tt.preflight(detector_name=DETECTOR, duration_s=1.0)
print(report.summary())
print()

if report.ok and not report.warnings:
    print('All green: the card, the sync, the window and the photons are as the')
    print('FLIM detector expects. Run a scan.')
elif report.ok:
    print('Green with warnings. Each warning names the tutorial that settles it.')
else:
    print('RED: fix the failing lines first, with the tutorial each one names.')
print()
print('Three lines are skipped: they need a running scan. Tutorials 07 (line clock),')
print('08 (frame clock and pixel markers) and 09 (line delay) run them.')
