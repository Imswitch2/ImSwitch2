"""Estimating the illumination profile from the run itself.

Shading is fixed to the detector: whatever the stage does, the same corner is
dim in every tile. The sample is not — a spiral visits a different piece of
specimen at each stop. Averaging tiles in detector coordinates therefore lets
the specimen cancel while the illumination envelope survives.

The interesting cases are the ones where that reasoning fails. Too few tiles,
or a specimen with its own large-scale gradient, and the "profile" is the
sample — at which point dividing by it does more damage than the shading it
was meant to remove. Those must be refused, not corrected.
"""

import numpy as np
import pytest

from imswitch.imcommon.algorithms.tile_mosaic import (
    SHADING_MIN_TILES,
    MosaicDataset,
    MosaicTile,
    assemble,
    estimate_shading_profile,
)


def _one_sided_ramp(height, width, bright=1.15, dim=0.55):
    """The shape Lenny's rig produces: falloff toward one edge, not radial."""
    ramp = np.linspace(bright, dim, width, dtype=np.float32)
    return np.repeat(ramp[None, :], height, axis=0)


def _tiles(profile, count, seed=0, scale=50.0):
    """Independent specimen fields seen through one fixed profile."""
    rng = np.random.default_rng(seed)
    return [
        (rng.gamma(2.0, scale, profile.shape).astype(np.float32) * profile)
        for _ in range(count)
    ]


def test_a_one_sided_profile_is_recovered():
    profile = _one_sided_ramp(96, 96)
    expected = profile / np.median(profile)

    estimated = estimate_shading_profile(_tiles(profile, 40))

    assert estimated is not None
    assert np.abs(estimated - expected).max() < 0.1


def test_correcting_flattens_the_falloff_it_found():
    profile = _one_sided_ramp(96, 96)
    tiles = _tiles(profile, 40)
    estimated = estimate_shading_profile(tiles)

    def ratio(fields):
        columns = np.mean([field.mean(axis=0) for field in fields], axis=0)
        return columns[:8].mean() / columns[-8:].mean()

    corrected = [tile / estimated for tile in tiles]

    assert ratio(tiles) > 1.8            # a 2x falloff going in
    assert ratio(corrected) == pytest.approx(1.0, abs=0.15)


def test_brightness_is_preserved_not_just_flattened():
    """The profile is normalised, so correction must not rescale the mosaic."""
    profile = _one_sided_ramp(96, 96)
    tiles = _tiles(profile, 40)

    estimated = estimate_shading_profile(tiles)
    before = np.mean([tile.mean() for tile in tiles])
    after = np.mean([(tile / estimated).mean() for tile in tiles])

    assert after == pytest.approx(before, rel=0.05)


def test_a_bright_tile_does_not_set_the_profile():
    """Tiles contribute their shape, not their brightness."""
    profile = _one_sided_ramp(96, 96)
    tiles = _tiles(profile, 20)
    balanced = estimate_shading_profile(tiles)

    # One field a hundred times brighter than the rest, same illumination.
    tiles[3] = tiles[3] * 100.0
    skewed = estimate_shading_profile(tiles)

    assert np.abs(skewed - balanced).max() < 0.05


def test_too_few_tiles_is_refused_with_a_reason():
    profile = _one_sided_ramp(64, 64)
    messages = []

    estimated = estimate_shading_profile(
        _tiles(profile, SHADING_MIN_TILES - 1), progress=messages.append
    )

    assert estimated is None
    assert any('average out' in message for message in messages)


def test_a_specimen_gradient_is_refused_rather_than_divided_out():
    """The failure mode that matters: structure that does not cancel.

    Every field carries the same steep gradient, so averaging cannot tell it
    from illumination. It exceeds what an illumination envelope looks like,
    and is refused instead of being burned into the mosaic inverted.
    """
    height = width = 64
    gradient = np.linspace(0.02, 1.0, width, dtype=np.float32)
    gradient = np.repeat(gradient[None, :], height, axis=0)
    messages = []

    estimated = estimate_shading_profile(
        [gradient * (index + 1) for index in range(20)],
        progress=messages.append,
    )

    assert estimated is None
    assert any('specimen gradient' in message for message in messages)


def test_tiles_of_different_shapes_are_refused():
    messages = []

    estimated = estimate_shading_profile(
        [np.ones((32, 32), np.float32)] * 5 + [np.ones((32, 48), np.float32)],
        progress=messages.append,
    )

    assert estimated is None
    assert any('same shape' in message for message in messages)


def test_leading_axes_collapse_rather_than_weighting_the_estimate():
    """Z planes of one field see one illumination, so they count once."""
    profile = _one_sided_ramp(64, 64)
    flat = _tiles(profile, 20, seed=7)
    stacks = [np.stack([field, field * 0.5, field * 2.0]) for field in flat]

    assert np.abs(
        estimate_shading_profile(stacks) - estimate_shading_profile(flat)
    ).max() < 0.02


def test_the_correction_is_off_unless_asked_for():
    profile = _one_sided_ramp(32, 32)
    fields = _tiles(profile, 12, seed=3)
    dataset = MosaicDataset(
        tiles=[
            MosaicTile(
                name=f'tile-{index}', data=field,
                position=(0.0, float(index * 32)),
                grid=(index, 0), tile_id=index,
            )
            for index, field in enumerate(fields)
        ],
        pixel_size_um=(1.0, 1.0),
        tile_step_um=32.0,
    )

    plain = assemble(dataset, blend=False)
    corrected = assemble(dataset, blend=False, shading_correction=True)

    assert not np.allclose(plain, corrected)
    np.testing.assert_allclose(plain[:, :32], fields[0], rtol=1e-5)
