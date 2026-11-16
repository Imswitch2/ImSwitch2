"""Every raster read window sees only its own row (GalvoScanDesigner).

Two timing defects, found by the sparse-scanning end-to-end test:

- Off-centre fields: when the fast axis's initial positioning took longer than
  the slow axis's, the slow axis's first hold was not shortened by its own
  positioning, so every Y step came late by it -- far enough off-centre past
  the next read window, and line k was imaged at row k-1.
- Line steps: the per-row holds were expanded per line step afterwards, which
  repeated the long first period into row 0's later line steps, so row 0
  stepped early, inside its last line step's read window (also centred).
"""
import numpy as np
import pytest

from imswitch.imcontrol.model.scan_parameters import axis_pixel_positions
from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import GalvoScanDesigner

from .test_galvo_signal_goldens import _params, _setup_sted_like

pytestmark = pytest.mark.nohardware

N_X, N_Y, STEP = 12, 16, 0.1


@pytest.mark.parametrize("line_steps", [1, 3])
@pytest.mark.parametrize("centre", [
    (0.0, 0.0), (8.0, 8.0),
    (-16.2, 0.0),   # fast axis positions longest
    (-16.2, 9.1),   # ... by more than the flyback after the turnaround
    (0.0, -16.2),   # slow axis positions longest
])
def test_every_read_window_holds_its_own_row(centre, line_steps):
    setup = _setup_sted_like()
    setup.positioners["PiezoZ"].managerProperties["smoothScan"] = False
    params = _params(["GalvoX", "GalvoY", "PiezoZ"], [N_X * STEP, N_Y * STEP, 1.0],
                     [STEP, STEP, 1.0], n_linesteps=line_steps)
    params["axis_centerpos"][:2] = list(centre)
    signals, _, info = GalvoScanDesigner().make_signal(params, setup)

    conv = setup.positioners["GalvoY"].managerProperties["conversionFactor"]
    y = signals["GalvoY"] * conv
    rows = axis_pixel_positions(N_Y, STEP, center=centre[1])
    init = info["scan_pads_initpos"]
    # where the APD worker starts reading the first line
    first = (info["scan_throw_startzero"] + init[0] + info["scan_throw_settling"]
             + info["scan_throw_startacc"] + max(0, max(init[1:]) - init[0]))
    period, line = info["scan_samples_d2_period"], info["scan_samples"][1]
    for k in range(N_Y):
        for j in range(line_steps):
            start = first + period * (k * line_steps + j)
            window = y[start:start + line]
            assert np.allclose(window, rows[k], atol=1e-9), (centre, line_steps, k, j)
