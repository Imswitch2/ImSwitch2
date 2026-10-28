"""The geometry a point-scan frame carries to the viewer (plan D1).

Checks the pure part:

- ``frame_geometry_from_scan`` records the scan's **nominal** pixel grid, the
  same grid a recording writes as its OME pixel size, for lengths that are a
  whole number of steps and for lengths that are not (D4 keeps imported
  lengths verbatim).
- Against the generated galvo waveform, that grid is exact on the slow axis
  and centred correctly on the fast axis.
- Two designer defects found while writing these tests keep the fast axis
  from realizing the grid exactly: a pitch about 0.6–1 % too large, and a
  read window that starts early for non-integral lengths. They are pinned
  below as strict expected failures; fixing them would change Advanced.
- A ``ScanFrame`` carries its geometry through ImSwitch's signals without
  copying pixels, while nothing derived from it does.
"""
import copy

import numpy as np
import pytest

from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.imcontrol.model.scan_frame import (
    AxisGeometry,
    FRAME_GEOMETRY_KEY,
    FrameGeometry,
    ScanFrame,
    frame_geometry_from_scan,
    frame_geometry_from_scan_info,
    frame_geometry_of,
    with_frame_geometry,
)
from imswitch.imcontrol.model.signaldesigners.GalvoScanDesigner import (
    GalvoScanDesigner,
)

from .test_galvo_signal_goldens import _params, _setup_sted_like

X_CONV = 17.44  # GalvoX, µm per V
Y_CONV = 16.63  # GalvoY


def _build(lengthX, stepX, lengthY, stepY, centres=(3.0, -2.0), dwell_s=40e-6):
    setup = _setup_sted_like()
    setup.positioners["PiezoZ"].managerProperties["smoothScan"] = False
    params = _params(["GalvoX", "GalvoY", "PiezoZ"],
                     [lengthX, lengthY, 1.0], [stepX, stepY, 1.0],
                     centers=[centres[0], centres[1], 0.0])
    params["sequence_time"] = dwell_s
    signals, _, info = GalvoScanDesigner().make_signal(params, setup)
    return params, signals, info


def _firstLineSampleOffset(info):
    """Sample index at which the detector starts reading the first line.

    The APD's ScanWorker throws the start zero-padding, then the smooth
    initial positioning, settling and starting acceleration before its first
    line (APDManager ScanWorker._runAcquisition). The phase delay is thrown
    too, but it compensates the galvo's lag: the commanded position at this
    index is where the mirror is when the pixel is read.
    """
    initpos = info['scan_pads_initpos'][0] if info['scan_pads_initpos'] else 0
    return (info['scan_throw_startzero'] + initpos
            + info['scan_throw_settling'] + info['scan_throw_startacc'])


def _commandedFastAxisAtPixelCentres(signals, info, count):
    spp = info['samples_per_pixel']
    start = _firstLineSampleOffset(info)
    samples = np.arange(len(signals['GalvoX']))
    return np.array([
        np.interp(start + (pixel + 0.5) * spp, samples, signals['GalvoX']) * X_CONV
        for pixel in range(count)
    ])


@pytest.mark.parametrize('dwell_s', [40e-6, 20e-6])
def test_the_geometry_matches_the_waveform_where_the_designer_is_exact(dwell_s):
    """A whole number of steps (what Simple creates): the first line is
    centred where the geometry says, and the slow axis holds each line's
    position exactly. The fast axis's pitch is the designer's defect below."""
    params, signals, info = _build(10.0, 0.5, 2.0, 0.5, dwell_s=dwell_s)
    geometry = frame_geometry_from_scan(params, info)
    x, y = geometry.axes
    assert (x.device, y.device) == ('GalvoX', 'GalvoY')
    assert (x.count, y.count) == tuple(info['img_dims'])

    commanded = _commandedFastAxisAtPixelCentres(signals, info, x.count)
    nominal = np.array([x.position_um(j) for j in range(x.count)])
    assert commanded.mean() == pytest.approx(nominal.mean(), abs=0.02 * 0.5)

    samples = np.arange(len(signals['GalvoY']))
    firstLineMiddle = (_firstLineSampleOffset(info)
                       + x.count * info['samples_per_pixel'] / 2)
    commandedY = np.interp(firstLineMiddle, samples, signals['GalvoY']) * Y_CONV
    assert commandedY == pytest.approx(y.first_um, abs=1e-6)


@pytest.mark.xfail(strict=True, reason=(
    'GalvoScanDesigner defect found 2026-09-25: the swept fast axis realizes '
    'a pixel pitch 0.6 % (40 us dwell) to 1 % (20 us) larger than the step, '
    'so pixels drift from the nominal grid towards both line ends. The '
    'geometry records the nominal grid, as recordings do. Fixing the '
    'designer changes Advanced and is separate work; this flips when it is '
    'fixed.'))
@pytest.mark.parametrize('dwell_s', [40e-6, 20e-6])
def test_the_fast_axis_realizes_the_nominal_pitch(dwell_s):
    params, signals, info = _build(10.0, 0.5, 2.0, 0.5, dwell_s=dwell_s)
    x = frame_geometry_from_scan(params, info).axes[0]
    commanded = _commandedFastAxisAtPixelCentres(signals, info, x.count)
    assert np.mean(np.diff(commanded)) == pytest.approx(0.5, rel=1e-3)


@pytest.mark.xfail(strict=True, reason=(
    'GalvoScanDesigner defect found 2026-09-25: for a length that is not a '
    'whole number of steps (10 um at 0.3 um) the detector reads the first '
    'pixel about 0.7 pixel before the sweep reaches centre - length/2, i.e. '
    'while the mirror is still accelerating. Separate from D1; flips when the '
    'designer is fixed.'))
def test_a_non_integral_line_is_read_where_the_sweep_is():
    params, signals, info = _build(10.0, 0.3, 2.0, 0.5)
    x = frame_geometry_from_scan(params, info).axes[0]
    commanded = _commandedFastAxisAtPixelCentres(signals, info, x.count)
    assert commanded[0] == pytest.approx(x.first_um, abs=0.05 * 0.3)


def test_a_non_integral_length_is_not_centred_by_the_pixel_count():
    """Why the geometry carries the first pixel, not the centre: at 10 µm and
    0.3 µm the sweep starts at centre - 5, not centre - 32/2 * 0.3 = -4.8."""
    params, _, info = _build(10.0, 0.3, 2.0, 0.5, centres=(0.0, 0.0))
    x = frame_geometry_from_scan(params, info).axes[0]
    assert x.first_um == pytest.approx(-5.0 + 0.15)
    assert x.first_um != pytest.approx(-(x.count - 1) / 2 * 0.3)


def test_a_stepped_axis_is_centred_and_a_mock_axis_starts_at_zero():
    params = {
        'target_device': ['PiezoZ', 'Mock-Repeat'],
        'axis_length': [6.0, 4.0],
        'axis_step_size': [0.5, 1.0],
        'axis_centerpos': [5.0, 50.0],
    }
    info = {'axis_names': ['PiezoZ', 'Mock-Repeat'], 'img_dims': [12, 4],
            'smooth_axes': [False, False]}
    z, repeat = frame_geometry_from_scan(params, info).axes
    assert z.first_um == pytest.approx(5.0 - 11 / 2 * 0.5)
    assert repeat.first_um == 0.0


def test_a_pixel_count_that_disagrees_with_the_parameters_is_an_error():
    params, _, info = _build(10.0, 0.5, 2.0, 0.5)
    info = dict(info, img_dims=[21, 4])
    with pytest.raises(ValueError, match='GalvoX'):
        frame_geometry_from_scan(params, info)


def test_the_geometry_travels_through_scan_info_as_plain_data():
    geometry = FrameGeometry(
        axes=(AxisGeometry('X', 0.5, 20, -4.75), AxisGeometry('Y', 0.5, 4, -0.75)),
        run=3, iteration=7,
    )
    info = {FRAME_GEOMETRY_KEY: geometry.to_dict()}
    assert frame_geometry_from_scan_info(copy.deepcopy(info)) == geometry
    assert frame_geometry_from_scan_info({}) is None
    assert frame_geometry_from_scan_info(None) is None


def test_a_scan_frame_shares_pixels_and_derived_arrays_carry_no_geometry():
    geometry = FrameGeometry(axes=(AxisGeometry('X', 1.0, 4, 0.0),))
    storage = np.zeros((3, 4), dtype=np.uint16)

    frame = with_frame_geometry(storage, geometry)

    assert isinstance(frame, ScanFrame)
    assert frame_geometry_of(frame) is geometry
    assert np.shares_memory(frame, storage)          # no copy
    for derived in (frame[1:], frame * 2, np.rot90(frame), frame.copy()):
        assert frame_geometry_of(derived) is None
    assert frame_geometry_of(storage) is None


def test_no_geometry_returns_the_very_same_object():
    """Every scan without a geometry publishes exactly what it did before."""
    storage = np.zeros((3, 4))
    assert with_frame_geometry(storage, None) is storage


def test_the_geometry_survives_a_queued_signal_across_threads(qtbot):
    from qtpy import QtCore

    class Source(SignalInterface):
        sigImage = Signal(np.ndarray, bool, list)

    geometry = FrameGeometry(axes=(AxisGeometry('X', 1.0, 4, 0.0),))
    frame = with_frame_geometry(np.arange(12.0).reshape(3, 4), geometry)
    source = Source()
    received = []
    source.sigImage.connect(lambda im, init, scale: received.append(im))

    class Emitter(QtCore.QObject):
        def emitFrame(self):
            source.sigImage.emit(frame, True, [1.0, 1.0])

    thread = QtCore.QThread()
    emitter = Emitter()
    emitter.moveToThread(thread)
    thread.started.connect(emitter.emitFrame)
    thread.start()
    try:
        qtbot.waitUntil(lambda: len(received) == 1, timeout=2000)
    finally:
        thread.quit()
        thread.wait()

    assert received[0] is frame
    assert frame_geometry_of(received[0]) is geometry


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
