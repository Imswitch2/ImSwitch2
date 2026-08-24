"""Repro/demonstrator for docs/galvo-designer-single-axis-findings.md.

Originally the crash reproduction; since the branch's fixes EVERY case below
generates. With the real example_sted setup (ND-GalvoX conv 17.44, ND-GalvoY
conv 16.63, ND-PiezoZ conv 1.0 / vel_max 1000 / acc_max 1000, smoothScan
false since the Phase B fix -> Z-only runs as a stepped staircase):

1. Z-piezo-only scan — used to crash at __add_start_end (np.min of the empty
   ``np.tile(period, n_d2 - 1 = 0)`` middle); now a single line.
2. XZ (galvo fast, piezo d2) — the classic intended use, unchanged.
3. ZX (piezo as fast axis) — used to crash on an empty end-slice argmax.
4. XZ whose Z collapses to 1 step — degenerates to a working single line.
5. Degenerate XZ (2-step X) — the historical zero-change rig workaround.
6. BetaScanDesigner convFactor mapping: Z on dim 0 used to be divided by the
   galvo's factor (+-0.27 V, a silent 17x shrink); since the defect-4 fix
   both dim orders give the correct +-4.75 V.

Run headless:  QT_QPA_PLATFORM=offscreen python scripts/diagnostics/repro-single-axis-scan.py
"""
import os
import traceback

import numpy as np

from imswitch.imcontrol.view.guitools.ViewSetupInfo import ViewSetupInfo
from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import GalvoScanDesigner
from imswitch.imcontrol.model.signaldesigners.BetaScanDesigner import BetaScanDesigner

SETUP_JSON = os.path.expanduser(
    '~/ImSwitchConfig/imcontrol_setups/example_sted.json')

with open(SETUP_JSON) as f:
    setupInfo = ViewSetupInfo.from_json(f.read(), infer_missing=True)

designer = GalvoScanDesigner()
beta = BetaScanDesigner()

BASE = {
    'axis_centerpos': [0.0, 0.0, 0.0],
    'axis_startpos': [[0], [0], [0]],
    'sequence_time': 2e-05,
    'phase_delay': 100.0,
    'd3step_delay': 0.0,
}


def run(name, target, lengths, steps, use_beta=False):
    p = dict(BASE)
    p['target_device'] = target
    p['axis_length'] = lengths
    p['axis_step_size'] = steps
    if use_beta:
        p['return_time'] = 0.01
    label = 'BETA ' if use_beta else ''
    print(f'=== {label}{name} ===')
    print(f'    targets={target} lengths={lengths} steps={steps}')
    try:
        sig, positions, info = (beta if use_beta else designer).make_signal(p, setupInfo)
        print(f'    OK  img_dims={info["img_dims"]}'
              f' samples_total={info["scan_samples_total"]}')
        for dev, s in sig.items():
            print(f'    {dev}: len={len(s)} min={np.min(s):.4f} max={np.max(s):.4f} V')
    except Exception as e:
        tb = traceback.extract_tb(e.__traceback__)[-1]
        print(f'    CRASH {type(e).__name__}: {e}')
        print(f'    at {tb.filename.split("/")[-1]}:{tb.lineno} ({tb.name})')
    print()


# 1. The original bug: only the Z piezo active (dummy 1-step galvo entries,
#    exactly what AdvancedScanParameterSerializer.build_analog produces).
run('Z-only (crash case)',
    ['ND-PiezoZ', 'ND-GalvoX', 'ND-GalvoY'],
    [10.0, 1.0, 1.0], [0.5, 1.0, 1.0])

# 2. The intended use: galvo fast, piezo stepping on d2.
run('XZ (galvo fast, piezo d2) — intended use',
    ['ND-GalvoX', 'ND-PiezoZ', 'ND-GalvoY'],
    [5.0, 10.0, 1.0], [0.1, 0.5, 1.0])

# 3. Piezo as the FAST axis with a real second axis.
run('ZX (piezo fast, galvo d2) — piezo on d1',
    ['ND-PiezoZ', 'ND-GalvoX', 'ND-GalvoY'],
    [10.0, 5.0, 1.0], [0.5, 0.1, 1.0])

# 4. XZ where Z collapses to a single plane (1 step) -> 1 active axis again.
run('XZ with 1-step Z (single plane)',
    ['ND-GalvoX', 'ND-PiezoZ', 'ND-GalvoY'],
    [5.0, 0.5, 1.0], [0.1, 0.5, 1.0])

# 5. Zero-change rig workaround: degenerate XZ with a 2-step X.
run('Degenerate XZ (2-step X, piezo Z d2) — workaround',
    ['ND-GalvoX', 'ND-PiezoZ', 'ND-GalvoY'],
    [0.2, 10.0, 1.0], [0.1, 0.5, 1.0])

# 6. Beta designer: correct only when dims follow setup-JSON positioner order.
run('Z slow, X/Y size 0 (setup order) — correct',
    ['ND-GalvoX', 'ND-GalvoY', 'ND-PiezoZ'],
    [0.0, 0.0, 10.0], [0.1, 0.1, 0.5], use_beta=True)

run('Z FIRST (fast axis) — was the positional convFactor trap, now correct',
    ['ND-PiezoZ', 'ND-GalvoX', 'ND-GalvoY'],
    [10.0, 0.0, 0.0], [0.5, 0.1, 0.1], use_beta=True)
