"""Tutorial timetagger 14 -- Several detectors on one card, counting photons.

You will learn
  * the ``inputs`` block: the card's inputs by name and kind, and which
    role (if any) each one plays -- ``tt.inputs()``, ``tt.channels()``
  * that every detector input has its own trigger level, and that
    ``find_trigger_level`` takes an input by name; its setup line goes into
    that input's entry of the ``inputs`` map
  * one scan, one card, two images: each detector names its
    ``photon_input`` and counts photons per pixel (``measurement`` is
    ``counting``: no laser sync, no fit), all between the same pixel markers
  * how to check that each image came from the input it says it did

Setup
  Mock setup:   galvo_two_apd_counting_mock_setup.json
  It simulates: two APDs on inputs 1 and 2 of one Time Tagger and the
                scan's line and frame clocks on 3 and 4; no laser sync. The
                mock gives each input its own sample (red: the green sample
                with a brightness ramp across x), so the two images differ.
  Your own microscope: a timeTagger block with an inputs map and one
                SwabianTimeTaggerManager per detector input, each with its
                photon_input. On a rig the truth is your own sample: put the
                two images side by side (two colours, two polarisations).

Next: 01_meet_the_card.py and the rest of this folder work on any input by
      name; 10_flim_preflight.py lists every detector input.
"""

import os

import numpy as np

helpers = importScript('timetagger_helpers.py')
tt = helpers.findTimeTagger()
PARAMS = os.path.join(getScriptDirPath(), 'scan_params', 'flim_scan_64px.json')

# -- the card's inputs ----------------------------------------------------- #
print('The card\'s inputs (the timeTagger block\'s "inputs" map):')
helpers.printTable(
    [(name, c.kind, f'{c.channel:+d}', c.plays or '-', f'{c.trigger_v:+.2f} V',
      f'{c.deadtime_ps / 1000:g} ns') for name, c in tt.channels().items()],
    header=('input', 'kind', 'channel', 'role', 'trigger', 'dead time'))
print()
detectorInputs = tt.detector_inputs()
print(f'Detector inputs: {", ".join(detectorInputs)}. Roles played: '
      f'{", ".join(tt.roles()) or "none"} -- with two detector inputs, "photons" is no '
      'role at all: each detector names its input.')
print()

# -- the detectors on the card --------------------------------------------- #
detectors = {}
for name in api.imcontrol.getDetectorNames():
    params = api.imcontrol.getDetectorParameters(name)
    if 'click_role' in params:
        detectors[name] = (params['click_role']['value'], params['measurement']['value'])
print('Detectors on the card:')
helpers.printTable([(name, photonInput, measurement)
                    for name, (photonInput, measurement) in detectors.items()],
                   header=('detector', 'photon input', 'measurement'))
print()

# -- a level per input ----------------------------------------------------- #
rates = tt.count_rates(detectorInputs, duration_s=0.2).rates_hz
for name in detectorInputs:
    result = tt.find_trigger_level(name, duration_s=0.02)
    print(f'{name}: {rates[name]:,.0f} Hz; {result.verdict}, level {result.level_v:+.3f} V '
          f'-> in inputs["{name}"]: {result.block_line()}')
print('(not applied: find_trigger_level puts every level back unless apply=True)')
print()

# -- one scan, one image per detector -------------------------------------- #
backup, design = helpers.loadScanParams(PARAMS)
try:
    runScanAndWait(timeout=120)
    images = {name: np.asarray(api.imcontrol.getDetectorLatestFrame(name), dtype=float)[-1]
              for name in detectors}
finally:
    api.imcontrol.loadScanParamsFromFile(backup)

print('One scan, one image per detector:')
for name, image in images.items():
    print(f'  {name}: {image.shape[1]} x {image.shape[0]} px, {image.sum():,.0f} photons, '
          f'brightest pixel {image.max():,.0f}')
print()


def match(image, truth):
    """How well an image follows a truth map: the correlation of the two."""
    a, b = image.ravel() - image.mean(), truth.ravel() - truth.mean()
    return float(a @ b / max(1e-12, np.sqrt((a @ a) * (b @ b))))


if tt.is_mock:
    print('Each image against each input\'s truth (the mock\'s samples):')
    rows = []
    for name, image in images.items():
        own = detectors[name][0]
        scores = {other: match(image, tt.mock_truth(*image.shape, other)[0])
                  for other in detectorInputs}
        best = max(scores, key=scores.get)
        rows.append((name, own, best, ', '.join(f'{k} {v:.2f}' for k, v in scores.items())))
        if best != own:
            raise RuntimeError(f'{name} reads {own} but its image looks like {best}: '
                               'cross-wired?')
    helpers.printTable(rows, header=('detector', 'input', 'looks like', 'correlation'))
    print('  -> every image came from its own input.')
else:
    print('On a rig: compare the images with what each detector should see (two '
          'colours, two polarisations); a swapped cable swaps them.')
