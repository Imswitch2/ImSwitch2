"""Tutorial timetagger 12 -- Gated STED: software gates on the arrival times.

You will learn
  * ``GatedSTEDWorkflow``: a FLIM scan reduced to one image per time gate
    (photons that arrived in a window after the excitation), without
    keeping the cube
  * gate presets: JSON files shared with the Lifetime widget, whose gates
    are *peak-relative* (measured from the IRF peak), so they survive a
    change of ``t0_ps`` or of the cable lengths
  * the late/early ratio as the gated-STED contrast, and where the STED
    pulse sits (``sted_pulse_delay()``) when a photodiode on the STED beam
    is cabled

Setup
  Mock setup:   galvo_flim_mock_scan_setup.json
  It simulates: the beads sample with two lifetimes, so the late gate
                keeps the slow population and drops the fast one; a STED
                photodiode on input 5 firing 300 ps after the excitation.
  Your own microscope: a configured FLIM scan; the ``sted_pulse`` role is
                optional (the marker is skipped without it). The gates are
                detection-side only: the STED timing itself is the laser's.

Next: 13_tau_sted.py
"""

import os

import numpy as np

from imswitch.imcontrol.model.timeresolved import load_gate_preset
from imswitch.imcontrol.model.workflows import GatedSTEDParams, GatedSTEDWorkflow

helpers = importScript('timetagger_helpers.py')
tt = helpers.findTimeTagger()
DETECTOR = helpers.findFlimDetector()
PARAMS = os.path.join(getScriptDirPath(), 'scan_params', 'flim_scan_64px.json')
PRESET = os.path.join(getScriptDirPath(), 'gate_presets', 'sted_early_late.json')
if DETECTOR is None:
    raise RuntimeError('This setup has no FLIM detector.')

preset = load_gate_preset(PRESET)
print(f"preset {preset['name']}: "
      + ', '.join(f'{g.name} {g.start_ns:g}-{g.stop_ns:g} ns after the {g.reference}'
                  for g in preset['gates']))
numerator, denominator = preset['ratio']

# Where the STED pulse sits, if a photodiode sees it: on the same absolute
# axis as tutorial 06's IRF peak, so the difference is the STED delay.
if 'sted_pulse' in tt.roles():
    rep_rate_mhz = float(api.imcontrol.getDetectorParameter(DETECTOR, 'laser_rep_rate_mhz'))
    sted = tt.sted_pulse_delay(duration_s=1.0, laser_rep_rate_mhz=rep_rate_mhz)
    irf = tt.histogram(duration_s=1.0, laser_rep_rate_mhz=rep_rate_mhz)
    print(f'STED pulse at {sted.peak_ns:.3f} ns after the sync, the IRF peak at '
          f'{irf.peak_ns:.3f} ns: the depletion comes {1000 * (sted.peak_ns - irf.peak_ns):.0f} ps '
          'after the excitation; the early gate should start after it.')
else:
    print('No sted_pulse role: the STED pulse marker is skipped.')
print()

backup, design = helpers.loadScanParams(PARAMS)
flimWas = helpers.setFlimEnabled(DETECTOR, True)
try:
    facade = api.imcontrol.buildWorkflowFacade(time_resolved_detector_name=DETECTOR)
    params = GatedSTEDParams(
        gates=preset['gates'],
        capture_cube=False,
        save_folder=api.imcontrol.getRecFolder(),
        measurement_name='tutorial12_gated',
        save_h5=True, save_npz=False, save_tiff=True,
        timeout_s=120.0,
    )
    result = GatedSTEDWorkflow(facade, params).run()
    products = result.products
    for name, image in products.gate_images.items():
        print(f'gate {name}: {int(image.sum()):,} photons, mean {image.mean():.1f} per pixel')
    late, early = products.gate_images[numerator], products.gate_images[denominator]
    lit = early > 0
    ratio = np.where(lit, late / np.maximum(early, 1), 0.0)
    print(f'{numerator}/{denominator} ratio: median {np.median(ratio[lit]):.2f}, '
          f'range {ratio[lit].min():.2f}-{ratio[lit].max():.2f} over {int(lit.sum())} lit pixels')
    print('  -> a long lifetime keeps its photons for the late gate (high ratio); a')
    print('     short one, or STED-depleted fluorescence, does not.')
    print('written:')
    for label, path in result.output_paths.items():
        print(f'  {label}: {path}')
    print()
    print("In the Lifetime widget: Gated STED mode, presets -> sted_early_late, Run.")
    print('The gate images appear as layers, the regions on the decay can be dragged.')
finally:
    helpers.restoreFlimEnabled(DETECTOR, flimWas)
    api.imcontrol.loadScanParamsFromFile(backup)
