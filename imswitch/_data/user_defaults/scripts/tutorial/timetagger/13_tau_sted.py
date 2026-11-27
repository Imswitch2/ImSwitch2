"""Tutorial timetagger 13 -- Tau STED: a lifetime image from a scan.

You will learn
  * ``TauSTEDWorkflow``: a FLIM scan fitted per pixel (the detector's
    fit method, minimum counts and rep rate, taken from its settings) and
    saved as intensity, lifetime, decay and metadata
  * what the products carry (format version 2): the TCSPC direction, the
    background rate subtracted, the worst pile-up, the overflows, and how
    many scans were summed
  * where the per-pixel lifetime is trustworthy: above the count threshold
    and below a few percent of pile-up

Setup
  Mock setup:   galvo_flim_mock_scan_setup.json
  It simulates: the beads sample: 2.5 ns beads on a 1.0 ns background.
  Your own microscope: a configured FLIM scan; tutorials 03 (background),
                04 (rep rate) and 06 (t0) first, so the fit has its
                numbers; 10 says whether it does.

Next: 10_flim_preflight.py is the checklist before every session. The
      Lifetime widget does 11 to 13 interactively (Run once, Live, Save).
"""

import os

import numpy as np

from imswitch.imcontrol.model.workflows import (
    LifetimeFitConfig,
    TauSTEDParams,
    TauSTEDWorkflow,
)

helpers = importScript('timetagger_helpers.py')
tt = helpers.findTimeTagger()
DETECTOR = helpers.findFlimDetector()
PARAMS = os.path.join(getScriptDirPath(), 'scan_params', 'flim_scan_64px.json')
if DETECTOR is None:
    raise RuntimeError('This setup has no FLIM detector.')

# The fit the detector is set up for; a script may override any of it.
fit = LifetimeFitConfig(
    method=str(api.imcontrol.getDetectorParameter(DETECTOR, 'fit_method')),
    min_counts_per_pixel=int(api.imcontrol.getDetectorParameter(DETECTOR, 'min_counts_per_pixel')),
    laser_rep_rate_mhz=float(api.imcontrol.getDetectorParameter(DETECTOR, 'laser_rep_rate_mhz')),
)
print(f'fit: {fit.method}, at least {fit.min_counts_per_pixel} photons per pixel, '
      f'{fit.laser_rep_rate_mhz} MHz')

backup, design = helpers.loadScanParams(PARAMS)
flimWas = helpers.setFlimEnabled(DETECTOR, True)
try:
    facade = api.imcontrol.buildWorkflowFacade(time_resolved_detector_name=DETECTOR)
    params = TauSTEDParams(
        fit=fit,
        capture_cube=False,
        save_folder=api.imcontrol.getRecFolder(),
        measurement_name='tutorial13_tau',
        save_h5=True, save_npz=False, save_tiff=True,
        timeout_s=120.0,
    )
    result = TauSTEDWorkflow(facade, params).run()
    p = result.products
    tau = p.lifetime_ns
    valid = np.isfinite(tau) & (tau > 0)
    print(f'lifetime image {tau.shape}: {int(valid.sum())} of {tau.size} pixels fitted, '
          f'median {np.median(tau[valid]):.2f} ns, global tau {p.global_tau_ns:.2f} ns')
    print(f'{p.tcspc_direction} TCSPC, background {p.background_rate_hz:.0f} Hz subtracted, '
          f'pile-up max {100 * p.pileup_max:.1f} %, overflows {p.overflows}, '
          f'{p.frames_accumulated} scan(s) summed')
    if p.pileup_max > 0.05:
        print('  -> pile-up above 5 %: the brightest pixels read short; lower the power.')
    print('written:')
    for label, path in result.output_paths.items():
        print(f'  {label}: {path}')
    print()
    print('In the Lifetime widget, Tau STED mode shows the same scan live: lifetime')
    print('against intensity per pixel, the pile-up map, and accumulate sums scans.')
finally:
    helpers.restoreFlimEnabled(DETECTOR, flimWas)
    api.imcontrol.loadScanParamsFromFile(backup)
