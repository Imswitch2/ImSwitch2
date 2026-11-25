"""Red-zone proof: a setup whose ``calibCsvPath`` file loads today drives the
laser exactly as before, for both managers that apply calibrations.

``legacy_lut`` is the removed ``create_lut_from_calib`` of
``NidaqLaserManager`` / ``AAAOTFLaserManager``, copied verbatim. Every
percent the old table converted must give the identical drive; every
percent it refused (NaN) must still be refused, except the rounding gap just
below 100 % (now the top of the table, see ``laser_power.py``).
"""
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.interpolate import interp1d

from imswitch.imcontrol._test.unit.test_aa_aotf_laser_manager import _build as build_aa
from imswitch.imcontrol._test.unit.test_laser_power_lut import FakeDaq


def legacy_lut(calib_csv_path):
    """Verbatim from origin/main (NidaqLaserManager l. 127-131)."""
    data = np.loadtxt(calib_csv_path)
    data[:, 1] -= data[:, 1].min()
    data[:, 1] /= data[:, 1].max() * 0.01 # convert to %
    return interp1d(data[:, 1], data[:, 0], bounds_error=False)


MEASURE_HEADER = """# format: imswitch calibCsvPath LUT (raw drive -> optical power)
# laser: 775
# setting unit: V
# laser wavelength nm: 775.0
# meter: PM100D (simulated) P0011748
# plane: BFP
# zeroed in the dark: True
# created: 2026-11-25T07:21:41+00:00
# notes:
# columns: setting power_W  (calibCsvPath LUT; fitted, strictly increasing)
"""


def _curves():
    rng = np.random.default_rng(20261125)
    curves = {
        'measured_W': (MEASURE_HEADER, np.array([
            [0, 1.61741784e-05], [0.5, 0.000211601106], [1, 0.000779765189],
            [1.5, 0.00166469083], [2, 0.00278000788], [2.5, 0.00401603818],
            [3, 0.00525215791], [3.5, 0.00636699948], [4, 0.0072520014],
            [4.5, 0.00782026582], [5, 0.00801597146]])),
        'plain_two_column': ('', np.array([[0.0, 0.0], [1.0, 2.5], [2.5, 7.0], [5.0, 10.0]])),
        # a dark start: equal power at the lowest drives (plateau)
        'dark_plateau': ('', np.array([[0.0, 0.1], [0.5, 0.1], [1.0, 0.4], [3.0, 3.0],
                                       [5.0, 4.2]])),
        # rows not sorted by drive, a third (std) column
        'unsorted_three_columns': ('# hand edited\n', np.array([
            [3.0, 6.0, 0.1], [0.0, 0.2, 0.1], [1.5, 2.0, 0.1], [5.0, 9.0, 0.1]])),
    }
    for i in range(6):
        drive = np.sort(rng.uniform(0, 5, 12))
        power = np.cumsum(rng.uniform(0.001, 0.02, 12)) * rng.choice([1e-3, 1.0, 37.0])
        curves[f'random_{i}'] = ('', np.column_stack([drive, power]))
    return curves


def _write(tmp_path, name, header, rows, mw=False):
    path = tmp_path / f'{name}.csv'
    if mw:
        header = ('# Raw AOTF power, measured optical power [mW] | laser=561AOTF, '
                  'wavelength=561 nm, frequency=96 MHz\n')
    text = header + '\n'.join(' '.join(repr(float(v)) for v in row) for row in rows) + '\n'
    if mw:
        text += '# aligned 2026-11-20\n'
    path.write_text(text)
    return str(path)


PERCENTS = np.concatenate([np.linspace(-5, 105, 441), [0.0, 1e-9, 50.0, 99.9999999, 100.0]])


def _nidaq(path):
    from imswitch.imcontrol.model.managers.lasers.NidaqLaserManager import NidaqLaserManager

    daq = FakeDaq()
    daq.calls = []
    original = daq.setAnalog

    def setAnalog(**kw):
        daq.calls.append(kw['voltage'])
        return original(**kw)

    daq.setAnalog = setAnalog
    info = SimpleNamespace(managerProperties={'calibCsvPath': path}, wavelength=775,
                           valueRangeMin=0.0, valueRangeMax=5.0, valueRangeStep=0.01,
                           getAnalogChannel=lambda: 'ao0', getDigitalLine=lambda: 0)
    return NidaqLaserManager(info, '775', nidaqManager=daq), daq


def _expect(lut, data_path, p):
    """What the new code must do at ``p``: the legacy drive, or refuse."""
    legacy = float(lut(p))
    if np.isfinite(legacy):
        return legacy
    top = float(np.nanmax(lut.x))
    if top < p <= 100.0:            # the rounding gap below 100 %
        return float(lut(top))
    return None


@pytest.mark.parametrize('name', list(_curves()))
def test_nidaq_percent_to_volts_is_unchanged(tmp_path, name):
    header, rows = _curves()[name]
    path = _write(tmp_path, name, header, rows)
    lut = legacy_lut(path)
    manager, daq = _nidaq(path)
    assert manager.valueUnits == '%' and manager.usesCalibrationLookup()
    for p in PERCENTS:
        expected = _expect(lut, path, p)
        before = len(daq.calls)
        result = manager.setValue(float(p))
        if expected is None:
            assert result is False and len(daq.calls) == before, p
        else:
            assert daq.calls[-1] == expected, (p, daq.calls[-1], expected)   # bit for bit
            assert manager.applyValue(float(p)) == expected


@pytest.mark.parametrize('name', list(_curves()))
@pytest.mark.parametrize('mw', [False, True])
def test_aa_aotf_percent_to_amplitude_is_unchanged(tmp_path, name, mw):
    header, rows = _curves()[name]
    rows = rows.copy()
    rows[:, 0] = np.round(rows[:, 0] * 200)        # amplitudes 0..1000
    rows = rows[np.unique(rows[:, 0], return_index=True)[1]]
    path = _write(tmp_path, name, header.replace('setting unit: V', 'setting unit: arb'),
                  rows, mw=mw)
    lut = legacy_lut(path)
    manager, rs232 = build_aa(calibCsvPath=path)
    assert manager.valueUnits == '%'
    for p in PERCENTS:
        expected = _expect(lut, path, p)
        rs232.cmds.clear()
        manager.setValue(float(p))
        if expected is None:
            assert rs232.cmds == [], p
        else:
            assert rs232.cmds == [f'L1P{int(expected)}'], (p, rs232.cmds, expected)


def test_the_rounding_gap_below_100_percent_existed(tmp_path):
    """Why 100 % is special-cased: the legacy table refused it for this file."""
    rows = np.array([[0.0, 0.0], [1.0, 3.0], [2.0, 7.0]])
    path = _write(tmp_path, 'gap', '', rows)
    lut = legacy_lut(path)
    assert np.isnan(lut(100.0))
    manager, daq = _nidaq(path)
    assert manager.setValue(100.0) is not False
    assert daq.calls[-1] == 2.0
