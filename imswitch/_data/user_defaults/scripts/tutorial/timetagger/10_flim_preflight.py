"""Tutorial timetagger 10 -- The FLIM pre-flight checklist.

You will learn
  * ``preflight()``: the one call that runs tutorials 02 to 06's checks and
    prints a green/red list -- the same list the Lifetime widget shows
  * which tutorial fixes which red line
  * that the checks needing a scan (line clock edges, frame clock, last
    frame) are reported as skipped here: tutorials 07 to 09 run them
  * the extended mode (``EXTENDED = True``): the convergence test of the
    validation campaign -- N scans of a reference dye, every fit method
    refitted on the accumulated photons, the lifetime's stability and
    its bias against the reference tabulated and saved

Setup
  Mock setup:   galvo_flim_mock_scan_setup.json
  It simulates: a well-cabled card: everything is green, with one warning
                until tutorial 03's background rate is set.
  Your own microscope: run this before every FLIM session; a red line
                names the tutorial that finds the cause. For the extended
                mode put a reference dye under the beam and set
                REFERENCE_TAU_NS to its lifetime (the mock uses its
                sample's known map).

Next: that was the last Time Tagger tutorial of this release. The
      workflows folder has the gated-STED and tau-STED acquisitions.
"""

import json
import os

helpers = importScript('timetagger_helpers.py')
tt = helpers.findTimeTagger()
DETECTOR = helpers.findFlimDetector()
EXTENDED = False            # the convergence test of docs/timetagger/validation
N_SCANS = 8                 # scans accumulated; exp1 needs about 1000 photons per pixel
REFERENCE_TAU_NS = None     # the reference dye's lifetime; None: the mock's truth
TOLERANCE_NS = 0.1
PARAMS = os.path.join(getScriptDirPath(), 'scan_params', 'flim_scan_64px.json')

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

if EXTENDED and DETECTOR is not None:
    from imswitch.imcontrol.model.timeresolved.validation import convergence_report
    from imswitch.imcontrol.model.workflows import (
        BinnedPhotonArrivalParams,
        BinnedPhotonArrivalWorkflow,
    )

    # N scans with the cube kept, so every accumulation can be refitted
    # with each method the way the detector fits a frame.
    print()
    print(f'Extended mode: {N_SCANS} scans of the reference, every fit method.')
    backup, design = helpers.loadScanParams(PARAMS)
    flimWas = helpers.setFlimEnabled(DETECTOR, True)
    try:
        facade = api.imcontrol.buildWorkflowFacade(time_resolved_detector_name=DETECTOR)
        params = BinnedPhotonArrivalParams(save_h5=False, save_npz=False, save_tiff=False,
                                           timeout_s=120.0, measurement_name='convergence')
        products = []
        for k in range(N_SCANS):
            products.append(BinnedPhotonArrivalWorkflow(facade, params).run().products)
            print(f'  scan {k + 1}: {int(products[-1].decay_counts.sum()):,} photons')
        reference = REFERENCE_TAU_NS
        if reference is None:
            truth = tt.mock_truth(*products[0].intensity.shape)
            reference = truth[1] if truth is not None else None
            if reference is None:
                print('  no reference: set REFERENCE_TAU_NS to the dye\'s lifetime for the bias')
        convergence = convergence_report(products, reference_tau_ns=reference,
                                         tolerance_ns=TOLERANCE_NS)
        print(convergence.summary())
        out = os.path.join(api.imcontrol.getRecFolder(), 'flim_convergence.json')
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'w', encoding='utf-8') as file:
            json.dump(convergence.to_dict(), file, indent=2)
        print(f'saved {out} -- the number for the sign-off log (docs/setup-validation).')
    finally:
        helpers.restoreFlimEnabled(DETECTOR, flimWas)
        api.imcontrol.loadScanParamsFromFile(backup)
