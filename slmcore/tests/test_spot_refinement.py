import numpy as np
import pytest

from slmcore.core.cgh.localization.detection import (
    _refine_patch_gaussian,
    _refine_patches_gaussian,
    _refine_positions,
)
from slmcore.core.cgh.localization.model import SpotDetectionOptions
from slmcore.core.cgh.localization.parameters import LOCALIZATION_PARAMS
from slmcore.core.engine.parameters import ParamDisplayLevel


def _gaussian_image(shape,center,sigma=(1.1,1.4),amplitude=120.0,background=7.0):
    yy,xx = np.meshgrid(
        np.arange(shape[0],dtype=np.float64),
        np.arange(shape[1],dtype=np.float64),
        indexing="ij",
    )
    cx,cy = center
    sx,sy = sigma
    return background + amplitude * np.exp(
        -0.5 * (((xx - cx) / sx) ** 2 + ((yy - cy) / sy) ** 2)
    )


def test_gaussian_refinement_recovers_subpixel_center():
    center = np.asarray((10.37,9.62),dtype=np.float64)
    image = _gaussian_image((21,21),center)
    peak = np.asarray([[10.0],[10.0]],dtype=np.float64)

    refined = _refine_positions(image,peak,"gaussian",3)

    np.testing.assert_allclose(refined[:,0],center,atol=0.02)


def test_gaussian_refinement_supports_border_patch():
    center = np.asarray((1.18,1.42),dtype=np.float64)
    image = _gaussian_image((15,15),center,sigma=(0.9,1.0))
    peak = np.asarray([[1.0],[1.0]],dtype=np.float64)

    refined = _refine_positions(image,peak,"gaussian",3)

    np.testing.assert_allclose(refined[:,0],center,atol=0.04)



def test_batched_gaussian_matches_scalar_reference():
    rng = np.random.default_rng(7)
    count = 12
    size = 7
    yy,xx = np.meshgrid(
        np.arange(size,dtype=np.float64),
        np.arange(size,dtype=np.float64),
        indexing="ij",
    )
    centers = np.column_stack((
        rng.uniform(2.5,3.5,count),
        rng.uniform(2.5,3.5,count),
    ))
    sigmas = np.column_stack((
        rng.uniform(0.8,1.5,count),
        rng.uniform(0.8,1.5,count),
    ))
    amplitudes = rng.uniform(50.0,150.0,count)
    backgrounds = rng.uniform(0.0,10.0,count)

    patches = np.empty((count,size,size),dtype=np.float64)
    initial = np.empty((count,2),dtype=np.float64)
    for index in range(count):
        patch = backgrounds[index] + amplitudes[index] * np.exp(
            -0.5 * (
                ((xx - centers[index,0]) / sigmas[index,0]) ** 2
                + ((yy - centers[index,1]) / sigmas[index,1]) ** 2
            )
        )
        patch += rng.normal(0.0,1.0,patch.shape)
        patches[index] = patch
        weights = patch - np.min(patch)
        total = np.sum(weights)
        initial[index] = (
            np.sum(xx * weights) / total,
            np.sum(yy * weights) / total,
        )

    batched,valid = _refine_patches_gaussian(patches,initial)
    scalar = np.asarray([
        _refine_patch_gaussian(
            patch,x1=0,y1=0,initial=center,
        )
        for patch,center in zip(patches,initial)
    ])

    assert np.all(valid)
    np.testing.assert_allclose(batched,scalar,atol=1e-3)

def test_gaussian_refinement_falls_back_safely_on_flat_patch():
    image = np.ones((9,9),dtype=np.float64)
    peak = np.asarray([[4.0],[4.0]],dtype=np.float64)

    refined = _refine_positions(image,peak,"gaussian",2)

    np.testing.assert_array_equal(refined,peak)


def test_gaussian_is_a_valid_detection_refinement_method():
    options = SpotDetectionOptions(refinement_method="gaussian")
    assert options.refinement_method == "gaussian"

    with pytest.raises(ValueError,match="gaussian"):
        SpotDetectionOptions(refinement_method="unknown")


def test_spot_refinement_controls_are_not_advanced():
    assert LOCALIZATION_PARAMS["focus_localization_method"].display_level != ParamDisplayLevel.ADVANCED
    assert LOCALIZATION_PARAMS["focus_search_window_px"].display_level != ParamDisplayLevel.ADVANCED
    assert "gaussian" in LOCALIZATION_PARAMS["focus_localization_method"].choices
