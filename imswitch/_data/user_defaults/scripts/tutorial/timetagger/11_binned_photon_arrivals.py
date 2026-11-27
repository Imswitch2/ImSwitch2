"""Tutorial timetagger 11 -- Binned photon arrivals: the whole cube, saved.

You will learn
  * ``BinnedPhotonArrivalWorkflow``: a FLIM scan whose per-pixel TCSPC
    histograms (the cube) are kept and written, with gate images, to HDF5
    and TIFFs in the Recording folder -- the files the Lifetime widget's
    Save writes, through the same code
  * the HDF5 layout (format version 2): time_resolved/ with the integer
    cube, gates/ with each gate's relative and resolved bounds, fit/,
    time_tagger/ (the card and its conditioning) and background/
  * ``load_products()``: a saved file read back into the same products
    object a run returns

Setup
  Mock setup:   galvo_flim_mock_scan_setup.json
  It simulates: the 64 x 64 pixel FLIM scan of tutorials 07 to 09 on the
                beads sample with two lifetimes; the cube is 64 x 64 x 391
                bins (6 MB).
  Your own microscope: a configured FLIM scan (the script loads
                scan_params/flim_scan_64px.json and puts your settings
                back); DETECTOR is found from the setup. A 512 x 512 cube
                at 391 bins is 400 MB: keep the cube for small scans.

Next: 12_gated_sted.py
"""

import os

from imswitch.imcontrol.model.timeresolved import load_gate_preset, load_products
from imswitch.imcontrol.model.workflows import (
    BinnedPhotonArrivalParams,
    BinnedPhotonArrivalWorkflow,
)

helpers = importScript('timetagger_helpers.py')
tt = helpers.findTimeTagger()
DETECTOR = helpers.findFlimDetector()
PARAMS = os.path.join(getScriptDirPath(), 'scan_params', 'flim_scan_64px.json')
PRESET = os.path.join(getScriptDirPath(), 'gate_presets', 'sted_early_late.json')
if DETECTOR is None:
    raise RuntimeError('This setup has no FLIM detector.')

backup, design = helpers.loadScanParams(PARAMS)
flimWas = helpers.setFlimEnabled(DETECTOR, True)
try:
    facade = api.imcontrol.buildWorkflowFacade(time_resolved_detector_name=DETECTOR)
    preset = load_gate_preset(PRESET)
    print(f"gates from {os.path.basename(PRESET)}: "
          + ', '.join(f'{g.name} {g.start_ns:g}-{g.stop_ns:g} ns ({g.reference})'
                      for g in preset['gates']))

    # The workflow configures the detector's products for this run only
    # (the cube and the gates), runs the Scan widget's scan, waits for the
    # final frame, clears the products again and writes the files.
    params = BinnedPhotonArrivalParams(
        gates=preset['gates'],
        save_folder=api.imcontrol.getRecFolder(),     # where the Recording widget points
        measurement_name='tutorial11_cube',
        save_h5=True, save_npz=False, save_tiff=True,
        timeout_s=120.0,
    )
    result = BinnedPhotonArrivalWorkflow(facade, params).run()
    products = result.products
    print(f'cube {products.cube_counts.shape} {products.cube_counts.dtype}, '
          f'{int(products.decay_counts.sum()):,} photons, global tau {products.global_tau_ns:.2f} ns, '
          f'{products.tcspc_direction} TCSPC, pile-up max {100 * products.pileup_max:.1f} %')
    print('written:')
    for label, path in result.output_paths.items():
        print(f'  {label}: {path}')
    print()

    # Read the HDF5 file back: the same object, plus what the file knows.
    loaded = load_products(result.output_paths['h5'])
    print(f'loaded format version {loaded.format_version}: cube {loaded.cube_counts.shape}, '
          f'gates {list(loaded.gate_images)}, card {loaded.metadata["time_tagger"]["model"]}')
    for name, bounds in loaded.metadata['gates'].items():
        print(f"  gate {name}: {bounds['start_ns']:g}-{bounds['stop_ns']:g} ns from the "
              f"{bounds['reference']} -> {bounds['resolved_start_ns']:.2f}-"
              f"{bounds['resolved_stop_ns']:.2f} ns on the histogram axis")
    print()
    print('The Lifetime widget (Gated STED mode, Save) writes the same file from the')
    print('same preset; a script and the panel never disagree about a gate.')
finally:
    helpers.restoreFlimEnabled(DETECTOR, flimWas)
    api.imcontrol.loadScanParamsFromFile(backup)
