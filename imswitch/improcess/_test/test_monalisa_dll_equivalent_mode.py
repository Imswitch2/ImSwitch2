"""Numerical reference and mode-state regressions; no GUI or Windows DLL."""
import numpy as np
import pytest

from imswitch.improcess.reconstructors.monalisa.dll_equivalent import (
    DllFastExtractionOperator, coefficient_grid_shape, python_dll_coefficients,
)
from imswitch.improcess.reconstructors.monalisa.lattice import Lattice
from imswitch.improcess.reconstructors.monalisa.params import (
    PRESET_DLL, PRESET_FAST, PRESET_FULL, PRESETS,
    apply_preset, default_params, effective_mode_params,
)
from imswitch.improcess.reconstructors.monalisa.spot_settings import fwhm_to_sigma_px


def test_modes_and_retained_science():
    assert PRESETS == (PRESET_FAST, PRESET_FULL, PRESET_DLL)
    params = default_params()
    params.update(estimator="joint", spot_fwhm=235., cell_offsets=True,
                  localization_mode="Manual", orientation_mode="Manual")
    full = apply_preset(params, PRESET_FULL)
    full.update(reassignment="fixed", shift_factor=.41, sharpen=True)
    dll = apply_preset(full, PRESET_DLL)
    assert dll["preset"] == PRESET_DLL
    assert dll["gaussian_mode"] == PRESET_FULL
    assert dll["gaussian_estimator"] == "joint"
    assert dll["localization_mode"] == full["localization_mode"]
    assert dll["cell_offsets"] is True
    effective = effective_mode_params(dll)
    assert effective["estimator"] == "dll_python"
    assert effective["background"] == "constant"
    assert effective["variance_weighting"] is False
    assert effective["reassignment"] == "off"
    assert not effective["sharpen"]
    restored = apply_preset(dll, PRESET_FULL)
    assert restored["estimator"] == "joint"
    assert restored["reassignment"] == "fixed"
    assert restored["shift_factor"] == pytest.approx(.41)
    assert restored["sharpen"] is True
    assert restored["spot_fwhm"] == 235


@pytest.mark.parametrize('fwhm,signal,bg', [
    (220, 2.1219284534454346, -215.28964),
    (320, 2.554807186126709, -293.79346),
])
def test_dll_measured_interior_impulse(fwhm, signal, bg):
    frame = np.zeros((1,28,28),dtype=np.uint16)
    frame[0,15,15] = 1
    weights = python_dll_coefficients(
        frame, (4.7,5.2,10.,10.),
        [fwhm/(2.355*77), 9999/(2.355*77)], (3,3),
    )
    assert weights[0,0,1,1] == pytest.approx(signal, rel=1e-6)
    assert weights[1,0,1,1] == pytest.approx(bg, rel=1e-5)


def test_operator_equals_reference_kernel_with_reordered_foci():
    # Spatial (x,y) lattice and its independently generated ordered focus list.
    lattice = Lattice.rectangular(10.,10.,5.2,4.7)
    x = np.array([25.2, 5.2, 15.2, 25.2])
    y = np.array([24.7, 4.7, 14.7, 14.7])
    sigma = fwhm_to_sigma_px(220.,'nm',77.)
    op = DllFastExtractionOperator.build(
        x,y,sigma, (28,28),lattice=lattice, pixel_size_nm=77., background='constant',
    )
    frames=np.zeros((2,28,28),dtype=np.uint16)
    frames[0,15,15]=200
    frames[1,5,5]=120
    actual = op.apply_device(frames, device='CPU')
    ref = python_dll_coefficients(frames, (4.7,5.2,10.,10.),
            [220/(2.355*77),9999/(2.355*77)], (3,3))
    rows=np.rint((y-4.7)/10).astype(int)
    cols=np.rint((x-5.2)/10).astype(int)
    np.testing.assert_allclose(actual.amplitude, ref[0,:,rows,cols].T,rtol=1e-6,atol=1e-6)
    np.testing.assert_allclose(actual.background, ref[1,:,rows,cols].T,rtol=1e-6,atol=1e-4)


def test_rejects_rotated_or_hexagonal_lattice():
    lat=Lattice.rectangular(10.,10.,5.2,4.7)
    assert coefficient_grid_shape((28,28), (4.7,5.2,10,10)) == (3,3)
    lat2 = Lattice(offset=lat.offset, a1=np.array([10.,2.]), a2=np.array([0.,10.]))
    with pytest.raises(ValueError,match='axis-aligned'):
        DllFastExtractionOperator.build([5.2],[4.7], 1.2, (28,28),
            lattice=lat2, pixel_size_nm=77.)


def test_modern_scan_pipeline_executes_dll_extraction_and_common_placement():
    from imswitch.improcess.reconstructors.monalisa.calibration import PatternLocalization
    from imswitch.improcess.reconstructors.monalisa.pipeline import PipelineParams, reconstruct_scan
    from imswitch.improcess.reconstructors.monalisa.scan_frame import raster_scan_index
    lat = Lattice.rectangular(10., 10., 5.2, 4.7)
    loc = PatternLocalization.from_lattice(lat, source="manual")
    frames = np.ones((4,28,28), dtype=np.uint16) * 30
    for j in range(4):
        frames[j, 15, 15] += 200 + j * 5
    params = PipelineParams(
        estimator="dll_python", background="constant", pixel_size_nm=77.,
        spot_sigma_px=fwhm_to_sigma_px(220., "nm", 77.), device="CPU",
        frame_gain="off", cell_offsets="off", reassignment="off",
        variance_weighting=False, pinhole_stack="off",
        footprint_shape="square", footprint_mode="from_lattice",
    )
    result = reconstruct_scan(
        frames, np.diag([.5,.5]), (2,2), params,
        orientation="+x+y", localization=loc,
        scan_index=raster_scan_index(2,2,"+x+y"),
    )
    assert np.isfinite(result.amplitude.image).any()
    assert result.geometry.diagnostics["estimator"] == "dll_python"
    assert "integrated Gaussian" in result.geometry.diagnostics["dll_equivalent_model"]
